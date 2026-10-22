import copy
import unittest
from unittest.mock import AsyncMock, patch

import test_domp_bridge as fixtures
from domp_city_selection import city_selection_failure, selection_number, selection_options
from domp_offline_bridge import OfflineConfirmationStore
from mcp_service import McpServiceError
from sqlite_cache import SqliteMessageCache


AREAS = [
    {"area_name": "日喀则市", "area_code": "540200"},
    {"area_name": "日喀则市.日喀则市市本级", "area_code": "540299"},
    {"area_name": "日喀则市.桑珠孜区", "area_code": "540202"},
    {"area_name": "日喀则市.南木林县", "area_code": "540221"},
]
FAILURE = {"identifier": "40386", "submission": {"status": "填报失败", "requires_city_selection": True,
    "area_candidates": AREAS, "message": "行政区划匹配不唯一，请手工选择：" + "、".join(a["area_name"] for a in AREAS)},
    "approval": {"status": "未开始"}, "execution": {"status": "未开始"}}


class CitySelectionTests(unittest.TestCase):
    def test_legacy_candidates_are_verified_against_live_names_and_codes(self):
        legacy = copy.deepcopy(FAILURE)
        legacy["submission"].pop("requires_city_selection")
        legacy["submission"].pop("area_candidates")
        self.assertTrue(city_selection_failure(legacy))
        self.assertEqual(AREAS, selection_options(legacy, AREAS))
        legacy["submission"]["area_candidates"] = [{**AREAS[0], "area_code": "invented"}, "不存在县"]
        self.assertEqual([], selection_options(legacy, AREAS))

    def test_uncertain_or_started_business_requests_never_become_selection_retries(self):
        for changes in ({"work_num": "existing"}, {"work_id": "existing"}, {"dry_run": True}, {"duplicate": True},
            {"submission": {**FAILURE["submission"], "demand_submitted": True}},
            {"submission": {**FAILURE["submission"], "sql_submitted": True}},
            {"submission": {**FAILURE["submission"], "status": "待核对"}},
            {"approval": {"status": "审核通过"}}, {"execution": {"status": "待核对"}}):
            with self.subTest(changes=changes):
                self.assertFalse(city_selection_failure({**FAILURE, **changes}))

    def test_number_parser_rejects_explanations_and_accepts_explicit_ticket(self):
        self.assertEqual(3, selection_number("40386 3", "40386"))
        self.assertEqual(3, selection_number("3", "40386"))
        for text in ("3 不要执行", "是", "-1", "3; SELECT 1", "40387 3"):
            self.assertIsNone(selection_number(text, "40386"))


class CitySelectionConversationTests(unittest.IsolatedAsyncioTestCase):
    process = fixtures.GroupWorkflowTests.process
    message = fixtures.GroupWorkflowTests.message

    async def asyncSetUp(self):
        await fixtures.GroupWorkflowTests.asyncSetUp(self)
        self.context = {"owner_wxid": self.owner, "chat_id": self.chat, "sender": self.sender}
        self.cache.upsert_group_members(self.chat, [{"wxid": self.owner, "name": "Aixed"},
            {"wxid": self.sender, "name": "杨宇"}], owner_wxid=self.owner)
        self.failure = copy.deepcopy(FAILURE)
        self.catalog = copy.deepcopy(AREAS)
        self.workflow_calls = []
        self.workflow_error = False
        async def call_tool(**kwargs):
            if kwargs["tool_name"] == "match_data_maintenance_city":
                text = kwargs["arguments"]["city_text"]
                area = next((a for a in self.catalog if a["area_name"] == text), {})
                return {"structured": {"matched": bool(area), **area, "supported_areas": self.catalog}}
            self.workflow_calls.append(kwargs)
            if len(self.workflow_calls) == 1:
                return {"structured": self.failure}
            if self.workflow_error:
                raise McpServiceError("模拟响应超时")
            return {"structured": copy.deepcopy(fixtures.PAYLOAD)}
        self.main.mcp_service.call_tool = AsyncMock(side_effect=call_tool)
        self.main.ai_service.analyze = AsyncMock(return_value=fixtures.ANALYSIS)

    async def start(self):
        await self.process(self.message())
        self.assertEqual(1, len(self.workflow_calls))
        self.assertIsNotNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))

    async def test_sender_gets_numbered_choices_and_number_resumes_original_sql_once_after_restart(self):
        await self.start()
        args = self.send.call_args.args
        self.assertEqual((self.chat, self.sender, "杨宇"), args[:3])
        self.assertIn("1. 日喀则市", args[3])
        self.assertIn("3. 日喀则市.桑珠孜区", args[3])
        self.assertIn("@Aixed 3", args[3])
        self.assertNotIn("手工选择", args[3])
        with patch.object(self.main, "sqlite_cache", SqliteMessageCache(self.cache.db_path)):
            await self.process(self.message(id="choice", msg="40386 3"))
        self.assertEqual(2, len(self.workflow_calls))
        original = self.workflow_calls[0]["arguments"]
        resumed = self.workflow_calls[1]["arguments"]
        self.assertEqual({**original, "city": "日喀则市.桑珠孜区"}, resumed)
        self.assertFalse(resumed["dry_run"])
        self.assertIn("执行成功", self.send.call_args.args[3])
        self.assertEqual(self.sender, self.send.call_args.args[1])
        self.main.ai_service.analyze.assert_awaited_once()
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))
        await self.process(self.message(id="choice", msg="40386 3"))
        await self.process(self.message(id="choice-again", msg="3"))
        self.assertEqual(2, len(self.workflow_calls))

    async def test_invalid_numbers_and_missing_or_wrong_mentions_do_not_resume(self):
        await self.start()
        for index, text in enumerate(("0", "999", "是", "3 不要执行")):
            await self.process(self.message(id=f"invalid-{index}", msg=text))
        no_mention = self.message(id="no-at", msg="3", atuserlist=[])
        no_mention["msg"] = "3"
        await self.process(no_mention)
        await self.process(self.message(id="wrong-at", msg="@Aixed 3", atuserlist=["someone_else"]))
        await self.process(self.message(id="other-person", msg="3", fromid="stranger"))
        self.assertEqual(1, len(self.workflow_calls))

    async def test_changed_platform_code_does_not_retarget_the_published_number(self):
        await self.start()
        self.catalog[2]["area_code"] = "changed-code"
        await self.process(self.message(id="stale-code", msg="3"))
        self.assertEqual(1, len(self.workflow_calls))
        self.assertIn("未填报", self.send.call_args.args[3])

    async def test_timeout_retains_claim_and_new_number_does_not_replay(self):
        await self.start()
        self.workflow_error = True
        await self.process(self.message(id="timeout-choice", msg="3"))
        await self.process(self.message(id="another-choice", msg="4"))
        self.assertEqual(2, len(self.workflow_calls))
        self.assertIn("勿重复提交", self.send.call_args.args[3])
        self.assertEqual("日喀则市.桑珠孜区", OfflineConfirmationStore(self.cache.db_path).get(self.context)["confirmed_city"])

    async def test_cancel_removes_pending_without_any_further_business_call(self):
        await self.start()
        await self.process(self.message(id="cancel", msg="取消"))
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))
        self.assertEqual(1, len(self.workflow_calls))

    async def test_existing_pending_cannot_be_overwritten_by_a_new_sql_request(self):
        await self.start()
        await self.process(self.message(id="new-sql", msg=fixtures.SOURCE))
        self.assertEqual(1, len(self.workflow_calls))

    async def test_failure_after_business_start_is_not_offered_as_resumable(self):
        self.failure["work_num"] = "possibly-created"
        await self.process(self.message())
        self.assertIsNone(OfflineConfirmationStore(self.cache.db_path).get(self.context))
        self.assertEqual(1, len(self.workflow_calls))


if __name__ == "__main__":
    unittest.main()
