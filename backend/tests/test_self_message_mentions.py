"""Self-sent messages must explicitly address the smart-reply Agent."""

import unittest
from unittest.mock import patch

from domp_query_bridge import QUERY_TOOL
from tests import test_domp_unified_bridge as fixtures


class SelfMessageMentionTests(unittest.IsolatedAsyncioTestCase):
    process = fixtures.UnifiedGroupTests.process
    message = fixtures.UnifiedGroupTests.message

    async def asyncSetUp(self):
        await fixtures.UnifiedGroupTests.asyncSetUp(self)
        self.cache.upsert_group_members(
            self.chat,
            [
                {"wxid": self.owner, "name": "Aixed"},
                {"wxid": self.sender, "name": "Aixed"},
                {"wxid": "other_member", "name": "远方多远"},
            ],
            owner_wxid=self.owner,
        )
        agent = patch.object(self.main.agent_manager, "get_agent", return_value={
            "wxid": self.owner, "nickname": "Aixed",
        })
        agent.start()
        self.addCleanup(agent.stop)

    async def test_self_messages_without_agent_mention_never_reach_ai_or_tools(self):
        for index, content in enumerate((
            "查一下 41123 执行情况",
            "兄弟，帮我执行下 41184",
            "提交运维单 41356",
            "审核一下 41366 运维单",
            "今天有没有待审核和待执行的单子",
            "@远方多远 38家单位那个数据咋样了",
        )):
            with self.subTest(content=content):
                await self.process(self.message(
                    id=str(index), msg=content, fromid=self.owner,
                    isSender=1, sendorrecv="1",
                ))
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.send.assert_not_awaited()

    async def test_every_self_sender_signal_requires_mention(self):
        for index, overrides in enumerate((
            {"fromid": self.owner, "isSender": 0, "sendorrecv": "2"},
            {"isSender": 1, "sendorrecv": "2"},
            {"isSender": 0, "sendorrecv": "1"},
        )):
            with self.subTest(overrides=overrides):
                await self.process(self.message(
                    id=str(index), msg="查一下 41123 执行情况", **overrides,
                ))
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.send.assert_not_awaited()

    async def test_other_member_and_all_mentions_do_not_address_agent(self):
        for index, target in enumerate(("other_member", self.sender, "notify@all")):
            await self.process(self.message(
                id=str(index), msg="@空气 查一下 41123 执行情况", fromid=self.owner,
                isSender=1, sendorrecv="1", atuserlist=[target],
            ))
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.send.assert_not_awaited()

    async def test_own_nickname_and_partial_air_name_do_not_trigger(self):
        for index, content in enumerate((
            "@Aixed 查一下 41123 执行情况",
            "@空气污染 查一下 41123 执行情况",
            "查一下 41123 执行情况",
        )):
            await self.process(self.message(
                id=str(index), msg=content, fromid=self.owner,
                isSender=1, sendorrecv="1", atuserlist=[self.owner],
            ))
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.send.assert_not_awaited()

    async def test_explicit_agent_mentions_still_allow_owner_queries(self):
        for index, mention in enumerate((
            {"atuserlist": [self.owner]},
            {"msgsource": "<msgsource><atuserlist><![CDATA[" + self.owner + "]]></atuserlist></msgsource>"},
            {},  # Some Hook versions only provide the visible mention text.
        )):
            await self.process(self.message(
                id=str(index), msg="@空气\u2005查一下 41123 执行情况",
                fromid=self.owner, isSender=1, sendorrecv="1", **mention,
            ))
        self.assertEqual([QUERY_TOOL] * 3, [call["tool_name"] for call in self.calls])
        self.assertEqual(6, self.send.await_count)

    async def test_mentioning_other_member_in_metadata_cannot_be_overridden_by_text(self):
        await self.process(self.message(
            msg="@空气 查一下 41123 执行情况", fromid=self.owner,
            isSender=1, sendorrecv="1", atuserlist=["other_member"],
        ))
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([], self.calls)
        self.send.assert_not_awaited()

    async def test_air_mention_at_end_of_command_is_allowed(self):
        await self.process(self.message(
            msg="查一下 41123 执行情况@空气", fromid=self.owner,
            isSender=1, sendorrecv="1",
        ))
        self.assertEqual([QUERY_TOOL], [call["tool_name"] for call in self.calls])
        self.assertEqual(2, self.send.await_count)

    async def test_query_history_does_not_allow_unmentioned_self_followups(self):
        await self.process(self.message(
            msg="@空气 查一下 41123 执行情况", fromid=self.owner,
            isSender=1, sendorrecv="1", atuserlist=[self.owner],
        ))
        self.main.ai_service.analyze.reset_mock()
        await self.process(self.message(
            id="101", msg="详情呢", fromid=self.owner, isSender=1, sendorrecv="1",
        ))
        self.main.ai_service.analyze.assert_not_awaited()
        self.assertEqual([QUERY_TOOL], [call["tool_name"] for call in self.calls])
        self.assertEqual(2, self.send.await_count)


if __name__ == "__main__":
    unittest.main()
