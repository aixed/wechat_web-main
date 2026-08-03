import os
import tempfile
import unittest

from sqlite_cache import SqliteMessageCache


class ProtocolHistoryCacheTests(unittest.TestCase):
    def test_empty_database_skips_history_preload_until_first_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "wechat_protocol_cache.sqlite3")
            cache = SqliteMessageCache(db_path)

            self.assertFalse(cache.existed_before_init)
            self.assertFalse(cache.has_message_history(owner_wxid="wxid_owner"))

            message = {
                "id": "msg_1",
                "msgtype": "1",
                "msg": "hello",
                "timestamp": 100,
                "time_unix": 100,
                "sendorrecv": "2",
            }
            cache.upsert_messages("wxid_friend", [message], owner_wxid="wxid_owner")

            self.assertTrue(cache.has_message_history(owner_wxid="wxid_owner"))
            self.assertFalse(cache.has_message_history(owner_wxid="wxid_other"))
            self.assertEqual([message], cache.get_messages("wxid_friend", owner_wxid="wxid_owner"))

            reopened = SqliteMessageCache(db_path)
            self.assertTrue(reopened.existed_before_init)
            self.assertTrue(reopened.has_message_history(owner_wxid="wxid_owner"))
            self.assertEqual([message], reopened.get_messages("wxid_friend", owner_wxid="wxid_owner"))

    def test_hook_and_protocol_database_files_do_not_share_messages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hook_cache = SqliteMessageCache(os.path.join(temp_dir, "wechat_cache.sqlite3"))
            protocol_cache = SqliteMessageCache(os.path.join(temp_dir, "wechat_protocol_cache.sqlite3"))
            hook_cache.upsert_messages(
                "wxid_friend",
                [{"id": "hook_1", "msgtype": "1", "msg": "hook", "timestamp": 1}],
                owner_wxid="wxid_owner",
            )

            self.assertFalse(protocol_cache.has_message_history(owner_wxid="wxid_owner"))
            self.assertEqual([], protocol_cache.get_messages("wxid_friend", owner_wxid="wxid_owner"))


if __name__ == "__main__":
    unittest.main()
