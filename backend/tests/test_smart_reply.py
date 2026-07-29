import os
import tempfile
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
        "target_senders": [SENDER],
        "rules": [{"id": "rule_1", "keyword": "urgent", "reply": "received"}],
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

    def test_unknown_direction_is_allowed_when_sender_is_not_self(self):
        decision = self.evaluate("title\nbody\nurgent", sendorrecv="")
        self.assertTrue(decision.should_send)

    def test_two_lines_reply_one_and_stop_group_listener(self):
        decision = self.evaluate("first\nsecond")
        self.assertEqual("1", decision.reply)
        self.assertTrue(decision.disable_after_send)

    def test_whitelist_and_self_messages_are_filtered(self):
        denied = self.evaluate("a\nb\nurgent", fromid="wxid_other")
        self.assertEqual("sender_not_allowed", denied.reason)
        own = self.evaluate("a\nb\nurgent", sendorrecv="1")
        self.assertEqual("self_message", own.reason)

    def test_empty_location_image_and_non_text_messages_are_filtered(self):
        self.assertEqual("empty_message", self.evaluate(" ").reason)
        self.assertEqual("ignored_prefix", self.evaluate("[位置]\na\nurgent").reason)
        self.assertEqual("ignored_prefix", self.evaluate("[图片] caption\na\nurgent").reason)
        self.assertEqual("non_text_message", self.evaluate("a\nb\nurgent", msgtype="3").reason)

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


class SmartReplyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache = SqliteMessageCache(os.path.join(self.temp_dir.name, "cache.sqlite3"))

    def tearDown(self):
        self.cache = None
        self.temp_dir.cleanup()

    def test_crud_stats_and_owner_isolation(self):
        saved = self.cache.upsert_smart_reply_config(config(), owner_wxid=OWNER)
        self.assertTrue(saved["enabled"])
        self.assertEqual([SENDER], saved["target_senders"])
        self.assertEqual([], self.cache.list_smart_reply_configs(owner_wxid="other_owner"))

        updated = self.cache.record_smart_reply_trigger(
            CHAT_ID,
            owner_wxid=OWNER,
            disable=True,
            triggered_at=123,
        )
        self.assertIsNotNone(updated)
        self.assertFalse(updated["enabled"])
        self.assertEqual(1, updated["reply_count"])
        self.assertEqual(123, updated["last_triggered_at"])

        self.cache.upsert_smart_reply_config(config(enabled=True), owner_wxid=OWNER)
        reenabled = self.cache.get_smart_reply_config(CHAT_ID, owner_wxid=OWNER)
        self.assertIsNotNone(reenabled)
        self.assertTrue(reenabled["enabled"])
        self.assertEqual(1, reenabled["reply_count"])
        self.assertTrue(self.cache.delete_smart_reply_config(CHAT_ID, owner_wxid=OWNER))
        self.assertIsNone(self.cache.get_smart_reply_config(CHAT_ID, owner_wxid=OWNER))


if __name__ == "__main__":
    unittest.main()
