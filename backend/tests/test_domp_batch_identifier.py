import json
import unittest
from unittest.mock import AsyncMock, patch

from domp_bridge import PROMPT, workflow_arguments, workflow_reply
from domp_identifier import identifiers_in
from domp_submission_bridge import SUBMISSION_PROMPT, submission_arguments
from domp_unified_bridge import UNIFIED_PROMPT, submission_workflow_identifier
from domp_query_bridge import QUERY_PROMPT, query_arguments
from domp_approval_bridge import APPROVAL_PROMPT, approval_target, approve_target
from domp_execution_bridge import EXECUTION_PROMPT, execution_target, execute_target
import test_domp_bridge as fixtures


def analysis(identifier, **extra):
    return {**fixtures.ANALYSIS, "result": json.dumps({"identifier": identifier, "risk_level": "medium",
        "sql_purpose": "按指定人员修改日期", "risk_notes": [], **extra}, ensure_ascii=False)}


class BatchIdentifierTests(unittest.TestCase):
    def test_sql_headers_and_txt_names_preserve_full_batch_identifier(self):
        for identifier in ("42157-1", "42157-2", "42157-3", "42157-12"):
            for header in ("--- xzsb-" + identifier, "文件名：需求" + identifier + ".txt\n文件内容："):
                with self.subTest(header=header):
                    source = header + "\n" + fixtures.SQL
                    args = workflow_arguments(source, analysis(identifier), {})
                    self.assertEqual(identifier, args["identifier"])
                    self.assertEqual(source, args["sql_text"])
                    with self.assertRaises(ValueError):
                        workflow_arguments(source, analysis("42157"), {})

    def test_invalid_or_multiple_batch_identifiers_cannot_be_truncated(self):
        for identifier in ("42157-0", "42157-01", "42157-1-2", "2750-1", "42157-", "42157-1 42157-2"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                workflow_arguments("--- xzsb-" + identifier + "\n" + fixtures.SQL, analysis("42157"), {})
        self.assertEqual({"42157-12345"}, identifiers_in("--- xzsb-42157-12345", strict=True))

    def test_submission_query_approval_execution_and_recent_sql_use_full_batch(self):
        identifier = "42157-1"
        self.assertEqual({"identifier": identifier, "force": False},
            submission_arguments("提交运维单 " + identifier, analysis(identifier, action="submit")))
        queried = query_arguments("查一下 " + identifier + " 执行情况", analysis(identifier, query_type="execution"), [])
        self.assertEqual(identifier, queried["identifier"])
        action, target = approval_target("审核并执行 " + identifier, analysis(identifier, action="approve_execute"), [])
        self.assertEqual(("approve_execute", identifier), (action, target["identifier"]))
        for source in ("执行 " + identifier, identifier + " 执行下"):
            self.assertEqual(identifier, execution_target(source, analysis(identifier, action="execute"), [])["identifier"])
        self.assertEqual(identifier, submission_workflow_identifier("提交运维单 " + identifier + " 并审核执行"))
        import main
        self.assertEqual(identifier, main._recent_chat_sql_identifier("--- xzsb-" + identifier + "\n" + fixtures.SQL))
        self.assertNotEqual("42157", main._recent_chat_sql_identifier("--- xzsb-" + identifier + "\n" + fixtures.SQL))

    def test_duplicate_reply_teaches_the_next_identifier_without_pending_warning(self):
        payload = {"identifier": "42157", "duplicate": True, "existing_work_num": "5400000000385000",
            "resubmission": {"message": "如需重新填报，请改为：\n--- xzsb-42157-1\n并附完整 SQL。"}}
        reply = workflow_reply(payload, {})
        self.assertIn("本次未填报、审核或执行", reply)
        self.assertIn("--- xzsb-42157-1", reply)
        self.assertNotIn("待核对", reply)
        self.assertNotIn("Error executing tool", reply)

    def test_all_domp_prompts_document_explicit_supplementary_batches(self):
        for prompt in (PROMPT, UNIFIED_PROMPT, SUBMISSION_PROMPT, QUERY_PROMPT, APPROVAL_PROMPT, EXECUTION_PROMPT):
            self.assertIn("42157-1", prompt)
            self.assertIn("后缀", prompt)


class BatchGroupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GroupWorkflowTests.asyncSetUp
    process = fixtures.GroupWorkflowTests.process
    message = fixtures.GroupWorkflowTests.message

    async def test_group_message_keeps_suffix_sql_and_stable_identity_without_auto_increment(self):
        self.analysis = analysis("42157-1")
        source = "--- xzsb-42157-1\n" + fixtures.SQL + ";"
        await self.process(self.message(msg=source + "\n\u2005@Aixed"))
        self.assertEqual(1, len(self.calls))
        self.assertEqual("42157-1", self.calls[0]["arguments"]["identifier"])
        self.assertEqual(source, self.calls[0]["arguments"]["sql_text"])

    async def test_duplicate_message_returns_suffix_hint_and_never_retries_the_tool(self):
        payload = {"identifier": "40386", "duplicate": True,
            "resubmission": {"message": "请将首行改为：\n--- xzsb-40386-1\n并附完整 SQL。"}}
        with patch.object(self.main.mcp_service, "call_tool", AsyncMock(return_value={"structured": payload})) as call:
            await self.process(self.message())
        call.assert_awaited_once()
        self.assertIn("--- xzsb-40386-1", self.send.call_args.args[3])
        self.assertNotIn("结果待核对", self.send.call_args.args[3])

    async def test_dry_run_resolves_only_the_exact_batch_in_pending_lists(self):
        base = {"identifier": "42157", "work_name": "42157", "work_num": "5400000000385000", "work_id": "5400000000385001", "work_status": "2"}
        batch = {"identifier": "42157-1", "work_name": "42157-1", "work_num": "5400000000385002", "work_id": "5400000000385003", "work_status": "2"}
        async def call(name, arguments):
            return {"structured": {"query_type": "pending_approvals", "items": [base, batch]}}
        target = {"identifier": "42157-1", "work_id": "", "work_num": ""}
        approved = await approve_target("approve", target, call, dry_run=True)
        executed, _ = await execute_target(target, call, dry_run=True)
        self.assertIn(batch["work_num"], approved)
        self.assertIn(batch["work_num"], executed)
        self.assertNotIn(base["work_num"], approved)
        self.assertNotIn(base["work_num"], executed)
