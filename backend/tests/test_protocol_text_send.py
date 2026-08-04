import unittest
from unittest.mock import AsyncMock, patch

import httpx

import config as app_config
import login_remote_hook
import protocol_api
import wechat_api


class ProtocolTextSendTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_ws_setting_only_applies_to_remote_hook(self):
        self.assertFalse(app_config._resolve_agent_ws_enabled("local_hook", True))
        self.assertTrue(app_config._resolve_agent_ws_enabled("remote_hook", True))
        self.assertFalse(app_config._resolve_agent_ws_enabled("remote_hook", False))
        self.assertFalse(app_config._resolve_agent_ws_enabled("remote_protocol", True))

    def test_local_start_wechat_body_omits_remote_ws(self):
        with patch.object(login_remote_hook, "AGENT_WS_ENABLED", False):
            body = login_remote_hook._start_wechat_body()
        self.assertNotIn("RemoteWS", body)

    def test_remote_hook_start_wechat_body_includes_remote_ws(self):
        with (
            patch.object(login_remote_hook, "AGENT_WS_ENABLED", True),
            patch.object(login_remote_hook, "CLIENT_WSS_URL", "wss://example.com/agent"),
        ):
            body = login_remote_hook._start_wechat_body()
        self.assertEqual("wss://example.com/agent", body["RemoteWS"])

    def test_local_hook_never_uses_remote_agent_transport(self):
        with (
            patch.object(wechat_api, "AGENT_WS_ENABLED", True),
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "IS_LOCAL_HOOK", True),
        ):
            self.assertFalse(wechat_api._use_agent_ws_transport())

    def test_remote_hook_uses_agent_transport_when_enabled(self):
        with (
            patch.object(wechat_api, "AGENT_WS_ENABLED", True),
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "IS_LOCAL_HOOK", False),
        ):
            self.assertTrue(wechat_api._use_agent_ws_transport())

    def test_remote_hook_can_disable_agent_transport(self):
        with (
            patch.object(wechat_api, "AGENT_WS_ENABLED", False),
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "IS_LOCAL_HOOK", False),
        ):
            self.assertFalse(wechat_api._use_agent_ws_transport())

    def test_api_log_category_is_scoped(self):
        self.assertEqual("main", wechat_api._CURRENT_LOG_CATEGORY.get())
        with wechat_api.use_log_category("smart_reply"):
            self.assertEqual("smart_reply", wechat_api._CURRENT_LOG_CATEGORY.get())
        self.assertEqual("main", wechat_api._CURRENT_LOG_CATEGORY.get())

    async def test_api_log_uses_active_category(self):
        with (
            patch.object(wechat_api, "append_daily_log") as append_log,
            wechat_api.use_log_category("smart_reply"),
        ):
            await wechat_api._append_api_log("send details")

        append_log.assert_called_once_with("smart_reply", "send details")

    async def test_local_hook_text_uses_http_hook_contract(self):
        response = httpx.Response(200, json={"SendTextMsg": "1"})
        post = AsyncMock(return_value=response)

        with patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "_post", post):
            result = await wechat_api.send_text("filehelper", "hello")

        self.assertEqual({"SendTextMsg": "1"}, result)
        post.assert_awaited_once_with("/SendTextMsg", json={"toid": "filehelper", "msg": "hello"})

    async def test_local_hook_no_src_text_uses_http_hook_contract(self):
        response = httpx.Response(200, json={"SendTextMsg_NoSrc": "1"})
        post = AsyncMock(return_value=response)

        with patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "_post", post):
            result = await wechat_api.send_text_no_src("filehelper", "hello")

        self.assertEqual({"SendTextMsg_NoSrc": "1"}, result)
        post.assert_awaited_once_with("/SendTextMsg_NoSrc", json={"toid": "filehelper", "msg": "hello"})

    async def test_protocol_client_uses_newsendmsg_contract(self):
        post = AsyncMock(return_value={"ok": True})

        with patch.object(protocol_api, "_post", post):
            result = await protocol_api.send_text("SESSION_123", "filehelper", "hello")

        self.assertEqual({"ok": True}, result)
        post.assert_awaited_once_with(
            "/newsendmsg",
            {
                "session_id": "SESSION_123",
                "userName": "filehelper",
                "content": "hello",
                "msgType": 1,
                "async": 0,
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
