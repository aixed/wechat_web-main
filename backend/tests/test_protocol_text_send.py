import unittest
from unittest.mock import AsyncMock, patch

import protocol_api
import wechat_api


class ProtocolTextSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_client_uses_newsendmsg_contract(self):
        post = AsyncMock(return_value={"ok": True})

        with patch.object(protocol_api, "_post", post):
            result = await protocol_api.send_text("SESSION_123", "filehelper", "hello")

        self.assertEqual({"ok": True}, result)
        post.assert_awaited_once_with(
            "/newsendmsg",
            {
                "session_id": "SESSION_123",
                "username": "filehelper",
                "content": "hello",
            },
            timeout=30.0,
        )

    async def test_shared_send_text_forwards_active_protocol_session(self):
        send = AsyncMock(return_value={"ok": True})

        with (
            patch.object(wechat_api, "IS_HOOK", False),
            patch.object(wechat_api.protocol_api, "send_text", send),
            wechat_api.use_agent("SESSION_456"),
        ):
            result = await wechat_api.send_text("wxid_target", "reply")

        self.assertEqual({"ok": True}, result)
        send.assert_awaited_once_with("SESSION_456", "wxid_target", "reply")

    async def test_protocol_client_rejects_missing_session(self):
        post = AsyncMock(return_value={"ok": True})

        with patch.object(protocol_api, "_post", post):
            result = await protocol_api.send_text("", "filehelper", "hello")

        self.assertFalse(result["ok"])
        self.assertIn("session_id", result["error"])
        post.assert_not_awaited()

    async def test_protocol_client_downloads_cdn_image(self):
        post = AsyncMock(return_value={"ok": True, "data": {"data": {"imageBase64": "abc"}}})

        with patch.object(protocol_api, "_post", post):
            result = await protocol_api.download_cdn_image("SESSION_123", "001122", "file-id")

        self.assertTrue(result["ok"])
        post.assert_awaited_once_with(
            "/cdndownload",
            {
                "session_id": "SESSION_123",
                "aeskey": "001122",
                "fileid": "file-id",
                "fileType": 1,
                "chatType": 0,
                "largesVideo": 0,
            },
            timeout=55.0,
        )


if __name__ == "__main__":
    unittest.main()
