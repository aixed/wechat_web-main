from pathlib import Path
import sys
import unittest

from domp_submission_bridge import SUBMISSION_TOOL, SUBMISSION_PROMPT, SUBMISSION_REPLY_PREFIX, submission_candidate, submission_arguments, submission_reply, submission_reply_chunks
from domp_execution_bridge import EXECUTION_TOOL, EXECUTION_PROMPT
from domp_query_bridge import QUERY_TOOL, query_candidate
from mcp_service import McpServiceError
import test_domp_query_bridge as fixtures

ANALYSIS = {'matched': True, 'confidence': 98, 'result': '{"action":"submit","identifier":"41356"}'}
PAYLOAD = {'identifier': '41356', 'submitted': True, 'work_num': '5400000000383911', 'area_name': '山南市.扎囊县', 'reused': False}


class SubmissionBridgeTests(unittest.TestCase):
    def test_explicit_submission_phrases_and_original_identifier(self):
        for source in ('提交数据运维填报单 41356', '提交运维单 41356', '帮我提交数据运维单41356', '老师，麻烦把运维单41356提交一下', '提交运维单 xzsb-41356'):
            self.assertTrue(submission_candidate(source), source)
            self.assertEqual({'identifier': '41356', 'force': False}, submission_arguments(source, ANALYSIS))
        for source in ('41356提交了吗', '查一下41356提交结果', '不要提交运维单41356', '明天提交运维单41356', '他说帮我提交运维单41356', '如何提交运维单41356', '提交运维单41356并执行', '提交运维单41356并审核', '需求41356 UPDATE t SET a=1 WHERE id=1', '重新提交运维单41356', '我已提交运维单41356'):
            self.assertFalse(submission_candidate(source), source)
        for source, analysis in (('提交运维单41356和41357', ANALYSIS), ('提交运维单5400000000383911', ANALYSIS), ('提交运维单41356', {**ANALYSIS, 'result': '{"action":"submit","identifier":"41357"}'}), ('提交运维单41356', {**ANALYSIS, 'result': '{"action":"execute","identifier":"41356"}'})):
            with self.assertRaises(ValueError):
                submission_arguments(source, analysis)

    def test_truthful_result_and_reply_loop_marker(self):
        reply = submission_reply(PAYLOAD, '41356')
        self.assertIn('保存并提交成功', reply)
        self.assertIn('需求编号：5400000000383911', reply)
        self.assertIn('未确认提交成功', submission_reply({**PAYLOAD, 'submitted': False}, '41356'))
        self.assertIn('待核对', submission_reply({**PAYLOAD, 'identifier': '41357'}, '41356'))
        self.assertNotIn('保存并提交成功', submission_reply(None, '41356'))
        chunks = submission_reply_chunks(reply + '\n异常：' + '全部原因' * 1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1800 and not submission_candidate(chunk) and not query_candidate(chunk) for chunk in chunks))


class SubmissionGroupTests(unittest.IsolatedAsyncioTestCase):
    process = fixtures.QueryGroupTests.process
    message = fixtures.QueryGroupTests.message

    async def asyncSetUp(self):
        await fixtures.QueryGroupTests.asyncSetUp(self)
        row = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)
        row['ai_tasks'].extend([
            {**row['ai_tasks'][0], 'id': 'submit', 'skill_id': 'domp_submission_agent_text', 'instruction': SUBMISSION_PROMPT, 'mcp_tool_name': SUBMISSION_TOOL},
            {**row['ai_tasks'][0], 'id': 'execute', 'instruction': EXECUTION_PROMPT, 'mcp_tool_name': EXECUTION_TOOL},
        ])
        self.cache.upsert_smart_reply_config(row, owner_wxid=self.owner)
        self.submission_analysis = ANALYSIS
        async def analyze(content, task):
            self.analyzed_messages.append((content, task.get('mcp_tool_name')))
            return self.submission_analysis if task.get('mcp_tool_name') == SUBMISSION_TOOL else {'matched': False, 'confidence': 99, 'result': ''}
        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            if self.error:
                raise McpServiceError('提交响应超时')
            return {'structured': PAYLOAD}
        self.main.ai_service.analyze = analyze
        self.main.mcp_service.call_tool = call_tool

    async def test_two_requested_phrases_route_only_submission_for_sender_and_owner(self):
        for index, (source, sender) in enumerate((
            ('提交数据运维填报单 41356', self.sender), ('提交运维单 41356', self.sender),
            ('提交数据运维填报单 41356', self.owner), ('提交运维单 41356', self.owner),
        )):
            await self.process(self.message(id=str(500 + index), msg=source, fromid=sender, isSender=int(sender == self.owner)))
            self.assertEqual((self.chat, sender), self.send.call_args.args[:2])
            self.assertIn('需求编号：5400000000383911', self.send.call_args.args[3])
        self.assertEqual([SUBMISSION_TOOL] * 4, [call['tool_name'] for call in self.calls])
        self.assertTrue(all(call['arguments'] == {'identifier': '41356', 'force': False} for call in self.calls))
        self.assertEqual([SUBMISSION_TOOL] * 4, [kind for _, kind in self.analyzed_messages])
        reply = self.send.call_args.args[3]
        await self.process(self.message(id='504', msg=reply, fromid=self.owner, isSender=1))
        self.assertEqual(4, len(self.calls))

    async def test_denied_sender_test_mode_and_uncertain_response_do_not_write(self):
        await self.process(self.message(msg='提交运维单41356', fromid='unlisted_sender'))
        self.assertEqual([], self.calls)
        task = next(task for task in self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)['ai_tasks'] if task.get('mcp_tool_name') == SUBMISSION_TOOL)
        reply = await self.main._mcp_task_reply({**task, '_source_content': '提交运维单41356'}, ANALYSIS)
        self.assertIn('此为测试，未提交', reply[0])
        self.assertEqual([], self.calls)
        self.error = True
        await self.process(self.message(msg='提交运维单41356'))
        self.assertEqual([SUBMISSION_TOOL], [call['tool_name'] for call in self.calls])
        self.assertIn('结果待核对', self.send.call_args.args[3])
        self.assertIn('提交响应超时', self.send.call_args.args[3])

    async def test_confidence_and_identifier_mismatch_stop_submission(self):
        self.submission_analysis = {**ANALYSIS, 'confidence': 60}
        await self.process(self.message(msg='提交运维单41356'))
        self.assertEqual([], self.calls)
        self.submission_analysis = {**ANALYSIS, 'result': '{"action":"submit","identifier":"41357"}'}
        await self.process(self.message(id='101', msg='提交运维单41356'))
        self.assertEqual([], self.calls)
        self.assertIn('单号与原消息不一致', self.send.call_args.args[3])

    async def test_configure_submission_agent_is_idempotent_and_preserves_senders(self):
        scripts = Path(__file__).resolve().parents[2] / 'scripts'
        sys.path.insert(0, str(scripts))
        try:
            from configure_domp_skill import configure
            for _ in range(2):
                configure(self.owner, self.chat, self.cache.db_path, add_submission_agent=True)
        finally:
            sys.path.remove(str(scripts))
        row = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)
        self.assertEqual({'text': [self.sender], 'file': [self.sender]}, row['target_senders_by_type'])
        self.assertEqual(1, sum(task.get('mcp_tool_name') == SUBMISSION_TOOL for task in row['ai_tasks']))


if __name__ == '__main__':
    unittest.main()
