"""Owner commands in filehelper and missing WeChat mention metadata regressions."""
import unittest
import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from domp_bridge import WORKFLOW_TOOL
from domp_offline_bridge import OfflineConfirmationStore
from smart_reply import SmartReplyEngine
import test_domp_bridge as base
import test_domp_unified_bridge as unified
import test_domp_offline as offline


class FilehelperWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await unified.UnifiedGroupTests.asyncSetUp(self)
        self.group = self.chat
        self.file_config = {**self.cache.get_smart_reply_config(self.group, owner_wxid=self.owner),
            "chat_id": "filehelper", "chat_name": "文件传输助手", "message_types": ["text"],
            "target_senders_by_type": {"text": [self.owner]}, "rules": []}
        self.file_config["ai_tasks"] = [t for t in self.file_config["ai_tasks"] if t["message_type"] == "text"]
        self.cache.upsert_smart_reply_config(self.file_config, owner_wxid=self.owner)
        self.current_analysis = unified.analysis("sql_workflow", "40386", sql_purpose="修改指定单位区划", risk_level="medium")
        self.file_send = AsyncMock(return_value={"SendTextMsg": "1"})
        p = patch.object(self.main.wechat_api, "send_text", self.file_send)
        p.start(); self.addCleanup(p.stop)

    async def process(self, content, **changes):
        message = {"id": "owner-sql", "msgtype": "1", "msg": content,
            "fromid": self.owner, "toid": "filehelper", "isSender": 1, "sendorrecv": "1", **changes}
        await self.main._process_smart_reply_message(owner_wxid=self.owner, self_wxid=self.owner,
            chat_id="filehelper", agent_id="agent", message=message)

    async def test_table_followup_inherits_scope_and_sends_attachment(self):
        connection = self.file_config["ai_tasks"][0]["mcp_connection_id"]
        self.cache.append_query_turn("filehelper", self.owner, {
            "connection_id": connection, "message_id": "previous-query",
            "message": "统计一下上个月问题平台提了多少单子",
            "arguments": {"query_type": "problem_tickets", "identifier": "", "date_from": "2026-09-01",
                "date_to": "2026-09-30", "date_label": "上月", "incomplete_only": False},
            "answer": "783 条"}, owner_wxid=self.owner)
        report = Path(self.temp.name) / "report.xlsx"
        report.write_bytes(b"report remains available to the asynchronous client")
        called = AsyncMock(return_value={"structured": {"query_type": "problem_tickets", "count": 1, "date_from": "2026-09-01", "date_to": "2026-09-30",
            "items": [{"identifier": "xzsb-41198", "status": "处理中"}], "complete": True}})
        send_file = AsyncMock(return_value={"SendFileMsg_NoSrc": "1"})
        with patch.object(self.main.mcp_service, "call_tool", called), patch.object(self.main.wechat_api, "send_file", send_file), patch.object(
                self.main, "_create_problem_ticket_table", return_value={"filepath": str(report), "filename": "report.xlsx"}) as table:
            await self.process("@空气 整理为表格发我", id="export-followup")
        self.assertEqual("query_operation_tickets", called.call_args.kwargs["tool_name"])
        arguments = called.call_args.kwargs["arguments"]
        self.assertEqual(("2026-09-01", "2026-09-30"), (arguments["date_from"], arguments["date_to"]))
        self.assertTrue(table.call_args.kwargs["force"])
        self.assertTrue(report.exists())
        send_file.assert_awaited_once()
        self.assertEqual("filehelper", send_file.call_args.args[0])
        self.assertEqual("思考中...", self.file_send.await_args_list[0].args[1])
        self.main.ai_service.analyze.assert_not_awaited()
        await self.process("@空气 整理为表格发我", id="export-followup")
        self.assertEqual(1, called.await_count)

    async def test_group_table_followup_uses_sender_history_and_mentions_sender(self):
        connection = self.file_config["ai_tasks"][0]["mcp_connection_id"]
        self.cache.append_query_turn(self.group, self.sender, {
            "connection_id": connection, "message_id": "group-query",
            "arguments": {"query_type": "problem_tickets", "identifier": "", "date_from": "2026-09-01",
                "date_to": "2026-09-30", "date_label": "上月", "incomplete_only": False}}, owner_wxid=self.owner)
        called = AsyncMock(return_value={"structured": {"query_type": "problem_tickets", "count": 1, "date_from": "2026-09-01", "date_to": "2026-09-30",
            "items": [{"identifier": "xzsb-41198", "status": "处理中"}], "complete": True}})
        send_file = AsyncMock(return_value={"SendFileMsg_NoSrc": "1"})
        with patch.object(self.main.mcp_service, "call_tool", called), patch.object(self.main.wechat_api, "send_file", send_file), patch.object(
                self.main, "_create_problem_ticket_table", return_value={"filepath": "test-report.xlsx", "filename": "report.xlsx"}):
            await self.main._process_smart_reply_message(owner_wxid=self.owner, self_wxid=self.owner,
                chat_id=self.group, agent_id="agent", message={"id": "group-export", "msgtype": "1", "msg": "@空气 整理为表格发我", "atuserlist": [self.owner], "fromid": self.sender, "isSender": 0, "sendorrecv": "2"})
        self.assertEqual("query_operation_tickets", called.call_args.kwargs["tool_name"])
        self.assertEqual("2026-09-01", called.call_args.kwargs["arguments"]["date_from"])
        self.assertEqual(self.group, send_file.call_args.args[0])
        self.assertEqual((self.group, self.sender), self.send.await_args_list[0].args[:2])
        self.assertEqual("思考中...", self.send.await_args_list[0].args[3])

    async def test_screenshot_year_commands_send_the_requested_range_table(self):
        connection = self.file_config["ai_tasks"][0]["mcp_connection_id"]
        self.cache.append_query_turn("filehelper", self.owner, {"connection_id": connection,
            "message_id": "old-today", "arguments": {"query_type": "problem_tickets", "date_from": "2026-10-21",
                "date_to": "2026-10-21", "date_label": "今天", "time_range": "today"}}, owner_wxid=self.owner)
        payload = {"query_type": "problem_tickets", "count": 12, "date_from": "2026-01-01", "date_to": "2026-10-21",
            "items": [{"identifier": f"xzsb-{42000+n}", "status": "处理中"} for n in range(12)], "complete": True}
        called = AsyncMock(return_value={"structured": payload})
        send_file = AsyncMock(return_value={"SendFileMsg": "1"})
        sources = ("@空气 统计一下今年以来的运维平台问题数量 发表格给我", "@空气 统计一下今年以来的运维平台单子数量",
            "@空气 统计一下从2026年1月1日到2026年10月21日的运维平台单子数量",
            "@空气 统计一下从2026-01-01 至 2026-10-21的运维平台单子数量")
        reports = []
        try:
            with patch.object(self.main.mcp_service, "call_tool", called), patch.object(self.main.wechat_api, "send_file", send_file), \
                    patch("domp_query_bridge.date", wraps=date) as calendar:
                calendar.today.return_value = date(2026, 10, 21)
                for index, source in enumerate(sources):
                    called.return_value = {"structured": payload if index == 0 else {**payload, "count_only": True, "items": []}}
                    await self.process(source, id="year-command-" + str(index))
                    sent = called.call_args.kwargs
                    self.assertEqual("query_operation_tickets", sent["tool_name"])
                    expected = {"date_from": "2026-01-01", "date_to": "2026-10-21", "incomplete_only": False}
                    if index:
                        expected["count_only"] = True
                    self.assertEqual(expected, sent["arguments"])
                    if index == 0:
                        report = Path(send_file.call_args.args[1])
                        reports.append(report)
                        self.assertTrue(report.exists())
                        self.assertEqual("2026-01-01至2026-10-21_问题平台提单统计.xlsx", report.name)
                    else:
                        self.assertEqual(1, send_file.await_count)
                    self.assertIn("2026-01-01 至 2026-10-21", self.file_send.call_args.args[1])
            self.assertEqual(len(sources), called.await_count)
            self.assertEqual(1, send_file.await_count)
            self.main.ai_service.analyze.assert_not_awaited()
        finally:
            for report in reports:
                report.unlink(missing_ok=True)
                report.parent.rmdir()

    async def test_mismatched_server_range_never_sends_or_remembers_wrong_table(self):
        called = AsyncMock(return_value={"structured": {"query_type": "problem_tickets", "count": 12,
            "date_from": "2026-10-21", "date_to": "2026-10-21", "items": [], "complete": True}})
        send_file = AsyncMock()
        with patch.object(self.main.mcp_service, "call_tool", called), patch.object(self.main.wechat_api, "send_file", send_file), \
                patch.object(self.main, "_create_problem_ticket_table") as table, patch("domp_query_bridge.date", wraps=date) as calendar:
            calendar.today.return_value = date(2026, 10, 21)
            await self.process("@空气 统计一下今年以来的运维平台问题数量 发表格给我", id="wrong-range")
        table.assert_not_called()
        send_file.assert_not_awaited()
        self.assertIn("日期范围与本次请求不一致", self.file_send.call_args.args[1])
        history = self.cache.get_query_conversation("filehelper", self.owner, owner_wxid=self.owner)
        self.assertNotIn("符合条件：12 条", history["turns"][-1]["answer"])

    async def test_owner_air_sql_uses_real_workflow_and_replies_in_filehelper(self):
        await self.process("@空气\n" + base.SOURCE)
        self.assertEqual([WORKFLOW_TOOL], [c["tool_name"] for c in self.calls])
        self.assertEqual(base.SOURCE, self.calls[0]["arguments"]["sql_text"])
        self.assertFalse(self.calls[0]["arguments"]["dry_run"])
        self.assertEqual(2, self.file_send.await_count)
        self.assertEqual("思考中...", self.file_send.await_args_list[0].args[1])
        self.assertEqual("filehelper", self.file_send.call_args.args[0])
        self.assertIn("执行成功", self.file_send.call_args.args[1])
        self.send.assert_not_awaited()

    async def test_non_commands_other_accounts_and_air_inside_sql_never_trigger(self):
        cases = [
            ("午饭吃什么", {}), (base.SOURCE, {}), ("@Aixed\n" + base.SOURCE, {}),
            ("--xzsb-40386\nUPDATE S.T SET name='@空气 foo' WHERE id=1;", {}),
            ("@空气\n" + base.SOURCE, {"fromid": "someone_else", "isSender": 0, "sendorrecv": "2"}),
            ("@空气\n" + base.SOURCE, {"fromid": "someone_else", "isSender": 1, "sendorrecv": "1"}),
        ]
        for index, (content, overrides) in enumerate(cases):
            await self.process(content, id=str(index), **overrides)
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.file_send.assert_not_awaited()

    async def test_disabled_config_or_text_type_never_runs(self):
        for changes in ({"enabled": False}, {"message_types": ["file"]}):
            self.cache.upsert_smart_reply_config({**self.file_config, **changes}, owner_wxid=self.owner)
            await self.process("@空气\n" + base.SOURCE)
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)

    async def test_high_risk_sql_returns_reason_without_business_tool(self):
        self.current_analysis = unified.analysis("sql_workflow", "40386", sql_purpose="修改区划", risk_level="high", risk_notes=["缺少有效 WHERE"])
        await self.process("@空气\n--xzsb-40386\nUPDATE S.T SET v=1;")
        self.assertEqual([], self.calls)
        self.assertIn("风险", self.file_send.call_args.args[1])

    async def test_automatic_reply_echo_with_air_sql_cannot_trigger(self):
        reply = "@空气\n" + base.SOURCE
        self.main.smart_reply_engine.remember_reply(self.owner, "filehelper", reply)
        await self.process(reply)
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)

    async def test_verified_literal_owner_mention_passes_the_group_mention_filter(self):
        row = self.cache.get_smart_reply_config(self.group, owner_wxid=self.owner)
        self.cache.upsert_smart_reply_config({**row, "mention_only": True, "mention_message_types": ["text"]}, owner_wxid=self.owner)
        for index, targets in enumerate((None, ["wrong_member"])):
            msg = {"id": str(index), "msgtype": "1", "msg": base.SOURCE + ";\n\u2005@Aixed",
                "fromid": self.sender, "isSender": 0, "sendorrecv": "2"}
            if targets is not None:
                msg["atuserlist"] = targets
            await self.main._process_smart_reply_message(owner_wxid=self.owner, self_wxid=self.owner,
                chat_id=self.group, agent_id="agent", message=msg)
        self.assertEqual([WORKFLOW_TOOL], [c["tool_name"] for c in self.calls])
        self.assertEqual(base.SOURCE + ";", self.calls[0]["arguments"]["sql_text"])
        self.assertEqual(2, self.send.await_count)
        self.assertEqual("思考中...", self.send.await_args_list[0].args[3])

    async def test_api_configuration_always_binds_filehelper_to_current_owner(self):
        with patch.object(self.main, "_contact_owner_wxid", return_value=self.owner):
            request = self.main.SmartReplyConfigRequest(**{**self.file_config,
                "target_senders_by_type": {"text": ["filehelper", "someone_else"]}})
            row = self.main._normalized_smart_reply_config("filehelper", request)
        self.assertEqual({"text": [self.owner]}, row["target_senders_by_type"])
        self.assertEqual([self.owner], row["target_senders"])


class FilehelperOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await offline.OfflineConversationTests.asyncSetUp(self)
        self.context = {"owner_wxid": "owner", "chat_id": "filehelper", "sender": "owner"}
        row = self.cache.get_smart_reply_config("room@chatroom", owner_wxid="owner")
        self.cache.upsert_smart_reply_config({**row, "chat_id": "filehelper", "target_senders_by_type": {"text": ["owner"]}}, owner_wxid="owner")
        self.file_send = AsyncMock(return_value={"SendTextMsg": "1"})
        p = patch.object(offline.main.wechat_api, "send_text", self.file_send)
        p.start(); self.addCleanup(p.stop)

    async def process(self, text, identifier):
        await offline.main._process_smart_reply_message(owner_wxid="owner", self_wxid="owner", chat_id="filehelper", agent_id="agent",
            message={"id": identifier, "msgtype": "1", "msg": text, "fromid": "owner", "toid": "filehelper", "isSender": 1, "sendorrecv": "1"})

    async def test_offline_city_change_confirmation_and_reply_echo_are_guarded(self):
        await self.process("@空气\n" + offline.SOURCE, "sql")
        pending = OfflineConfirmationStore(self.cache.db_path).get(self.context)
        self.assertEqual("拉萨市", pending["matched_city"])
        request_id = pending["arguments"]["request_id"]
        self.workflow.assert_not_awaited()
        prompt = self.file_send.call_args.args[1]
        await self.process(prompt, "automatic-echo")
        self.assertEqual(2, self.file_send.await_count)
        await self.process("是", "unmentioned")
        self.workflow.assert_not_awaited()
        await self.process("@空气 改为日喀则市", "change-city")
        self.workflow.assert_not_awaited()
        await self.process("@空气 是", "confirmed")
        args = self.workflow.call_args.kwargs["arguments"]
        self.assertEqual("日喀则市", args["city"])
        self.assertEqual(request_id, args["request_id"])
        self.assertEqual(offline.SOURCE, args["sql_text"])
        self.workflow.assert_awaited_once()
        self.send.assert_not_awaited()


class ReplyEchoTests(unittest.TestCase):
    def test_group_echo_matches_send_at_prefix_but_air_command_is_not_stripped(self):
        engine = SmartReplyEngine()
        engine.remember_reply("owner", "room@chatroom", "是否确认？\n回复 @空气 是", now=1)
        for prefix in ("@远方多远\u2005", "@Aixed ", "@名字 带空格\u2005"):
            self.assertTrue(engine.is_reply_echo("owner", "room@chatroom", prefix + "是否确认？\n回复 @空气 是", now=2))
        engine.remember_reply("owner", "room@chatroom", "是", now=1)
        self.assertFalse(engine.is_reply_echo("owner", "room@chatroom", "@空气\u2005是", now=2))

    def test_callback_claim_includes_content_and_is_scoped_and_expires(self):
        engine = SmartReplyEngine(dedup_ttl=10)
        msg = {"id": "cb_same_second", "fromid": "sender", "msg": "@空气 边坝县"}
        self.assertTrue(engine.claim_ai_message("owner", "room", msg, now=1))
        self.assertFalse(engine.claim_ai_message("owner", "room", msg, now=2))
        self.assertTrue(engine.claim_ai_message("owner", "room", {**msg, "msg": "@空气 是"}, now=2))
        self.assertTrue(engine.claim_ai_message("other", "room", msg, now=2))
        self.assertTrue(engine.claim_ai_message("owner", "other", msg, now=2))
        self.assertTrue(engine.claim_ai_message("owner", "room", msg, now=12))

    def test_echoes_are_scoped_to_owner_chat_and_expire(self):
        engine = SmartReplyEngine(dedup_ttl=10, dedup_limit=1)
        engine.remember_reply("owner", "filehelper", "@空气\r\nSQL", now=1)
        self.assertTrue(engine.is_reply_echo("owner", "filehelper", "@空气\nSQL", now=2))
        self.assertFalse(engine.is_reply_echo("other", "filehelper", "@空气\nSQL", now=2))
        self.assertFalse(engine.is_reply_echo("owner", "another_chat", "@空气\nSQL", now=2))
        self.assertFalse(engine.is_reply_echo("owner", "filehelper", "@空气\nSQL", now=12))


if __name__ == "__main__":
    unittest.main()
