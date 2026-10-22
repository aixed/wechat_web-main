import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from mcp_service import McpService
from query_progress import progress_text
from domp_query_bridge import query_arguments, query_tool_call
import test_domp_query_bridge as fixtures


class QueryCountTests(unittest.TestCase):
    def test_count_only_is_selected_from_the_current_message_and_export_restores_details(self):
        analyzed = {"result": {"query_type": "problem_tickets", "identifier": ""}}
        source = "统计一下从2026-01-01 至 2026-10-21的运维平台单子数量"
        args = query_arguments(source, analyzed)
        self.assertTrue(args["count_only"])
        self.assertTrue(query_tool_call(args)[1]["count_only"])
        history = [{"arguments": args}]
        export = query_arguments("整理为表格发我", analyzed, history)
        self.assertNotIn("count_only", query_tool_call(export)[1])
        self.assertEqual((args["date_from"], args["date_to"]), (export["date_from"], export["date_to"]))
        for source in ("统计今年运维单数量 发表格给我", "查今年运维单明细", "统计今年运维单数量并列出处理时间"):
            self.assertFalse(query_arguments(source, analyzed).get("count_only", False))

    def test_progress_distinguishes_queued_running_stalled_and_unavailable(self):
        self.assertIn("排队", progress_text({"phase": "queued"}, 60))
        text = progress_text({"phase": "history", "history_done": 13, "history_total": 100,
            "source_total": 100, "scanned_count": 100, "idle_seconds": 120}, 180)
        self.assertIn("13 / 100", text)
        self.assertIn("120 秒没有新增结果", text)
        self.assertNotIn("执行成功", text)
        self.assertIn("无法取得服务进度", progress_text({"phase": "unavailable"}, 60))


class McpProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_polls_real_progress_without_reissuing_query_even_after_poll_failure(self):
        release = asyncio.Event()
        queries, polls, updates = [], [], []
        async def handler(request):
            body = json.loads(request.content)
            result = {}
            if body.get("method") == "tools/call":
                name, args = body["params"]["name"], body["params"]["arguments"]
                if name == "query_operation_tickets":
                    queries.append(args)
                    await release.wait()
                    result = {"structuredContent": {"query_type": "problem_tickets", "count": 8271, "count_only": True}}
                else:
                    self.assertEqual("get_operation_ticket_query_progress", name)
                    polls.append(args)
                    if len(polls) == 1:
                        return httpx.Response(503, text="temporarily unavailable")
                    result = {"structuredContent": {"query_id": args["query_id"], "phase": "list", "scanned_count": 50}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result})
        service = McpService(timeout_seconds=5)
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service.progress_poll_seconds = .001
        def update(snapshot):
            updates.append(snapshot)
            if len(updates) == 2:
                release.set()
        original = {"count_only": True}
        try:
            result = await service.call_tool_with_progress(url="http://test/mcp", tool_name="query_operation_tickets",
                arguments=original, progress_callback=update)
        finally:
            await service.close()
        self.assertEqual(1, len(queries))
        self.assertEqual(2, len(polls))
        self.assertEqual(queries[0]["query_id"], polls[0]["query_id"])
        self.assertEqual("unavailable", updates[0]["phase"])
        self.assertEqual(50, updates[1]["scanned_count"])
        self.assertEqual(8271, result["structured"]["count"])
        self.assertEqual({"count_only": True}, original)

    async def test_cancellation_stops_the_pending_http_request_and_polling(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(request):
            body = json.loads(request.content)
            if body.get("method") == "tools/call":
                if body["params"]["name"] == "query_operation_tickets":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        service = McpService(timeout_seconds=5)
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        pending = asyncio.create_task(service.call_tool_with_progress(url="http://test/mcp",
            tool_name="query_operation_tickets", arguments={}, progress_callback=lambda _: None))
        try:
            await asyncio.wait_for(started.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(cancelled.is_set())
        finally:
            await service.close()


class QueryHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.QueryGroupTests.asyncSetUp
    process = fixtures.QueryGroupTests.process
    message = fixtures.QueryGroupTests.message

    async def _run_slow_query(self, *, fail=False, source=None, count_only=True):
        release, started = asyncio.Event(), asyncio.Event()
        requests = []
        async def query(**kwargs):
            requests.append(kwargs)
            kwargs["progress_callback"]({"phase": "history", "history_done": 17, "history_total": 8271,
                "source_total": 8271, "scanned_count": 8271, "matched_count": 8271})
            started.set()
            await release.wait()
            if fail:
                raise fixtures.McpServiceError("平台响应超时")
            return {"structured": {"query_type": "problem_tickets", "date_from": "2026-01-01", "date_to": "2026-10-21",
                "count": 8271, "count_only": count_only, "items": []}}
        with patch.object(self.main, "QUERY_PROGRESS_INTERVAL_SECONDS", .01), patch.object(
                self.main.mcp_service, "call_tool_with_progress", query, create=True), patch.object(
                self.main, "_create_problem_ticket_table", return_value=None):
            pending = asyncio.create_task(self.process(self.message(msg=source or "统计一下从2026-01-01 至 2026-10-21的运维平台单子数量")))
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.sleep(.035)
                heartbeat = [call.args[3] for call in self.send.await_args_list if "查询处理进度" in call.args[3]]
                self.assertGreaterEqual(len(heartbeat), 2)
                self.assertTrue(all("17 / 8271" in text for text in heartbeat))
                self.assertTrue(all(call.args[:2] == (self.chat, self.sender) for call in self.send.await_args_list))
                release.set()
                await pending
                count = self.send.await_count
                await asyncio.sleep(.025)
                self.assertEqual(count, self.send.await_count)
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(1, len(requests))
        self.assertEqual(count_only, requests[0]["arguments"].get("count_only", False))
        return self.send.call_args.args[3]

    async def test_repeats_real_progress_and_stops_after_the_final_count(self):
        self.assertEqual(60.0, self.main.QUERY_PROGRESS_INTERVAL_SECONDS)
        reply = await self._run_slow_query()
        self.assertIn("符合条件：8271 条", reply)
        self.assertNotIn("表格文件发送", reply)

    async def test_failed_query_reports_failure_and_stops_heartbeat(self):
        reply = await self._run_slow_query(fail=True)
        self.assertIn("查询失败", reply)
        self.assertIn("平台响应超时", reply)

    async def test_mentioned_export_followup_inherits_scope_and_keeps_heartbeat(self):
        self.cache.append_query_turn(self.chat, self.sender, {"connection_id": "local_mcp",
            "arguments": {"query_type": "problem_tickets", "identifier": "", "date_from": "2026-01-01",
                "date_to": "2026-10-21", "date_label": "指定日期", "incomplete_only": False, "count_only": True}}, owner_wxid=self.owner)
        reply = await self._run_slow_query(source="整理为表格发我", count_only=False)
        self.assertIn("符合条件：8271 条", reply)
