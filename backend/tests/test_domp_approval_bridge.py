from pathlib import Path
import sys
import unittest

from domp_approval_bridge import (APPROVAL_PROMPT, APPROVAL_REPLY_PREFIX, APPROVAL_TOOL,
    approval_candidate, approval_reply_chunks, approval_target, approve_target)
from domp_execution_bridge import EXECUTION_TOOL
from domp_query_bridge import QUERY_TOOL, query_memory_turn, query_reply
from mcp_service import McpServiceError
import test_domp_query_bridge as fixtures

ITEM = {"work_id": "5400000000383941", "work_num": "5400000000383940", "work_name": "41366",
        "identifier": "41366", "work_status": "0", "status": "待审批"}
PENDING = {"query_type": "pending_approvals", "count": 1, "items": [ITEM]}
APPROVE = {"matched": True, "confidence": 98, "result": '{"action":"approve","identifier":"41366"}'}
APPROVE_CONTEXT = {"matched": True, "confidence": 98, "result": '{"action":"approve_execute","identifier":""}'}
EXECUTION = {"status": "执行成功", "complete": True, "affected_rows_total": 3, "exceptions": [],
             "details": [{"status": "执行成功", "affected_rows": 3, "elapsed_seconds": 0.02, "exception": ""}]}


def approval_history():
    return [query_memory_turn("查一下待审核的运维单",
        {"query_type": "pending_approvals", "identifier": ""}, query_reply(PENDING),
        connection_id="local_mcp", payload=PENDING)]


class ApprovalBridgeTests(unittest.IsolatedAsyncioTestCase):
    def test_natural_approval_phrases_and_query_guards(self):
        for source in ("审核一下 41366 运维单", "帮我审批数据运维单41366", "把这张运维单审核通过",
                       "审核和执行一下运维单", "审核并执行 41366", "把这张单审批后执行"):
            self.assertTrue(approval_candidate(source), source)
        for source in ("查一下今天待审核的运维单", "41366审核了吗", "审核结果如何", "不要审核41366",
                       "明天审核41366", "例如审核一下41366", "都审核执行一下", "全部审批并执行"):
            self.assertFalse(approval_candidate(source), source)

    def test_explicit_and_context_targets_are_program_verified(self):
        action, target = approval_target("审核一下 41366 运维单", APPROVE, [])
        self.assertEqual("approve", action)
        self.assertEqual("41366", target["identifier"])
        action, target = approval_target("审核和执行一下运维单", APPROVE_CONTEXT, approval_history())
        self.assertEqual(("approve_execute", ITEM["work_num"]), (action, target["work_num"]))
        with self.assertRaisesRegex(ValueError, "不得从历史"):
            approval_target("审核和执行一下运维单",
                {**APPROVE_CONTEXT, "result": '{"action":"approve_execute","identifier":"41366"}'}, approval_history())
        with self.assertRaises(ValueError):
            approval_target("审核一下 41366 和 41367", APPROVE, [])

    async def test_approve_only_and_approve_then_execute(self):
        calls = []
        async def call(name, arguments):
            calls.append((name, arguments))
            if name == QUERY_TOOL:
                return {"structured": PENDING}
            if name == APPROVAL_TOOL:
                return {"structured": {**arguments, "status": "审核通过"}}
            if name == EXECUTION_TOOL:
                return {"structured": EXECUTION}
            raise AssertionError(name)
        reply = await approve_target("approve", {"identifier": "41366", "work_id": "", "work_num": ""}, call)
        self.assertEqual([QUERY_TOOL, APPROVAL_TOOL], [name for name, _ in calls])
        self.assertIn("审核：审核通过", reply)
        self.assertNotIn("执行：", reply)
        calls.clear()
        reply = await approve_target("approve_execute", approval_history()[0]["approval_targets"]["items"][0], call)
        self.assertEqual([QUERY_TOOL, APPROVAL_TOOL, EXECUTION_TOOL], [name for name, _ in calls])
        self.assertIn("执行：执行成功", reply)
        self.assertIn("影响条数合计：3", reply)
        self.assertIn("异常详情：无", reply)

    async def test_stale_dry_run_and_uncertain_approval_do_not_execute(self):
        calls = []
        async def stale(name, _arguments):
            calls.append(name)
            return {"structured": {"query_type": "pending_approvals", "count": 0, "items": []}}
        with self.assertRaises(ValueError):
            await approve_target("approve_execute", {"identifier": "41366", "work_id": "", "work_num": ""}, stale)
        self.assertEqual([QUERY_TOOL], calls)
        calls.clear()
        async def uncertain(name, _arguments):
            calls.append(name)
            if name == QUERY_TOOL:
                return {"structured": PENDING}
            raise McpServiceError("审核响应超时")
        reply = await approve_target("approve_execute", {"identifier": "41366", "work_id": "", "work_num": ""}, uncertain)
        self.assertEqual([QUERY_TOOL, APPROVAL_TOOL], calls)
        self.assertIn("执行：未开始", reply)
        calls.clear()
        reply = await approve_target("approve", {"identifier": "41366", "work_id": "", "work_num": ""}, uncertain, dry_run=True)
        self.assertEqual([QUERY_TOOL], calls)
        self.assertIn("仅查询", reply)
        chunks = approval_reply_chunks(APPROVAL_REPLY_PREFIX + "\n异常：" + "完整异常" * 1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(not approval_candidate(chunk) for chunk in chunks))


class ApprovalGroupTests(unittest.IsolatedAsyncioTestCase):
    process = fixtures.QueryGroupTests.process
    message = fixtures.QueryGroupTests.message

    async def asyncSetUp(self):
        await fixtures.QueryGroupTests.asyncSetUp(self)
        row = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)
        row["ai_tasks"].append({**row["ai_tasks"][0], "id": "approve", "skill_id": "domp_approval_agent_text",
            "name": "数据运维审核 Agent", "instruction": APPROVAL_PROMPT, "mcp_tool_name": APPROVAL_TOOL})
        self.cache.upsert_smart_reply_config(row, owner_wxid=self.owner)
        self.approval_analysis = APPROVE
        async def analyze(content, task):
            self.analyzed_messages.append((content, task.get("mcp_tool_name")))
            return self.approval_analysis if task.get("mcp_tool_name") == APPROVAL_TOOL else self.query_analysis
        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            name = kwargs["tool_name"]
            if name == QUERY_TOOL:
                if kwargs["arguments"].get("query_type") == "pending_executions":
                    return {"structured": {"query_type": "pending_executions", "count": 0, "items": []}}
                return {"structured": self.query_result}
            if name == APPROVAL_TOOL:
                return {"structured": {**kwargs["arguments"], "status": "审核通过"}}
            if name == EXECUTION_TOOL:
                return {"structured": EXECUTION}
            raise AssertionError(name)
        self.main.ai_service.analyze = analyze
        self.main.mcp_service.call_tool = call_tool

    async def test_reported_explicit_approval_routes_only_approval_agent(self):
        self.query_result = PENDING
        for index, sender in enumerate((self.sender, self.owner)):
            await self.process(self.message(id=str(880 + index), msg="审核一下 41366 运维单",
                fromid=sender, isSender=int(sender == self.owner)))
            self.assertIn("审核：审核通过", self.send.call_args.args[3])
            self.assertNotIn("执行：", self.send.call_args.args[3])
            self.assertEqual(APPROVAL_TOOL, self.analyzed_messages[-1][1])
        self.assertEqual([QUERY_TOOL, APPROVAL_TOOL] * 2, [call["tool_name"] for call in self.calls])

    async def test_reported_query_then_context_approval_and_execution(self):
        self.query_analysis = {**fixtures.ANALYZED, "result": '{"query_type":"pending_maintenance","identifier":""}'}
        self.query_result = PENDING
        await self.process(self.message(msg="查一下今天待审核和待执行的运维单"))
        self.approval_analysis = APPROVE_CONTEXT
        await self.process(self.message(id="902", msg="审核和执行一下运维单"))
        self.assertEqual([QUERY_TOOL, QUERY_TOOL, QUERY_TOOL, APPROVAL_TOOL, EXECUTION_TOOL],
            [call["tool_name"] for call in self.calls])
        self.assertIn("审核：审核通过", self.send.call_args.args[3])
        self.assertIn("影响条数合计：3", self.send.call_args.args[3])

    async def test_configure_approval_agent_is_idempotent(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        sys.path.insert(0, str(scripts))
        try:
            from configure_domp_skill import configure
            for _ in range(2):
                configure(self.owner, self.chat, self.cache.db_path, add_approval_agent=True)
        finally:
            sys.path.remove(str(scripts))
        row = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)
        self.assertEqual(1, sum(task.get("mcp_tool_name") == APPROVAL_TOOL for task in row["ai_tasks"]))
        self.assertEqual({"text": [self.sender], "file": [self.sender]}, row["target_senders_by_type"])


if __name__ == "__main__":
    unittest.main()
