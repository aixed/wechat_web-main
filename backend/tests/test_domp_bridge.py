import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from domp_bridge import PROMPT, WORKFLOW_TOOL, workflow_arguments, workflow_reply, strip_chat_mentions
from mcp_service import McpServiceError
from smart_reply import SmartReplyEngine
from sqlite_cache import SqliteMessageCache

SQL = "UPDATE EINP_BASICINFO.ab01 SET aab034='540199' WHERE aab001=540103325631"
SOURCE = "需求 40386\n" + SQL
ANALYSIS = {"matched": True, "confidence": 98, "result": json.dumps({"identifier": "40386", "risk_level": "medium", "risk_notes": [], "sql_purpose": "调整指定单位经办区划"}, ensure_ascii=False)}
PAYLOAD = {"identifier": "40386", "work_num": "num-1", "submission": {"status": "填报成功"}, "approval": {"status": "审核通过"},
           "execution": {"status": "执行成功", "complete": True, "affected_rows_total": 12, "exceptions": [], "details": [
               {"status": "执行成功", "affected_rows": n, "elapsed_seconds": .01, "exception": ""} for n in (1,3,2,6,0)]}}


class BridgeTests(unittest.TestCase):
    def test_confirmed_mentions_at_both_edges_preserve_sql_and_unknown_at_values(self):
        sql = '--41356\n' + SQL + ';\n' + SQL + ';'
        for source in (sql + '\n@Aixed', sql + '\n@Aixed\u2005\n@Agent 12345\n', '@Aixed\u2005 ' + sql, '@Aixed\u2005 ' + sql + '\n@Aixed', '@Agent 12345\u2005 ' + sql):
            self.assertEqual(sql, strip_chat_mentions(source, ['Aixed', 'Agent 12345']))
        for source in (sql + '\n@Unknown', sql + '\n@Aixed123', sql + '\n@Aixed\n' + SQL + ';', sql.replace("'540199'", "'a@Aixed;原值'"), SQL.replace("'540199'", '@Aixed') + ';', SQL.replace('ab01', 'ab01@Aixed') + ';', SQL + " SET note='原文;\n@Aixed"):
            self.assertEqual(source, strip_chat_mentions(source, ['Aixed']))
        self.assertEqual(sql + '\n@Aixed', strip_chat_mentions(sql + '\n@Aixed', []))

    def test_raw_source_and_stable_message_key_cannot_be_changed_by_analysis(self):
        task = {"_source_context": {"owner_wxid": "owner", "chat_id": "room", "sender": "sender", "message_id": "100"}}
        args = workflow_arguments(SOURCE, ANALYSIS, task)
        self.assertEqual(SOURCE, args["sql_text"])
        self.assertFalse(args["dry_run"])
        self.assertEqual(args["request_id"], workflow_arguments(SOURCE, ANALYSIS, task)["request_id"])
        self.assertTrue(workflow_arguments(SOURCE, ANALYSIS, {})["dry_run"])
        for source in [SQL, "40386 40387\n" + SQL]:
            with self.assertRaises(ValueError): workflow_arguments(source, ANALYSIS, task)
        high = {**ANALYSIS, "result": json.dumps({"identifier": "40386", "risk_level": "high", "risk_notes": ["需要人工确认"]})}
        with self.assertRaises(ValueError): workflow_arguments(SOURCE, high, task)

    def test_business_reply_keeps_actual_zero_unknown_and_full_exception(self):
        reply = workflow_reply(PAYLOAD, {"sql_purpose": "区划调整", "risk_level": "medium"})
        self.assertIn("影响条数合计：12", reply)
        self.assertIn("SQL 5：执行成功，影响 0 条", reply)
        failure = {**PAYLOAD, "execution": {"status": "执行失败", "affected_rows_total": None, "exceptions": ["完整错误"],
                   "details": [{"status": "执行失败", "affected_rows": None, "elapsed_seconds": 0, "exception": "数据库错误\n全部原因"}]}}
        reply = workflow_reply(failure, {})
        self.assertIn("影响条数合计：未知", reply)
        self.assertIn("数据库错误\n全部原因", reply)


class GroupWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_mention_metadata_cleans_before_ai_and_mcp_but_txt_is_unchanged(self):
        self.cache.upsert_group_members(self.chat, [{'wxid': self.owner, 'name': 'Aixed'}], owner_wxid=self.owner)
        analyzed = []
        async def analyze(content, task):
            analyzed.append(content)
            return self.analysis
        self.main.ai_service.analyze = analyze
        body = SOURCE + ';'
        msgsource = '<msgsource><atuserlist><![CDATA[' + self.owner + ']]></atuserlist></msgsource>'
        await self.process(self.message(msg='@Aixed\u2005 ' + body + '\n@Aixed', msgsource=msgsource))
        self.assertEqual([body], analyzed)
        self.assertEqual(body, self.calls[0]['arguments']['sql_text'])
        await self.process(self.message(id='101', msg=body + '\n@Unknown', msgsource=msgsource))
        self.assertEqual(body + '\n@Unknown', self.calls[-1]['arguments']['sql_text'])
        task = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)['ai_tasks'][1]
        context = {'owner_wxid': self.owner, 'chat_id': self.chat, 'sender': self.sender, 'message_id': '102', 'message_type': 'file', 'mention_names': ['Aixed']}
        await self.main._evaluate_ai_tasks(body + '\n@Aixed', [task], source_context=context)
        self.assertEqual(body + '\n@Aixed', self.calls[-1]['arguments']['sql_text'])

    async def test_non_sql_chat_does_not_invoke_mcp_or_reply(self):
        self.analysis = {"matched": False, "confidence": 99, "result": "没有 SQL"}
        await self.process(self.message(msg="已经处理，谢谢"))
        self.assertEqual([], self.calls)
        self.send.assert_not_awaited()

    async def asyncSetUp(self):
        import main
        import config
        self.main = main
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.cache = SqliteMessageCache(os.path.join(self.temp.name, "cache.sqlite3"))
        self.calls = []
        self.sender = "allowed_sender"; self.owner = "owner"; self.chat = "room@chatroom"
        self.error = False
        self.analysis = ANALYSIS
        async def analyze(content, task): return self.analysis
        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            if self.error: raise McpServiceError("HTTP response timed out")
            return {"structured": PAYLOAD, "text": "", "raw": {}}
        ai = type("Ai", (), {"configured": True})()
        ai.analyze = analyze
        mcp = type("Mcp", (), {})(); mcp.call_tool = call_tool
        self.send = AsyncMock(return_value={"SendAtMsg": "1"})
        for target, value in [("sqlite_cache", self.cache), ("smart_reply_engine", SmartReplyEngine(cooldown=0)), ("ai_service", ai), ("mcp_service", mcp)]:
            p = patch.object(main, target, value); p.start(); self.addCleanup(p.stop)
        for target in [patch.object(main.wechat_api, "send_at", self.send), patch.object(main.wechat_api, "send_text", AsyncMock(side_effect=AssertionError("must reply to sender"))),
                       patch.object(main.manager, "broadcast", AsyncMock()), patch.object(main, "_broadcast_local_sent_for_agent", AsyncMock()),
                       patch.object(config, "MCP_CONNECTIONS", [{"id": "local_mcp", "url": "http://test/mcp", "enabled": True}])]:
            target.start(); self.addCleanup(target.stop)
        self.cache.upsert_smart_reply_config({"chat_id": self.chat, "enabled": True, "message_types": ["text", "file"], "file_types": ["txt"], "mention_only": False,
            "target_senders_by_type": {"text": [self.sender], "file": [self.sender]}, "rules": [], "ai_tasks": [{"id": x, "name": "SQL", "instruction": PROMPT, "enabled": True,
            "message_type": x, "mcp_enabled": True, "mcp_connection_id": "local_mcp", "mcp_tool_name": WORKFLOW_TOOL, "confidence": 85} for x in ("text", "file")]}, owner_wxid=self.owner)

    async def process(self, msg):
        await self.main._process_smart_reply_message(owner_wxid=self.owner, agent_id="agent", self_wxid=self.owner, chat_id=self.chat, message=msg)

    def message(self, **kwargs):
        return {"id": "100", "msgtype": "1", "msg": SOURCE, "fromid": self.sender, "isSender": 0, "sendorrecv": "2", **kwargs}

    async def test_target_sender_text_runs_full_tool_and_replies_to_only_that_sender(self):
        await self.process(self.message())
        self.assertEqual(WORKFLOW_TOOL, self.calls[0]["tool_name"])
        self.assertFalse(self.calls[0]["arguments"]["dry_run"])
        self.assertEqual(SOURCE, self.calls[0]["arguments"]["sql_text"])
        args = self.send.call_args.args
        self.assertEqual((self.chat, self.sender), args[:2])
        self.assertIn("影响条数合计：12", args[3])

    async def test_unapproved_sender_and_self_never_reach_ai_or_mcp(self):
        await self.process(self.message(fromid="someone_else"))
        await self.process(self.message(fromid=self.owner))
        self.assertEqual([], self.calls); self.send.assert_not_called()

    async def test_utf16_txt_is_read_in_full_and_passed_unchanged(self):
        path = Path(self.temp.name)/"需求40386.txt"; path.write_text(SQL, encoding="utf-16")
        await self.process(self.message(msgtype="49", msg='<msg><appmsg><type>6</type><title>需求40386.txt</title></appmsg></msg>', file_path=str(path)))
        self.assertEqual("文件名：需求40386.txt\n文件内容：\n" + SQL, self.calls[0]["arguments"]["sql_text"])
        self.send.assert_called_once()

    async def test_timeout_reply_never_falls_back_to_unexecuted_ai_success(self):
        self.error = True
        await self.process(self.message())
        reply = self.send.call_args.args[3]
        self.assertIn("结果待核对", reply)
        self.assertNotIn("填报成功", reply)
        self.assertEqual(1, len(self.calls))

    async def test_overlong_txt_is_rejected_without_mcp_and_reply_explains_failure(self):
        path = Path(self.temp.name)/"需求40386.txt"; path.write_text(SQL + " "*21000, encoding="utf-8")
        # Ensure stripping whitespace cannot bring the received content below limit.
        path.write_text(SQL + "\n-- " + "x"*21000, encoding="utf-8")
        await self.process(self.message(msgtype="49", msg='<msg><appmsg><type>6</type><title>需求40386.txt</title></appmsg></msg>', file_path=str(path)))
        self.assertEqual([], self.calls)
        self.assertIn("未填报、审核或执行", self.send.call_args.args[3])

    async def test_skill_test_forces_dry_run_without_production_context(self):
        task = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)["ai_tasks"][0]
        replies = await self.main._mcp_task_reply({**task, "_source_content": SOURCE}, ANALYSIS)
        self.assertTrue(self.calls[0]["arguments"]["dry_run"])
        self.send.assert_not_called()


if __name__ == '__main__': unittest.main()
