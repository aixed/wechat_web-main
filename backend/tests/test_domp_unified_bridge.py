import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

from domp_approval_bridge import APPROVAL_TOOL
from domp_bridge import WORKFLOW_TOOL
from domp_execution_bridge import EXECUTION_TOOL
from domp_query_bridge import QUERY_TOOL
from domp_submission_bridge import SUBMISSION_TOOL
from domp_unified_bridge import (UNIFIED_PROMPT, UNIFIED_SKILL_ID, UNIFIED_SKILL_NAME,
    is_unified_task, submission_workflow_identifier, unified_candidate, unified_route)
import test_domp_bridge as fixtures


def analysis(intent, identifier="", **values):
    result = {"intent": intent, "identifier": identifier, "query_type": "",
              "sql_purpose": "", "risk_level": "", "risk_notes": [],
              "needs_clarification": False, **values}
    return {"matched": True, "confidence": 98, "result": json.dumps(result, ensure_ascii=False),
            "items": [], "reply": ""}


class UnifiedBridgeTests(unittest.TestCase):
    def test_one_skill_accepts_supported_phrases_and_rejects_unrelated_chat(self):
        for source in ("提交运维单 41356", "提交填报运维单 41323,并审核执行", "查一下 41123 执行情况", "今天有没有代审核和待执行的单子",
                       "审核一下 41366 运维单", "能不能帮我审核 41366 运维单", "兄弟，帮我执行下 41184",
                       "能不能帮我执行 41184", "都审核执行一下", "把 41323 运维单搞一下"):
            self.assertTrue(unified_candidate(source, []), source)
        self.assertFalse(unified_candidate("午饭吃什么", []))
        self.assertFalse(unified_candidate("‘执行 41184’这句话能识别吗", []))
        self.assertTrue(unified_candidate(fixtures.SOURCE, [], message_type="file"))
        self.assertFalse(unified_candidate("普通附件", [], message_type="file"))

    def test_combined_submission_requires_one_explicit_id_and_all_three_actions(self):
        self.assertEqual("41323", submission_workflow_identifier("提交填报运维单 41323,并审核执行"))
        self.assertEqual("41323", submission_workflow_identifier("能不能帮我提交填报运维单41323并审核执行"))
        for source in ("提交运维单 41323", "审核并执行 41323", "不要提交运维单 41323 并审核执行",
                       "提交运维单 41323 和 41324 并审核执行", "明天提交运维单 41323 并审核执行",
                       "41323 能不能提交并审核执行", "‘提交填报运维单41323并审核执行’这句话能识别吗"):
            self.assertEqual("", submission_workflow_identifier(source), source)

    def test_prompt_distinguishes_polite_requests_from_capability_and_meta_questions(self):
        self.assertIn("不按单个词机械拦截", UNIFIED_PROMPT)
        self.assertIn("能不能帮我执行41323", UNIFIED_PROMPT)
        self.assertIn("修改 Prompt", UNIFIED_PROMPT)

    def test_intents_map_only_to_existing_protected_routes(self):
        cases = (
            ("提交运维单 41356", analysis("submit", "41356"), SUBMISSION_TOOL, "submit"),
            ("查一下 41123 执行情况", analysis("query", "41123", query_type="execution"), QUERY_TOOL, "execution"),
            ("审核一下 41366 运维单", analysis("approve", "41366"), APPROVAL_TOOL, "approve"),
            ("审核并执行 41366", analysis("approve_execute", "41366"), APPROVAL_TOOL, "approve_execute"),
            ("执行 41184", analysis("execute", "41184"), EXECUTION_TOOL, "execute"),
            ("都审核执行一下", analysis("approve_execute_all"), EXECUTION_TOOL, "approve_execute_all"),
            (fixtures.SOURCE, analysis("sql_workflow", "40386", sql_purpose="调整区划", risk_level="medium"), WORKFLOW_TOOL, None),
        )
        for source, value, tool, action in cases:
            with self.subTest(source=source):
                actual_tool, routed = unified_route(source, value)
                self.assertEqual(tool, actual_tool)
                parsed = json.loads(routed["result"])
                if action in {"execution"}:
                    self.assertEqual(action, parsed["query_type"])
                elif action:
                    self.assertEqual(action, parsed["action"])
        with self.assertRaisesRegex(ValueError, "包含 SQL"):
            unified_route(fixtures.SOURCE, analysis("query", "40386", query_type="execution"))
        with self.assertRaisesRegex(ValueError, "没有识别到 SQL"):
            unified_route("查询 41184", analysis("sql_workflow", "41184"))


class UnifiedGroupTests(unittest.IsolatedAsyncioTestCase):
    process = fixtures.GroupWorkflowTests.process
    message = fixtures.GroupWorkflowTests.message

    async def asyncSetUp(self):
        await fixtures.GroupWorkflowTests.asyncSetUp(self)
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        sys.path.insert(0, str(scripts))
        try:
            from configure_domp_skill import configure
            self.backup = configure(self.owner, self.chat, self.cache.db_path, unify_agent=True)
        finally:
            sys.path.remove(str(scripts))
        self.current_analysis = analysis("query", "41123", query_type="execution")
        self.main.ai_service.analyze = AsyncMock(side_effect=lambda _content, _task: self.current_analysis)
        self.main.ai_service.select_mcp_tool = AsyncMock(side_effect=AssertionError("受控统一 Agent 不得进入通用工具参数生成"))

        async def call_tool(**kwargs):
            self.calls.append(kwargs)
            name = kwargs["tool_name"]
            if name == QUERY_TOOL:
                return {"structured": {"query_type": "execution", "identifier": "41123", "work_num": "5400000000383829",
                    "execution": {"status": "执行成功", "complete": True, "affected_rows_total": 0,
                                  "details": [{"status": "执行成功", "affected_rows": 0, "elapsed_seconds": 0.01, "exception": ""}], "exceptions": []}}}
            if name == "list_data_maintenance_executions":
                return {"structured": {"items": [{"work_name": "41184", "work_id": "5400000000383894",
                    "work_num": "5400000000383893", "work_status": "2"}]}}
            if name == EXECUTION_TOOL:
                return {"structured": {"status": "执行成功", "complete": True, "affected_rows_total": 3,
                    "details": [{"status": "执行成功", "affected_rows": 3, "elapsed_seconds": 0.02, "exception": ""}], "exceptions": []}}
            if name == SUBMISSION_TOOL:
                return {"structured": {"identifier": "41356", "submitted": True,
                    "work_num": "5400000000383911", "area_name": "山南市.扎囊县"}}
            if name == WORKFLOW_TOOL:
                return {"structured": fixtures.PAYLOAD}
            raise AssertionError(name)
        self.main.mcp_service.call_tool = call_tool

    async def test_migration_leaves_one_visible_agent_per_message_type(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        sys.path.insert(0, str(scripts))
        try:
            from configure_domp_skill import configure
            configure(self.owner, self.chat, self.cache.db_path, unify_agent=True)
        finally:
            sys.path.remove(str(scripts))
        row = self.cache.get_smart_reply_config(self.chat, owner_wxid=self.owner)
        unified = [task for task in row["ai_tasks"] if is_unified_task(task)]
        self.assertEqual({"text", "file"}, {task["message_type"] for task in unified})
        self.assertEqual({UNIFIED_SKILL_ID}, {task["skill_id"] for task in unified})
        self.assertEqual({UNIFIED_SKILL_NAME}, {task["name"] for task in unified})
        self.assertTrue(all(task["mcp_enabled"] and task["mcp_tool_name"] == "" and task["instruction"] == UNIFIED_PROMPT for task in unified))
        self.assertEqual(0, sum(task.get("mcp_tool_name") in {WORKFLOW_TOOL, QUERY_TOOL, APPROVAL_TOOL, EXECUTION_TOOL, SUBMISSION_TOOL} for task in row["ai_tasks"]))

    async def test_unified_query_and_execute_use_controlled_tools_and_truthful_results(self):
        await self.process(self.message(msg="查一下 41123 执行情况"))
        self.assertEqual([QUERY_TOOL], [call["tool_name"] for call in self.calls])
        self.assertIn("影响条数合计：0", self.send.call_args.args[3])
        self.calls.clear()
        self.current_analysis = analysis("execute", "41184")
        await self.process(self.message(id="101", msg="兄弟，帮我执行下 41184",
                                        fromid=self.owner, isSender=1, sendorrecv="1"))
        self.assertEqual(["list_data_maintenance_executions", EXECUTION_TOOL], [call["tool_name"] for call in self.calls])
        self.assertIn("影响条数合计：3", self.send.call_args.args[3])
        self.assertEqual((self.chat, self.owner), self.send.call_args.args[:2])
        self.main.ai_service.select_mcp_tool.assert_not_awaited()

    async def test_unified_submission_and_sql_workflow_keep_existing_guards(self):
        self.current_analysis = analysis("submit", "41356")
        await self.process(self.message(msg="提交运维单 41356"))
        self.assertEqual(SUBMISSION_TOOL, self.calls[-1]["tool_name"])
        self.assertEqual({"identifier": "41356", "force": False}, self.calls[-1]["arguments"])
        self.current_analysis = analysis("sql_workflow", "40386", sql_purpose="调整指定单位经办区划", risk_level="medium")
        await self.process(self.message(id="102", msg=fixtures.SOURCE))
        self.assertEqual(WORKFLOW_TOOL, self.calls[-1]["tool_name"])
        self.assertEqual(fixtures.SOURCE, self.calls[-1]["arguments"]["sql_text"])
        self.assertFalse(self.calls[-1]["arguments"]["dry_run"])

    async def test_combined_text_request_uses_same_senders_recent_matching_txt(self):
        sql = "UPDATE EINP_BASICINFO.ab01 SET aab034='540224' WHERE aab001=540100279605"
        path = Path(self.temp.name) / "41323.txt"
        path.write_text(sql, encoding="utf-8")
        self.cache.upsert_messages(self.chat, [{
            "id": "file-41323", "msgtype": "49", "timestamp": 100,
            "fromid": self.sender, "sendorrecv": "2", "isSender": 0,
            "msg": "<msg><appmsg><type>6</type><title>41323.txt</title></appmsg></msg>",
            "file_path": str(path),
        }], owner_wxid=self.owner)
        self.current_analysis = analysis("sql_workflow", "41323", sql_purpose="调整指定单位经办机构", risk_level="medium")
        await self.process(self.message(id="combined-41323", timestamp=101,
            msg="提交填报运维单 41323,并审核执行"))
        self.assertEqual(WORKFLOW_TOOL, self.calls[-1]["tool_name"])
        submitted = self.calls[-1]["arguments"]["sql_text"]
        self.assertIn("操作要求：填报运维单 41323，审核通过后执行。", submitted)
        self.assertIn("文件名：41323.txt", submitted)
        self.assertTrue(submitted.endswith(sql))

    async def test_combined_text_request_never_uses_another_senders_txt(self):
        path = Path(self.temp.name) / "41323.txt"
        path.write_text("UPDATE S.T SET value=1 WHERE id=1", encoding="utf-8")
        self.cache.upsert_messages(self.chat, [{
            "id": "other-file", "msgtype": "49", "timestamp": 100,
            "fromid": "another_sender", "sendorrecv": "2", "isSender": 0,
            "msg": "<msg><appmsg><type>6</type><title>41323.txt</title></appmsg></msg>",
            "file_path": str(path),
        }], owner_wxid=self.owner)
        await self.process(self.message(id="combined-without-own-file", timestamp=101,
            msg="提交填报运维单 41323,并审核执行"))
        self.assertEqual([], self.calls)
        self.assertIn("未找到同一发送人", self.send.call_args.args[3])


if __name__ == "__main__":
    unittest.main()
