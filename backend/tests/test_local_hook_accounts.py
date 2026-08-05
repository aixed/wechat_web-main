import unittest
from unittest.mock import AsyncMock, patch

import main


class LocalHookAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_app_state = main.app_state
        self.old_active_agent_id = main._active_agent_id
        self.old_last_seen_at = main._local_hook_last_seen_at
        self.old_profile_refreshed = set(main._agent_self_profile_refreshed)
        main.app_state = main._new_app_state()
        main._active_agent_id = main._LOCAL_HOOK_ACCOUNT_ID
        main._local_hook_last_seen_at = 0.0
        main._agent_self_profile_refreshed.discard(main._LOCAL_HOOK_ACCOUNT_ID)

    def tearDown(self):
        main.app_state = self.old_app_state
        main._active_agent_id = self.old_active_agent_id
        main._local_hook_last_seen_at = self.old_last_seen_at
        main._agent_self_profile_refreshed.clear()
        main._agent_self_profile_refreshed.update(self.old_profile_refreshed)

    async def test_probe_uses_configured_http_port_and_login_status(self):
        main.app_state["initialized"] = True
        main.app_state["self_info"] = {
            "wxid": "wxid_local",
            "nickname": "Partial Local User",
        }
        status = {
            "onlinestatus": "3",
            "msg": "登陆完成！",
            "selfwxid": "wxid_local",
            "nickname": "Local User",
        }
        profile = {
            "wxid": "wxid_local",
            "nickname": "Local User",
            "head_big": "https://example.test/avatar.jpg",
        }

        with (
            patch.object(main.config, "HOOK_HOST", "127.0.0.1"),
            patch.object(main.config, "HOOK_PORT", 30123),
            patch.object(main.wechat_api, "is_login_status", AsyncMock(return_value=status)) as get_status,
            patch.object(main.wechat_api, "get_self_info", AsyncMock(return_value=profile)) as get_profile,
        ):
            account = await main._probe_local_hook_account()

        get_status.assert_awaited_once_with()
        get_profile.assert_awaited_once_with()
        self.assertTrue(account["port_alive"])
        self.assertTrue(account["connected"])
        self.assertEqual("3", account["login_status"])
        self.assertEqual("127.0.0.1:30123", account["peer"])
        self.assertEqual(30123, account["api_port"])
        self.assertEqual("http", account["transport"])
        self.assertEqual("wxid_local", account["wxid"])
        self.assertEqual("Local User", account["nickname"])
        self.assertEqual("https://example.test/avatar.jpg", account["avatar"])
        self.assertEqual(profile, account["profile"])
        self.assertEqual(
            "https://example.test/avatar.jpg",
            main.app_state["avatar_urls"]["wxid_local"],
        )
        self.assertTrue(account["initialized"])

    async def test_probe_reuses_complete_profile_during_same_login(self):
        main.app_state["self_info"] = {
            "wxid": "wxid_local",
            "nickname": "Local User",
            "head_big": "https://example.test/avatar.jpg",
        }
        main._agent_self_profile_refreshed.add(main._LOCAL_HOOK_ACCOUNT_ID)
        status = {
            "onlinestatus": "3",
            "selfwxid": "wxid_local",
            "nickname": "Local User",
        }

        with (
            patch.object(main.wechat_api, "is_login_status", AsyncMock(return_value=status)),
            patch.object(main.wechat_api, "get_self_info", AsyncMock()) as get_profile,
        ):
            account = await main._probe_local_hook_account()

        get_profile.assert_not_awaited()
        self.assertEqual("https://example.test/avatar.jpg", account["avatar"])

    async def test_probe_refreshes_profile_after_login_state_resets(self):
        main._agent_self_profile_refreshed.add(main._LOCAL_HOOK_ACCOUNT_ID)
        offline_status = {"onlinestatus": "0", "msg": "未登录"}
        online_status = {
            "onlinestatus": "3",
            "selfwxid": "wxid_new",
            "nickname": "New User",
        }
        profile = {
            "wxid": "wxid_new",
            "nickname": "New User",
            "head_big": "https://example.test/new-avatar.jpg",
            "account": "new-account",
        }

        with (
            patch.object(
                main.wechat_api,
                "is_login_status",
                AsyncMock(side_effect=[offline_status, online_status]),
            ),
            patch.object(main.wechat_api, "get_self_info", AsyncMock(return_value=profile)) as get_profile,
        ):
            await main._probe_local_hook_account()
            account = await main._probe_local_hook_account()

        get_profile.assert_awaited_once_with()
        self.assertEqual("wxid_new", account["wxid"])
        self.assertEqual("new-account", account["wechat_account"])

    async def test_probe_returns_configured_port_when_hook_is_offline(self):
        with (
            patch.object(main.config, "HOOK_HOST", "127.0.0.1"),
            patch.object(main.config, "HOOK_PORT", 30001),
            patch.object(
                main.wechat_api,
                "is_login_status",
                AsyncMock(side_effect=ConnectionError("connection refused")),
            ),
            patch.object(main.wechat_api, "get_self_info", AsyncMock()) as get_profile,
        ):
            account = await main._probe_local_hook_account()

        get_profile.assert_not_awaited()
        self.assertFalse(account["port_alive"])
        self.assertFalse(account["connected"])
        self.assertEqual("", account["login_status"])
        self.assertEqual("本地 Hook 接口不可用", account["login_message"])
        self.assertEqual(30001, account["start_port"])

    async def test_accounts_endpoint_returns_local_http_probe(self):
        account = {
            "id": main._LOCAL_HOOK_ACCOUNT_ID,
            "active": True,
            "port_alive": True,
            "login_status": "3",
        }
        with (
            patch.object(main.config, "IS_PROTOCOL", False),
            patch.object(main.config, "IS_LOCAL_HOOK", True),
            patch.object(main.config, "HOOK_HOST", "127.0.0.1"),
            patch.object(main.config, "HOOK_PORT", 30001),
            patch.object(main, "_probe_local_hook_account", AsyncMock(return_value=account)),
        ):
            result = await main.list_accounts()

        self.assertEqual("hook", result["source"])
        self.assertEqual("http", result["transport"])
        self.assertEqual("http://127.0.0.1:30001", result["configured_endpoint"])
        self.assertEqual([account], result["accounts"])

    async def test_local_account_activation_does_not_require_agent_ws(self):
        account = {
            "id": main._LOCAL_HOOK_ACCOUNT_ID,
            "active": True,
            "port_alive": True,
            "login_status": "3",
            "initialized": True,
        }
        main.app_state["initialized"] = True
        with (
            patch.object(main.config, "IS_PROTOCOL", False),
            patch.object(main.config, "IS_LOCAL_HOOK", True),
            patch.object(main, "_probe_local_hook_account", AsyncMock(return_value=account)) as probe,
            patch.object(main, "_activate_runtime", return_value=main._LOCAL_HOOK_ACCOUNT_ID),
            patch.object(main.agent_manager, "is_connected", return_value=False),
        ):
            result = await main.activate_account(main.ActivateAccountRequest(agent_id=main._LOCAL_HOOK_ACCOUNT_ID))

        self.assertTrue(result["ok"])
        self.assertEqual(main._LOCAL_HOOK_ACCOUNT_ID, result["active_id"])
        self.assertEqual(2, probe.await_count)


if __name__ == "__main__":
    unittest.main()
