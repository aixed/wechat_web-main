import json
import unittest
from unittest.mock import patch

from domp_query_bridge import QUERY_TOOL, TASK_QUERY_TOOL, QUERY_PROMPT, QUERY_REPLY_PREFIX, query_candidate, query_arguments, query_tool_call, query_reply, query_reply_chunks, explicit_pending_analysis, query_memory_turn
from domp_execution_bridge import EXECUTION_TOOL, EXECUTION_PROMPT, execution_candidate
from domp_bridge import WORKFLOW_TOOL
from mcp_service import McpServiceError
import test_domp_bridge as workflow_fixtures

ANALYSIS, PAYLOAD = workflow_fixtures.ANALYSIS, workflow_fixtures.PAYLOAD


QUERY = '帮我查一下 41123执行情况'
ANALYZED = {'matched':True,'confidence':98,'result':json.dumps({'query_type':'execution','identifier':'41123'})}
RESULT = {'query_type':'execution','identifier':'41123','work_num':'num-1','found':True,'execution':PAYLOAD['execution']}
TASK_RESULT = {'query_type':'task_processing','identifier':'xzsb-41198','found':True,'status':'转现场处理','assignee':'徐国发','assignee_org':'西藏公司','history':[{'initiated_at':'2026-9-9 11:27:20','status':'转现场处理','assignee':'徐国发','comment':'请处理','acceptance':'未'}]}


class QueryBridgeTests(unittest.TestCase):
    def test_semantic_gate_accepts_unlisted_typos_without_correcting_identifiers(self):
        for source, kind, identifier in (
            ('带审合的单子有多少', 'pending_approvals', ''),
            ('还有待申批的吗', 'pending_approvals', ''),
            ('有哪些代执形的单子', 'pending_executions', ''),
            ('查41184执形结过', 'execution', '41184'),
            ('查41198任物处里情况', 'task_processing', '41198'),
        ):
            self.assertTrue(query_candidate(source), source)
            analysis = {'result': json.dumps({'query_type': kind, 'identifier': identifier})}
            self.assertEqual({'query_type': kind, 'identifier': identifier}, query_arguments(source, analysis))
        with self.assertRaisesRegex(ValueError, '不会自动纠正单号'):
            query_arguments('查41l84执形结过', {'result': '{"query_type":"execution","identifier":"41184"}'}, [{'arguments': {'query_type': 'execution', 'identifier': '41184'}}])
        self.assertTrue(query_candidate('把这些单子搞一下'))
        with self.assertRaisesRegex(ValueError, '暂时无法确定'):
            query_arguments('把这些单子搞一下', {'result': '{"needs_clarification":true,"identifier":""}'})

    def test_pending_spelling_aliases_and_combined_question(self):
        for source, kind in (
            ('今天有没有代审核和待执行的单子', 'pending_maintenance'),
            ('今天有待审批和代执行的单子吗', 'pending_maintenance'),
            ('现在有没有代审核的单子', 'pending_approvals'),
            ('有哪些代审批需求', 'pending_approvals'),
            ('还有代执行的单子吗', 'pending_executions'),
        ):
            self.assertTrue(query_candidate(source), source)
            self.assertFalse(execution_candidate(source), source)
            analysis = explicit_pending_analysis(source)
            self.assertEqual({'query_type': kind, 'identifier': ''}, query_arguments(source, analysis))
        for source in ('代审核太多了', '帮我审核下，看看待执行单子', '执行一下待执行的单子', QUERY_REPLY_PREFIX + '\n目前待审核需求：0 条；待执行需求：1 条'):
            self.assertIsNone(explicit_pending_analysis(source), source)
        with self.assertRaisesRegex(ValueError, '暂不支持'):
            query_arguments('今天新增的待审核和待执行单子有多少', explicit_pending_analysis('今天新增的待审核和待执行单子有多少'))

    def test_combined_query_keeps_unknown_and_verified_execution_target(self):
        payload = {'query_type': 'pending_maintenance', 'results': [
            {'query_type': 'pending_approvals', 'error': '审核连接超时'},
            {'query_type': 'pending_executions', 'count': 1, 'items': [{'identifier': '41184', 'work_num': '5400000000383893', 'work_id': '5400000000383894'}]},
        ]}
        reply = query_reply(payload)
        self.assertIn('目前待审核需求：未知', reply)
        self.assertIn('审核连接超时', reply)
        self.assertIn('目前待执行需求：1 条', reply)
        self.assertIn('包含以前提交的单据', reply)
        turn = query_memory_turn('今天有没有代审核和待执行的单子', {'query_type': 'pending_maintenance', 'identifier': ''}, reply, payload=payload)
        self.assertEqual(1, turn['execution_targets']['count'])
        from domp_execution_bridge import context_execution_target
        self.assertEqual('5400000000383893', context_execution_target([turn])['work_num'])
        self.assertTrue(all(not query_candidate(chunk) for chunk in query_reply_chunks(reply)))

    def test_execution_detail_phrases_are_readonly_even_without_history(self):
        analysis={**ANALYZED,'result':'{"query_type":"execution","identifier":"41184"}'}
        for source in ('41184 的执行详情发我看看','41184执行详情','把41184的执行明细给我','把41184执行详情给我','41184 SQL脚本发我看看'):
            self.assertTrue(query_candidate(source),source)
            self.assertFalse(execution_candidate(source),source)
            self.assertEqual((QUERY_TOOL,{'query_type':'execution','identifier':'41184'}),query_tool_call(query_arguments(source,analysis)))

    def test_requested_details_preserve_raw_sql_zero_unknown_and_full_chunks(self):
        sql="UPDATE t SET a='原始值' WHERE aac001=123;\n-- "+'原始注释'*600
        payload={'query_type':'execution','identifier':'41184','execution':{'status':'执行失败','complete':True,'affected_rows_total':None,
            'details':[{'sql':sql,'status':'执行成功','affected_rows':0,'elapsed_seconds':0,'exception':''},
                       {'sql':'','status':'执行失败','affected_rows':None,'exception':'完整错误'}],'exceptions':['完整错误']}}
        reply=query_reply(payload,include_sql=True)
        self.assertIn(sql,reply);self.assertIn('影响 0 条，耗时 0 秒',reply)
        self.assertIn('系统未返回 SQL 原文',reply);self.assertIn('影响条数合计：未知',reply)
        chunks=query_reply_chunks(reply)
        self.assertTrue(all(len(c)<=1800 and not query_candidate(c) and not execution_candidate(c) for c in chunks))
        reconstructed=''.join(c.removeprefix(QUERY_REPLY_PREFIX+'\n') for c in chunks)
        self.assertIn(sql,reconstructed);self.assertIn('完整错误',reconstructed)
        self.assertNotIn(sql,query_reply(payload))

    def test_execution_status_question_triggers_readonly_lookup(self):
        analyzed={**ANALYZED,'result':json.dumps({'query_type':'execution','identifier':'41233'})}
        for source in ('41233 执行了吗，结果如何','41233 执行了吗','41233有没有执行','41233执行了没','41233执行成功了吗','41233 是否已经执行'):
            with self.subTest(source=source):
                self.assertTrue(query_candidate(source))
                args=query_arguments(source,analyzed)
                self.assertEqual((QUERY_TOOL,{'query_type':'execution','identifier':'41233'}),query_tool_call(args))
        self.assertFalse(query_candidate('帮我执行41233'))
        self.assertTrue(query_candidate('还有待执行吗'))
        self.assertEqual({'query_type':'pending_executions','identifier':''},query_arguments('还有待执行吗',{'result':json.dumps({'query_type':'pending_executions','identifier':''})}))

    def test_failure_exception_and_missing_exception_are_reported_honestly(self):
        payload={'query_type':'execution','identifier':'41233','execution':{'status':'执行失败','complete':True,'affected_rows_total':None,'details':[{'status':'执行失败','affected_rows':None,'exception':'ORA-00942: table or view does not exist'}],'exceptions':['ORA-00942: table or view does not exist']}}
        reply=query_reply(payload)
        self.assertIn('执行结果：执行失败',reply)
        self.assertIn('影响条数合计：未知',reply)
        self.assertIn('SQL 1 异常详情：ORA-00942: table or view does not exist',reply)
        payload['execution']['details']=[];payload['execution']['exceptions']=[]
        self.assertIn('系统未返回异常文本',query_reply(payload))
        self.assertNotIn('异常详情：无',query_reply(payload))

    def test_task_processing_phrases_route_to_comprehensive_query(self):
        analyzed = {**ANALYZED, 'result':json.dumps({'query_type':'task_processing','identifier':'41198'})}
        for source in ('查一下 41198 处理情况', '查一下 41198 任务处理情况', 'xzsb-41198 谁在处理', '查 41198 受理人', '查41198处理履历'):
            with self.subTest(source=source):
                self.assertTrue(query_candidate(source))
                args = query_arguments(source, analyzed)
                self.assertEqual((TASK_QUERY_TOOL, {'identifier':'41198'}), query_tool_call(args))
        # Old-system wording wins even if the model confuses the two systems.
        args = query_arguments('查一下 41198 处理情况', {**analyzed, 'result':json.dumps({'query_type':'execution','identifier':'41198'})})
        self.assertEqual(TASK_QUERY_TOOL, query_tool_call(args)[0])
        self.assertEqual(QUERY_TOOL, query_tool_call(query_arguments(QUERY, ANALYZED))[0])
        reply=query_reply(TASK_RESULT)
        self.assertIn('当前状态：转现场处理',reply)
        self.assertIn('受理人：徐国发',reply)
        self.assertIn('受理人评论：请处理',reply)
        self.assertIn('不表示任务已解决',reply)
        self.assertIn('受理情况：尚未受理',reply)
        self.assertFalse(query_candidate(reply))
        self.assertNotIn('影响条数',reply)

    def test_natural_query_and_strict_readonly_arguments(self):
        self.assertTrue(query_candidate(QUERY))
        self.assertTrue(query_candidate('目前有所少待审核的需求'))
        self.assertFalse(query_candidate('帮我执行41123'))
        self.assertFalse(query_candidate('UPDATE x.t SET x=1 WHERE aab001=1'))
        self.assertEqual({'query_type':'execution','identifier':'41123'},query_arguments(QUERY,ANALYZED))
        for source,analysis in [('查41123和41124执行情况',ANALYZED),(QUERY,{**ANALYZED,'result':json.dumps({'query_type':'execute','identifier':'41123'})}),(QUERY,{**ANALYZED,'result':json.dumps({'query_type':'execution','identifier':'41124'})})]:
            with self.subTest(source=source),self.assertRaises(ValueError):query_arguments(source,analysis)
        args=query_arguments('现在有哪些待审批需求',{'result':json.dumps({'query_type':'pending_approvals','identifier':''})})
        self.assertEqual('pending_approvals',args['query_type'])

    def test_zero_unknown_missing_duplicate_full_exception_and_chunk_loop_guard(self):
        self.assertIn('影响条数合计：12',query_reply(RESULT))
        self.assertIn('影响 0 条',query_reply(RESULT))
        self.assertIn('目前待审核需求：0 条',query_reply({'query_type':'pending_approvals','count':0}))
        reply=query_reply({'query_type':'execution','execution':{'status':'执行失败','affected_rows_total':None,'details':[],'exceptions':['全部异常' * 800]}})
        self.assertIn('影响条数合计：未知',reply)
        chunks=query_reply_chunks(reply)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(x)<=1800 and not query_candidate(x) for x in chunks))
        reconstructed=''.join(x.removeprefix(QUERY_REPLY_PREFIX+'\n') for x in chunks)
        self.assertIn('全部异常'*800,reconstructed)
        self.assertIn('当前账号未找到',query_reply({'query_type':'execution','status':'未找到','message':'当前账号未找到'}))
        self.assertIn('工作编号：two',query_reply({'query_type':'execution','status':'需要选择','matches':[{'work_num':'two'}]}))


class QueryGroupTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only the existing isolated fake-cache setup, not its test methods.
    process=workflow_fixtures.GroupWorkflowTests.process
    message=workflow_fixtures.GroupWorkflowTests.message

    async def asyncSetUp(self):
        await workflow_fixtures.GroupWorkflowTests.asyncSetUp(self)
        self.query_analysis=ANALYZED
        self.query_result=RESULT
        self.analyzed_messages=[]
        row=self.cache.get_smart_reply_config(self.chat,owner_wxid=self.owner)
        row['ai_tasks'].append({**row['ai_tasks'][0], 'id':'query','name':'数据运维查询 Agent','instruction':QUERY_PROMPT,'mcp_tool_name':QUERY_TOOL})
        self.cache.upsert_smart_reply_config(row,owner_wxid=self.owner)
        async def analyze(content,task):
            if task.get('mcp_tool_name')==QUERY_TOOL:
                self.analyzed_messages.append(content)
                return self.query_analysis
            return ANALYSIS if not query_candidate(content) else {'matched':False,'confidence':99,'result':''}
        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            if self.error:raise McpServiceError('查询连接超时')
            return {'structured':self.query_result if kwargs['tool_name'] in {QUERY_TOOL, TASK_QUERY_TOOL} else PAYLOAD}
        self.main.ai_service.analyze=analyze
        self.main.mcp_service.call_tool=call_tool

    async def test_unlisted_typo_reaches_semantic_agent_and_ambiguous_intent_never_calls_tool(self):
        self.query_analysis = {**ANALYZED, 'result': '{"query_type":"pending_executions","identifier":""}'}
        self.query_result = {'query_type': 'pending_executions', 'count': 0, 'items': []}
        await self.process(self.message(msg='有哪些代执形的单子'))
        self.assertEqual('有哪些代执形的单子', self.analyzed_messages[-1])
        self.assertEqual({'query_type': 'pending_executions', 'identifier': ''}, self.calls[-1]['arguments'])
        self.query_analysis = {**ANALYZED, 'confidence': 100, 'result': '{"needs_clarification":true,"identifier":""}'}
        await self.process(self.message(id='101', msg='把这些单子搞一下'))
        self.assertIn('暂时无法确定', self.send.call_args.args[3])
        self.assertEqual(1, len(self.calls))
        self.query_analysis = {'matched': False, 'confidence': 99, 'result': '', 'reply': ''}
        await self.process(self.message(id='103', msg='把这些单子搞一下'))
        self.assertIn('暂时无法确定', self.send.call_args.args[3])
        self.assertEqual(1, len(self.calls))
        before = self.send.call_count
        await self.process(self.message(id='102', msg='今天有没有新人'))
        self.assertEqual(1, len(self.calls))
        self.assertEqual(before, self.send.call_count)

    async def test_combined_typo_query_reads_both_categories_for_sender_and_owner(self):
        row = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)
        row['ai_tasks'].append({**row['ai_tasks'][0], 'id': 'execute', 'instruction': EXECUTION_PROMPT, 'mcp_tool_name': EXECUTION_TOOL})
        self.cache.upsert_smart_reply_config(row, owner_wxid=self.owner)
        self.query_analysis = {'matched': False, 'confidence': 99, 'result': ''}
        async def read(**kwargs):
            self.calls.append(kwargs)
            kind = kwargs['arguments']['query_type']
            return {'structured': {'query_type': kind, 'count': 0 if kind == 'pending_approvals' else 1,
                'items': [] if kind == 'pending_approvals' else [{'identifier': '41184', 'work_num': '5400000000383893', 'work_id': '5400000000383894'}]}}
        self.main.mcp_service.call_tool = read
        source = '今天有没有代审核和待执行的单子'
        for index, sender in enumerate((self.sender, self.owner)):
            await self.process(self.message(id=str(400 + index), msg=source, fromid=sender, isSender=int(sender == self.owner)))
            self.assertEqual((self.chat, sender), self.send.call_args.args[:2])
            self.assertIn('目前待审核需求：0 条', self.send.call_args.args[3])
            self.assertIn('目前待执行需求：1 条', self.send.call_args.args[3])
            self.assertIn('41184', self.send.call_args.args[3])
        self.assertEqual([QUERY_TOOL] * 4, [call['tool_name'] for call in self.calls])
        self.assertEqual(['pending_approvals', 'pending_executions'] * 2, [call['arguments']['query_type'] for call in self.calls])
        self.assertEqual([], self.analyzed_messages)
        self.assertEqual('pending_maintenance', self.cache.get_query_conversation(self.chat, self.sender, owner_wxid=self.owner)['turns'][-1]['arguments']['query_type'])

    async def test_combined_query_keeps_first_result_when_second_connection_fails(self):
        async def read(**kwargs):
            self.calls.append(kwargs)
            if kwargs['arguments']['query_type'] == 'pending_executions':
                raise McpServiceError('执行查询超时')
            return {'structured': {'query_type': 'pending_approvals', 'count': 2, 'items': []}}
        self.main.mcp_service.call_tool = read
        await self.process(self.message(msg='今天有没有代审核和待执行的单子'))
        reply = self.send.call_args.args[3]
        self.assertIn('目前待审核需求：2 条', reply)
        self.assertIn('目前待执行需求：未知', reply)
        self.assertIn('执行查询超时', reply)
        self.assertNotIn('目前待执行需求：0 条', reply)
        self.assertEqual([QUERY_TOOL, QUERY_TOOL], [call['tool_name'] for call in self.calls])

    async def test_allowed_sender_query_calls_only_read_tool_and_mentions_sender(self):
        await self.process(self.message(msg=QUERY))
        self.assertEqual([QUERY_TOOL],[c['tool_name'] for c in self.calls])
        self.assertEqual({'query_type':'execution','identifier':'41123'},self.calls[0]['arguments'])
        self.assertEqual((self.chat,self.sender),self.send.call_args.args[:2])
        self.assertIn('影响条数合计：12',self.send.call_args.args[3])

    async def test_owner_details_reply_includes_sql_but_history_keeps_summary(self):
        row=self.cache.get_smart_reply_config(self.chat,owner_wxid=self.owner)
        row['ai_tasks'].append({**row['ai_tasks'][0],'id':'execute','instruction':EXECUTION_PROMPT,'mcp_tool_name':EXECUTION_TOOL})
        self.cache.upsert_smart_reply_config(row,owner_wxid=self.owner)
        sql="UPDATE EINP_BASICINFO.AC01 SET aac007=20140101 WHERE aac001=54990010018312"
        self.query_analysis={**ANALYZED,'result':'{"query_type":"execution","identifier":"41184"}'}
        self.query_result={'query_type':'execution','identifier':'41184','work_num':'5400000000383893','execution':{'status':'执行成功','complete':True,'affected_rows_total':1,
            'details':[{'sql':sql,'status':'执行成功','affected_rows':1,'elapsed_seconds':'.08','exception':''}],'exceptions':[]}}
        for index,source in enumerate(('41184 的执行详情发我看看','把41184执行详情给我','41184 SQL脚本发我看看','详情呢')):
            await self.process(self.message(id=str(300+index),msg=source,fromid=self.owner,isSender=1,sendorrecv='1'))
            self.assertIn(sql,self.send.call_args.args[3])
        self.assertEqual([QUERY_TOOL]*4,[c['tool_name'] for c in self.calls])
        self.assertEqual((self.chat,self.owner),self.send.call_args.args[:2])
        history=self.cache.get_query_conversation(self.chat,self.owner,owner_wxid=self.owner)['turns']
        self.assertNotIn(sql,json.dumps(history,ensure_ascii=False))
        self.assertIn('影响条数合计：1',history[-1]['answer'])

    async def test_exact_execution_question_works_for_owner_and_reports_zero(self):
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'execution','identifier':'41233'})}
        self.query_result={'query_type':'execution','identifier':'41233','found':True,'work_num':'num-41233','execution':{'status':'执行成功','complete':True,'affected_rows_total':0,'details':[{'status':'执行成功','affected_rows':0,'elapsed_seconds':'0.01','exception':''}],'exceptions':[]}}
        for index,source in enumerate(('41233 执行了吗，结果如何','41233 执行了吗')):
            await self.process(self.message(id=str(200+index),msg=source,fromid=self.owner,isSender=1,sendorrecv='1'))
        self.assertEqual([QUERY_TOOL,QUERY_TOOL],[c['tool_name'] for c in self.calls])
        self.assertTrue(all(c['arguments']=={'query_type':'execution','identifier':'41233'} for c in self.calls))
        self.assertIn('执行结果：执行成功',self.send.call_args.args[3])
        self.assertIn('影响条数合计：0',self.send.call_args.args[3])
        self.assertIn('异常详情：无',self.send.call_args.args[3])

    async def test_owner_both_reported_phrases_call_only_comprehensive_query(self):
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'task_processing','identifier':'41198'})}
        self.query_result=TASK_RESULT
        for index,source in enumerate(('查一下 41198 处理情况','查一下 41198 任务处理情况')):
            await self.process(self.message(id=str(200+index),msg=source,fromid=self.owner,isSender=1,sendorrecv='1'))
        self.assertEqual([TASK_QUERY_TOOL,TASK_QUERY_TOOL],[c['tool_name'] for c in self.calls])
        self.assertTrue(all(c['arguments']=={'identifier':'41198'} for c in self.calls))
        self.assertEqual(2,self.send.call_count)
        self.assertEqual((self.chat,self.owner),self.send.call_args.args[:2])
        self.assertIn('受理人评论：请处理',self.send.call_args.args[3])
        await self.process(self.message(id='202',msg=self.send.call_args.args[3],fromid=self.owner,isSender=1,sendorrecv='1'))
        self.assertEqual(2,len(self.calls))

    async def test_owner_can_query_but_own_sql_and_bot_reply_do_not_trigger(self):
        await self.process(self.message(msg=QUERY,fromid=self.owner,isSender=1,sendorrecv='1'))
        self.assertEqual(QUERY_TOOL,self.calls[0]['tool_name'])
        reply=self.send.call_args.args[3]
        await self.process(self.message(id='101',msg=reply,fromid=self.owner,isSender=1,sendorrecv='1'))
        await self.process(self.message(id='102',fromid=self.owner,isSender=1,sendorrecv='1'))
        self.assertEqual(1,len(self.calls)); self.send.assert_called_once()

    async def test_original_sql_stays_on_workflow_and_unlisted_sender_cannot_query(self):
        await self.process(self.message())
        self.assertEqual([WORKFLOW_TOOL],[c['tool_name'] for c in self.calls])
        await self.process(self.message(id='101',msg=QUERY,fromid='not_allowed'))
        self.assertEqual(1,len(self.calls))

    async def test_pending_count_missing_identifier_and_read_failure(self):
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'pending_approvals','identifier':''})}
        self.query_result={'query_type':'pending_approvals','count':0,'items':[]}
        await self.process(self.message(msg='目前有所少待审核的需求'))
        self.assertIn('目前待审核需求：0 条',self.send.call_args.args[3])
        self.query_analysis=ANALYZED
        await self.process(self.message(id='101',msg='查一下执行情况'))
        self.assertEqual(1,len(self.calls))
        self.error=True
        await self.process(self.message(id='102',msg=QUERY))
        self.assertIn('查询失败',self.send.call_args.args[3])
        self.assertNotIn('执行成功',self.send.call_args.args[3])

    async def test_pending_execution_followup_has_history_and_refreshes_mcp(self):
        self.main.smart_reply_engine.cooldown=30
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'pending_approvals','identifier':''})}
        self.query_result={'query_type':'pending_approvals','count':0,'items':[]}
        await self.process(self.message(msg='有哪些待审批',fromid=self.owner,isSender=1))
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'pending_executions','identifier':''})}
        self.query_result={'query_type':'pending_executions','count':2,'items':[]}
        await self.process(self.message(id='101',msg='待执行呢',fromid=self.owner,isSender=1))
        self.assertEqual(['pending_approvals','pending_executions'],[c['arguments']['query_type'] for c in self.calls])
        context=json.loads(self.analyzed_messages[-1])
        self.assertEqual('待执行呢',context['current_message'])
        self.assertEqual('pending_approvals',context['query_history'][-1]['arguments']['query_type'])
        self.assertIn('目前待执行需求：2 条',self.send.call_args.args[3])
        self.assertEqual(2,self.send.call_count)

    async def test_sender_history_is_not_shared_and_new_identifier_wins(self):
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'task_processing','identifier':'41198'})}
        self.query_result=TASK_RESULT
        await self.process(self.message(msg='查41198任务处理情况',fromid=self.owner,isSender=1))
        await self.process(self.message(id='101',msg='那它呢'))
        self.assertEqual(1,len(self.calls))
        self.assertIn('没有可承接',self.send.call_args.args[3])
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'execution','identifier':'41199'})}
        await self.process(self.message(id='102',msg='那41199呢',fromid=self.owner,isSender=1))
        self.assertEqual(TASK_QUERY_TOOL,self.calls[-1]['tool_name'])
        self.assertEqual({'identifier':'41199'},self.calls[-1]['arguments'])

    async def test_expired_followup_does_not_inherit_ticket_and_new_query_starts_session(self):
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'task_processing','identifier':'41198'})}
        self.query_result=TASK_RESULT
        await self.process(self.message(msg='查41198任务处理情况',fromid=self.owner,isSender=1))
        first=self.cache.get_query_conversation(self.chat,self.owner,owner_wxid=self.owner)
        with patch('sqlite_cache.time.time',return_value=first['created_at']+7*86400):
            await self.process(self.message(id='101',msg='那它呢',fromid=self.owner,isSender=1))
            self.assertEqual(1,len(self.calls))
            self.assertIn('没有可承接',self.send.call_args.args[3])
            await self.process(self.message(id='102',msg='查41198任务处理情况',fromid=self.owner,isSender=1))
            second=self.cache.get_query_conversation(self.chat,self.owner,owner_wxid=self.owner)
        self.assertNotEqual(first['session_id'],second['session_id'])
        self.assertEqual(1,len(second['turns']))

    async def test_sql_workflow_result_supplies_query_target_without_reusing_sql(self):
        await self.process(self.message())
        self.query_analysis={**ANALYZED,'result':json.dumps({'query_type':'execution','identifier':'40386'})}
        await self.process(self.message(id='101',msg='它的执行结果呢'))
        self.assertEqual([WORKFLOW_TOOL,QUERY_TOOL],[c['tool_name'] for c in self.calls])
        self.assertEqual({'query_type':'execution','identifier':'40386'},self.calls[-1]['arguments'])
        context=json.loads(self.analyzed_messages[-1])
        self.assertNotIn('UPDATE',json.dumps(context['query_history']))


if __name__=='__main__':unittest.main()
