import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import main
import wechat_api
from message_store import MessageStore
from sqlite_cache import SqliteMessageCache


class LocalHookMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_cdn_download_uses_download_endpoint_and_relative_path(self):
        response = httpx.Response(
            200,
            json={"ret": 0, "retmsg": "ok"},
            request=httpx.Request("POST", "http://127.0.0.1:30001/download"),
        )
        with (
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "_post", AsyncMock(return_value=response)) as post,
        ):
            result = await wechat_api.cdn_download_pic(
                decode_key="aes-key",
                file_id="file-id",
                img_filename="image-1.jpg",
            )

        post.assert_awaited_once_with(
            "/download",
            json={
                "savePath": "downloads/image-1.jpg",
                "aeskey": "aes-key",
                "fileid": "file-id",
                "chatType": 0,
                "largesVideo": 0,
                "fileType": 1,
            },
            timeout=30.0,
            bypass_circuit_breaker=True,
        )
        self.assertEqual("downloads/image-1.jpg", result["requested_save_path"])

    async def test_send_success_with_local_path_is_not_filtered(self):
        message = {
            "msgtype": "3",
            "msg": "PC发图片消息成功",
            "fromid": "wxid_self",
            "img_path": r"C:\WeChat Files\FileStorage\Temp\image.jpg",
        }

        self.assertFalse(main._is_callback_status_echo(message, "1", "wxid_self"))

    async def test_history_image_matches_recvtype_one_path_by_size(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image_file:
            image_file.write(b"x" * 128)
            image_path = image_file.name
        try:
            history = {
                "id": "real-id",
                "msgtype": "3",
                "msg": '<msg><img length="128" md5="0123456789abcdef0123456789abcdef"/></msg>',
                "timestamp": 100,
                "sendorrecv": "1",
            }
            callback = {
                "id": "cb_100",
                "msgtype": "3",
                "msg": "0123456789abcdef0123456789abcdef",
                "timestamp": 100,
                "sendorrecv": "1",
                "img_path": image_path,
            }

            matched = main._find_history_image_media(history, [callback])

            self.assertEqual(callback, matched)
        finally:
            os.unlink(image_path)


class MediaPathPersistenceTests(unittest.TestCase):
    def test_message_store_preserves_media_when_history_replaces_message(self):
        store = MessageStore()
        store.add_message(
            "wxid_friend",
            {
                "id": "message-1",
                "msgtype": "3",
                "msg": "hash",
                "timestamp": 100,
                "sendorrecv": "1",
                "img_path": r"C:\Temp\image.jpg",
            },
        )
        store.add_message(
            "wxid_friend",
            {
                "id": "message-1",
                "msgtype": "3",
                "msg": "<msg><img/></msg>",
                "timestamp": 100,
                "sendorrecv": "1",
            },
            replace=True,
        )

        self.assertEqual(r"C:\Temp\image.jpg", store.get_messages("wxid_friend")[0]["img_path"])

    def test_sqlite_upsert_preserves_media_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SqliteMessageCache(os.path.join(temp_dir, "messages.sqlite3"))
            cache.upsert_messages(
                "wxid_friend",
                [{
                    "id": "message-1",
                    "msgtype": "3",
                    "msg": "hash",
                    "timestamp": 100,
                    "img_path": r"C:\Temp\image.jpg",
                }],
                owner_wxid="wxid_self",
            )
            cache.upsert_messages(
                "wxid_friend",
                [{
                    "id": "message-1",
                    "msgtype": "3",
                    "msg": "<msg><img/></msg>",
                    "timestamp": 100,
                }],
                owner_wxid="wxid_self",
            )

            stored = cache.get_messages("wxid_friend", owner_wxid="wxid_self")
            self.assertEqual(r"C:\Temp\image.jpg", stored[0]["img_path"])


if __name__ == "__main__":
    unittest.main()
