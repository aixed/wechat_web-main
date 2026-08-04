import os
import tempfile
import time
import unittest

from smart_reply import SmartReplyEngine
from sqlite_cache import SqliteMessageCache


CHAT_ID = "room@chatroom"
OWNER = "wxid_owner"
SENDER = "wxid_sender"


def config(**overrides):
    value = {
        "chat_id": CHAT_ID,
        "chat_name": "Test group",
        "enabled": True,
        "mention_only": False,
        "use_no_src": False,
        "message_types": ["text"],
        "target_senders": [SENDER],
        "rules": [{
            "id": "rule_1",
            "keyword": "urgent",
            "reply": "received",
            "use_regex": False,
            "reply_with_matched_line": False,
        }],
    }
    value.update(overrides)
    return value


def message(content: str, **overrides):
    value = {
        "msgtype": "1",
        "msg": content,
        "fromid": SENDER,
        "sendorrecv": "2",
        "isSender": 0,
    }
    value.update(overrides)
    return value


class SmartReplyEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = SmartReplyEngine(dedup_ttl=300, dedup_limit=5000, cooldown=1.5)

    def evaluate(self, content: str, *, now: float = 10.0, cfg=None, **message_overrides):
        return self.engine.evaluate(
            owner_wxid=OWNER,
            chat_id=CHAT_ID,
            self_wxid=OWNER,
            message=message(content, **message_overrides),
            config=cfg or config(),
            now=now,
        )

    def test_keyword_match_requires_allowed_multiline_text(self):
        decision = self.evaluate("title\nbody\nURGENT request")
        self.assertTrue(decision.should_send)
        self.assertEqual("received", decision.reply)
        self.assertEqual("keyword_matched", decision.reason)

    def test_keyword_matches_single_line_and_final_line(self):
        single_line = self.evaluate("urgent")
        self.assertEqual(("received",), single_line.replies)

        final_line = self.evaluate("first\nsecond\nurgent", now=12.0)
        self.assertEqual(("received",), final_line.replies)

    def test_regular_expression_rule(self):
        regex_config = config(rules=[{
            "id": "regex",
            "keyword": r"order-\d+$",
            "reply": "matched",
            "use_regex": True,
            "reply_with_matched_line": False,
        }])
        decision = self.evaluate("heading\nORDER-42", cfg=regex_config)
        self.assertEqual(("matched",), decision.replies)

    def test_all_plain_and_regex_fixed_reply_rules_are_applied(self):
        multi_config = config(rules=[
            {
                "id": "plain",
                "keyword": "和田",
                "reply": "和田回复",
                "use_regex": False,
                "reply_with_matched_line": False,
            },
            {
                "id": "regex",
                "keyword": r"喀什\d+$",
                "reply": "喀什回复",
                "use_regex": True,
                "reply_with_matched_line": False,
            },
            {
                "id": "not-matched",
                "keyword": "酒泉",
                "reply": "酒泉回复",
                "use_regex": False,
                "reply_with_matched_line": False,
            },
        ])
        decision = self.evaluate("和田市场\n今天喀什8", cfg=multi_config)
        self.assertEqual(("和田回复", "喀什回复"), decision.replies)

    def test_fixed_and_matched_line_rules_are_combined(self):
        multi_config = config(rules=[
            {
                "id": "line",
                "keyword": "和田",
                "reply": "",
                "use_regex": False,
                "reply_with_matched_line": True,
            },
            {
                "id": "fixed",
                "keyword": r"喀什\d+$",
                "reply": "喀什固定回复",
                "use_regex": True,
                "reply_with_matched_line": False,
            },
        ])
        decision = self.evaluate("和田市场99\n今天喀什8", cfg=multi_config)
        self.assertEqual(("和田市场", "喀什固定回复"), decision.replies)

    def test_matched_lines_are_returned_without_trailing_digits(self):
        line_config = config(rules=[{
            "id": "lines",
            "keyword": "和田",
            "reply": "",
            "use_regex": False,
            "reply_with_matched_line": True,
        }])
        decision = self.evaluate(
            "和田钢材市场455\n我在和田888\n今天和田2\n和田钢材拉不拉\n说和田玉\n没有命中",
            cfg=line_config,
        )
        self.assertEqual("matched_lines", decision.reason)
        self.assertEqual((
            "和田钢材市场",
            "我在和田",
            "今天和田",
            "和田钢材拉不拉",
            "说和田玉",
        ), decision.replies)

    def test_matched_lines_are_combined_across_all_keyword_rules(self):
        line_config = config(rules=[
            {
                "id": "hetian",
                "keyword": "和田",
                "reply": "",
                "use_regex": False,
                "reply_with_matched_line": True,
            },
            {
                "id": "kashi",
                "keyword": "喀什",
                "reply": "",
                "use_regex": False,
                "reply_with_matched_line": True,
            },
            {
                "id": "jiuquan",
                "keyword": "酒泉",
                "reply": "",
                "use_regex": False,
                "reply_with_matched_line": True,
            },
        ])
        decision = self.evaluate(
            "和田钢材市场455\n我在和田888\n今天和田2\n和田钢材拉不拉\n说和田玉\n喀什远方\n我在喀什8",
            cfg=line_config,
        )
        self.assertEqual((
            "和田钢材市场",
            "我在和田",
            "今天和田",
            "和田钢材拉不拉",
            "说和田玉",
            "喀什远方",
            "我在喀什",
        ), decision.replies)

    def test_line_matching_multiple_rules_is_sent_once(self):
        line_config = config(rules=[
            {
                "id": "hetian",
                "keyword": "和田",
                "reply": "",
                "use_regex": False,
                "reply_with_matched_line": True,
            },
            {
                "id": "kashi",
                "keyword": "喀什",
                "reply": "",
                "use_regex": False,
                "reply_with_matched_line": True,
            },
        ])
        decision = self.evaluate("标题\n和田喀什99\n结束", cfg=line_config)
        self.assertEqual(("和田喀什",), decision.replies)

    def test_unknown_direction_is_allowed_when_sender_is_not_self(self):
        decision = self.evaluate("title\nbody\nurgent", sendorrecv="")
        self.assertTrue(decision.should_send)

    def test_private_chat_processes_messages_from_that_contact(self):
        private_wxid = "wxid_friend"
        decision = self.engine.evaluate(
            owner_wxid=OWNER,
            chat_id=private_wxid,
            self_wxid=OWNER,
            message=message("urgent", fromid=private_wxid),
            config=config(chat_id=private_wxid, target_senders=[private_wxid]),
            now=10.0,
        )
        self.assertEqual(("received",), decision.replies)

    def test_private_chat_does_not_apply_group_mention_filter(self):
        private_wxid = "wxid_friend"
        decision = self.engine.evaluate(
            owner_wxid=OWNER,
            chat_id=private_wxid,
            self_wxid=OWNER,
            message=message("urgent", fromid=private_wxid),
            config=config(
                chat_id=private_wxid,
                target_senders=[private_wxid],
                mention_only=True,
            ),
            now=10.0,
        )
        self.assertEqual(("received",), decision.replies)

    def test_two_lines_reply_one_and_stop_group_listener(self):
        decision = self.evaluate("first\nsecond")
        self.assertEqual("1", decision.reply)
        self.assertTrue(decision.disable_after_send)

    def test_whitelist_and_self_messages_are_filtered(self):
        denied = self.evaluate("a\nb\nurgent", fromid="wxid_other")
        self.assertEqual("sender_not_allowed", denied.reason)
        own = self.evaluate("a\nb\nurgent", sendorrecv="1")
        self.assertEqual("self_message", own.reason)

    def test_mention_filter_is_optional_and_defaults_to_all_target_messages(self):
        decision = self.evaluate(
            "urgent without mention",
            cfg=config(mention_only=False),
            msgsource="<msgsource><atuserlist>wxid_other</atuserlist></msgsource>",
        )
        self.assertEqual(("received",), decision.replies)

    def test_mention_filter_requires_current_account_in_msgsource(self):
        missing = self.evaluate(
            "urgent without self mention",
            cfg=config(mention_only=True),
            msgsource="<msgsource><atuserlist>wxid_other,notify@all</atuserlist></msgsource>",
        )
        self.assertEqual("mention_required", missing.reason)

        mentioned = self.evaluate(
            "urgent with self mention",
            now=12.0,
            cfg=config(mention_only=True),
            msgsource=(
                "<msgsource><atuserlist><![CDATA[wxid_other,wxid_owner]]>"
                "</atuserlist></msgsource>"
            ),
        )
        self.assertEqual(("received",), mentioned.replies)

    def test_mention_filter_supports_direct_callback_field(self):
        decision = self.evaluate(
            "urgent direct mention",
            cfg=config(mention_only=True),
            atuserlist=["wxid_other", OWNER],
        )
        self.assertEqual(("received",), decision.replies)

    def test_empty_location_image_and_non_text_messages_are_filtered(self):
        self.assertEqual("empty_message", self.evaluate(" ").reason)
        self.assertEqual("ignored_prefix", self.evaluate("[位置]\na\nurgent").reason)
        self.assertEqual("message_type_not_enabled", self.evaluate("[图片] caption\na\nurgent").reason)
        self.assertEqual("message_type_not_enabled", self.evaluate("a\nb\nurgent", msgtype="3").reason)

    def test_enabled_non_text_categories_do_not_use_text_rules(self):
        samples = {
            "image": ("3", "urgent image"),
            "gif": ("47", "urgent gif"),
            "voice": ("34", "urgent voice"),
            "video": ("43", "urgent video"),
            "file": ("49", "<msg><appmsg><type>6</type><title>urgent file</title></appmsg></msg>"),
            "xml": ("49", "<msg><appmsg><type>5</type><title>urgent xml</title></appmsg></msg>"),
            "system": ("10000", "<sysmsg><content>urgent system</content></sysmsg>"),
            "recall": ("10000", "<sysmsg type=\"revokemsg\"><revokemsg><replacemsg>urgent recall</replacemsg></revokemsg></sysmsg>"),
            "quote": ("49", "<msg><appmsg><type>57</type><title>urgent quote</title><refermsg><content>original</content></refermsg></appmsg></msg>"),
        }
        for index, (message_type, (msg_type, content)) in enumerate(samples.items()):
            with self.subTest(message_type=message_type):
                enabled = self.evaluate(
                    content,
                    now=20.0 + index * 2,
                    cfg=config(message_types=[message_type]),
                    msgtype=msg_type,
                )
                self.assertEqual("keyword_not_matched", enabled.reason)
                self.assertFalse(enabled.should_send)

        disabled = self.evaluate(
            samples["xml"][1],
            now=40.0,
            cfg=config(message_types=["text"]),
            msgtype="49",
        )
        self.assertEqual("message_type_not_enabled", disabled.reason)

    def test_empty_media_categories_remain_inert_without_dedicated_rules(self):
        samples = {
            "image": ("3", "[图片]"),
            "gif": ("47", "[GIF]"),
            "voice": ("34", "[语音]"),
            "video": ("43", "[视频]"),
        }
        for index, (message_type, (msg_type, placeholder)) in enumerate(samples.items()):
            with self.subTest(message_type=message_type):
                media_config = config(
                    message_types=[message_type],
                    rules=[{
                        "id": message_type,
                        "keyword": placeholder,
                        "reply": "media received",
                        "use_regex": False,
                        "reply_with_matched_line": False,
                    }],
                )
                decision = self.evaluate("", now=60.0 + index * 2, cfg=media_config, msgtype=msg_type)
                self.assertEqual("keyword_not_matched", decision.reason)
                self.assertFalse(decision.should_send)

    def test_single_line_messages_are_filtered(self):
        self.assertEqual("pure_single_line", self.evaluate("English").reason)
        self.assertEqual("pure_single_line", self.evaluate("12345").reason)
        self.assertEqual("pure_single_line", self.evaluate("纯中文").reason)
        self.assertEqual("too_few_lines", self.evaluate("mixed123").reason)

    def test_duplicate_and_cooldown_are_enforced(self):
        first = self.evaluate("a\nb\nurgent one", now=10.0)
        duplicate = self.evaluate("a\nb\nurgent one", now=20.0)
        cooldown = self.evaluate("a\nb\nurgent two", now=10.5)
        after_cooldown = self.evaluate("a\nb\nurgent three", now=12.0)
        self.assertTrue(first.should_send)
        self.assertEqual("duplicate", duplicate.reason)
        self.assertEqual("cooldown", cooldown.reason)
        self.assertTrue(after_cooldown.should_send)

    def test_dedup_cache_is_bounded(self):
        engine = SmartReplyEngine(dedup_ttl=300, dedup_limit=3, cooldown=0)
        for index in range(5):
            decision = engine.evaluate(
                owner_wxid=OWNER,
                chat_id=CHAT_ID,
                self_wxid=OWNER,
                message=message(f"a\nb\nurgent {index}"),
                config=config(),
                now=float(index),
            )
            self.assertTrue(decision.should_send)
        self.assertEqual(3, len(engine._seen))

    def test_ai_replies_use_dedup_and_cooldown_gates(self):
        first = self.engine.reserve_ai_replies(
            owner_wxid=OWNER,
            chat_id=CHAT_ID,
            message=message("sql one"),
            replies=("analysis one",),
            now=10.0,
        )
        duplicate = self.engine.reserve_ai_replies(
            owner_wxid=OWNER,
            chat_id=CHAT_ID,
            message=message("sql one"),
            replies=("analysis one",),
            now=20.0,
        )
        cooldown = self.engine.reserve_ai_replies(
            owner_wxid=OWNER,
            chat_id=CHAT_ID,
            message=message("sql two"),
            replies=("analysis two",),
            now=10.5,
        )
        self.assertEqual(("analysis one",), first.replies)
        self.assertEqual("duplicate", duplicate.reason)
        self.assertEqual("cooldown", cooldown.reason)

    def test_ai_tasks_can_process_single_line_chinese_text(self):
        ai_config = config(
            rules=[],
            ai_tasks=[{
                "id": "ai_task_1",
                "name": "简要答复",
                "enabled": True,
                "instruction": "进行简要回复",
            }],
        )
        decision = self.evaluate("我想要办社保卡，在哪里办", cfg=ai_config)
        self.assertEqual("keyword_not_matched", decision.reason)
        self.assertFalse(decision.should_send)


class SmartReplyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = SqliteMessageCache(os.path.join(self.temp_dir.name, "cache.sqlite3"))

    def tearDown(self):
        self.cache = None
        self.temp_dir.cleanup()

    def test_crud_stats_and_owner_isolation(self):
        ai_tasks = [{
            "id": "ai_task_1",
            "name": "SQL analyzer",
            "enabled": True,
            "skill_type": "custom",
            "skill_id": "sql_analyzer",
            "instruction": "Analyze SQL",
            "confidence": 85,
            "output_mode": "result",
            "reply_template": "{{result}}",
            "preserve_formatting": True,
            "send_items_separately": False,
            "max_parallel": 3,
        }]
        saved = self.cache.upsert_smart_reply_config(
            config(ai_tasks=ai_tasks, mention_only=True, use_no_src=True),
            owner_wxid=OWNER,
        )
        self.assertTrue(saved["enabled"])
        self.assertTrue(saved["mention_only"])
        self.assertTrue(saved["use_no_src"])
        self.assertEqual(["text"], saved["message_types"])
        self.assertEqual([SENDER], saved["target_senders"])
        self.assertEqual(ai_tasks, saved["ai_tasks"])
        self.assertEqual([], self.cache.list_smart_reply_configs(owner_wxid="other_owner"))

        updated = self.cache.record_smart_reply_trigger(
            CHAT_ID,
            owner_wxid=OWNER,
            disable=True,
            increment=3,
            triggered_at=123,
        )
        self.assertIsNotNone(updated)
        self.assertFalse(updated["enabled"])
        self.assertEqual(3, updated["reply_count"])
        self.assertEqual(123, updated["last_triggered_at"])

        self.cache.upsert_smart_reply_config(config(enabled=True, ai_tasks=ai_tasks), owner_wxid=OWNER)
        reenabled = self.cache.get_smart_reply_config(CHAT_ID, owner_wxid=OWNER)
        self.assertIsNotNone(reenabled)
        self.assertTrue(reenabled["enabled"])
        self.assertEqual(ai_tasks, reenabled["ai_tasks"])
        self.assertEqual(3, reenabled["reply_count"])
        self.assertTrue(self.cache.delete_smart_reply_config(CHAT_ID, owner_wxid=OWNER))
        self.assertIsNone(self.cache.get_smart_reply_config(CHAT_ID, owner_wxid=OWNER))


class SmartReplyProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_level_text_reply_uses_no_src_sender(self):
        import main

        temp_dir = tempfile.TemporaryDirectory()
        cache = SqliteMessageCache(os.path.join(temp_dir.name, "cache.sqlite3"))
        normal_sent: list[tuple[str, str]] = []
        no_src_sent: list[tuple[str, str]] = []

        async def fake_send_text(wxid, text):
            normal_sent.append((wxid, text))
            return {"SendTextMsg": "1"}

        async def fake_send_text_no_src(wxid, text):
            no_src_sent.append((wxid, text))
            return {"SendTextMsg_NoSrc": "1"}

        async def noop(*_args, **_kwargs):
            return None

        old_cache = main.sqlite_cache
        old_engine = main.smart_reply_engine
        old_send_text = main.wechat_api.send_text
        old_send_text_no_src = main.wechat_api.send_text_no_src
        old_broadcast = main.manager.broadcast
        old_local_sent = main._broadcast_local_sent_for_agent
        old_log = main._log
        logs: list[str] = []
        try:
            main.sqlite_cache = cache
            main.smart_reply_engine = SmartReplyEngine(cooldown=0)
            main.wechat_api.send_text = fake_send_text
            main.wechat_api.send_text_no_src = fake_send_text_no_src
            main.manager.broadcast = noop
            main._broadcast_local_sent_for_agent = noop
            main._log = logs.append
            cache.upsert_smart_reply_config(config(use_no_src=True), owner_wxid=OWNER)

            await main._process_smart_reply_message(
                owner_wxid=OWNER,
                agent_id="agent_1",
                self_wxid=OWNER,
                chat_id=CHAT_ID,
                message=message("urgent"),
                received_at=time.perf_counter() - 0.02,
            )
        finally:
            main.sqlite_cache = old_cache
            main.smart_reply_engine = old_engine
            main.wechat_api.send_text = old_send_text
            main.wechat_api.send_text_no_src = old_send_text_no_src
            main.manager.broadcast = old_broadcast
            main._broadcast_local_sent_for_agent = old_local_sent
            main._log = old_log
            temp_dir.cleanup()

        self.assertEqual([], normal_sent)
        self.assertEqual([(CHAT_ID, "received")], no_src_sent)
        timing_log = next(line for line in logs if line.startswith("[SMART_REPLY] sent"))
        self.assertIn("mode=no_src", timing_log)
        self.assertRegex(timing_log, r"queue_ms=\d+\.\d")
        self.assertRegex(timing_log, r"evaluate_ms=\d+\.\d")
        self.assertRegex(timing_log, r"send_ms=\d+\.\d")
        self.assertRegex(timing_log, r"total_ms=\d+\.\d")

    async def test_callback_schedules_reply_before_profile_hydration(self):
        import main

        events: list[str] = []
        scheduled: list[dict] = []

        def fake_normalize(_msg, _sendorrecv, _self_wxid):
            return CHAT_ID, message("urgent")

        def fake_schedule(**kwargs):
            events.append("scheduled")
            scheduled.append(kwargs)

        async def fake_profiles(*_args, **_kwargs):
            events.append("profiles")
            return {}

        async def fake_broadcast(*_args, **_kwargs):
            events.append("broadcast")

        old_normalize = main._normalize_callback_message
        old_schedule = main._schedule_smart_reply_message
        old_profiles = main._ensure_contact_profiles
        old_store = main._store_message_and_session
        old_load_sessions = main._load_session_cache_into_state
        old_broadcast = main.manager.broadcast
        old_activate = main._activate_runtime
        old_put_self = main._put_self_info_field
        try:
            main._normalize_callback_message = fake_normalize
            main._schedule_smart_reply_message = fake_schedule
            main._ensure_contact_profiles = fake_profiles
            main._store_message_and_session = lambda *_args, **_kwargs: {}
            main._load_session_cache_into_state = lambda *_args, **_kwargs: None
            main.manager.broadcast = fake_broadcast
            main._activate_runtime = lambda *_args, **_kwargs: None
            main._put_self_info_field = lambda *_args, **_kwargs: None

            result = await main._process_wechat_callback({
                "agent_id": "agent_1",
                "selfwxid": OWNER,
                "sendorrecv": "2",
                "msglist": [message("urgent")],
            })
        finally:
            main._normalize_callback_message = old_normalize
            main._schedule_smart_reply_message = old_schedule
            main._ensure_contact_profiles = old_profiles
            main._store_message_and_session = old_store
            main._load_session_cache_into_state = old_load_sessions
            main.manager.broadcast = old_broadcast
            main._activate_runtime = old_activate
            main._put_self_info_field = old_put_self

        self.assertEqual({"status": "success"}, result)
        self.assertLess(events.index("scheduled"), events.index("profiles"))
        self.assertLess(events.index("scheduled"), events.index("broadcast"))
        self.assertEqual(1, len(scheduled))
        self.assertIsInstance(scheduled[0]["received_at"], float)

    async def test_private_ai_reply_reaches_send_stage(self):
        import main

        private_wxid = "kingloveliu"
        temp_dir = tempfile.TemporaryDirectory()
        cache = SqliteMessageCache(os.path.join(temp_dir.name, "cache.sqlite3"))
        sent: list[tuple[str, str]] = []

        class FakeAiService:
            configured = True

            async def analyze(self, _content, _task):
                return {
                    "matched": True,
                    "confidence": 95,
                    "result": "您可以在当地社保局或银行办理社保卡。",
                    "items": [],
                    "reply": "您可以在当地社保局或银行办理社保卡。",
                }

        async def fake_send_text(wxid, text):
            sent.append((wxid, text))
            return {"SendTextMsg": "1"}

        async def noop_broadcast(*_args, **_kwargs):
            return None

        async def noop_local_sent(*_args, **_kwargs):
            return None

        old_cache = main.sqlite_cache
        old_engine = main.smart_reply_engine
        old_ai_service = main.ai_service
        old_send_text = main.wechat_api.send_text
        old_broadcast = main.manager.broadcast
        old_local_sent = main._broadcast_local_sent_for_agent
        try:
            main.sqlite_cache = cache
            main.smart_reply_engine = SmartReplyEngine(cooldown=0)
            main.ai_service = FakeAiService()
            main.wechat_api.send_text = fake_send_text
            main.manager.broadcast = noop_broadcast
            main._broadcast_local_sent_for_agent = noop_local_sent
            cache.upsert_smart_reply_config(
                config(
                    chat_id=private_wxid,
                    target_senders=[private_wxid],
                    rules=[],
                    ai_tasks=[{
                        "id": "ai_task_1",
                        "name": "简要答复",
                        "enabled": True,
                        "instruction": "进行简要回复，限制50字以内，不限内容",
                        "confidence": 85,
                        "output_mode": "result",
                        "reply_template": "{{result}}",
                        "preserve_formatting": True,
                        "send_items_separately": False,
                        "max_parallel": 3,
                    }],
                ),
                owner_wxid=OWNER,
            )

            await main._process_smart_reply_message(
                owner_wxid=OWNER,
                agent_id="agent_1",
                self_wxid=OWNER,
                chat_id=private_wxid,
                message=message("我想要办社保卡，在哪里办", fromid=private_wxid),
            )
        finally:
            main.sqlite_cache = old_cache
            main.smart_reply_engine = old_engine
            main.ai_service = old_ai_service
            main.wechat_api.send_text = old_send_text
            main.manager.broadcast = old_broadcast
            main._broadcast_local_sent_for_agent = old_local_sent
            temp_dir.cleanup()

        self.assertEqual([(private_wxid, "您可以在当地社保局或银行办理社保卡。")], sent)


if __name__ == "__main__":
    unittest.main()
