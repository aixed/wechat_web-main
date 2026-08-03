import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main
from message_store import MessageStore
from sqlite_cache import SqliteMessageCache


class ProtocolHistoryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cache = main.sqlite_cache
        self.old_store = main.message_store
        self.old_app_state = main.app_state
        main.sqlite_cache = SqliteMessageCache(
            os.path.join(self.temp_dir.name, "wechat_protocol_cache.sqlite3")
        )
        main.message_store = MessageStore()
        main.app_state = main._new_app_state()

    async def asyncTearDown(self):
        main.sqlite_cache = self.old_cache
        main.message_store = self.old_store
        main.app_state = self.old_app_state
        self.temp_dir.cleanup()

    async def test_empty_protocol_history_does_not_query_hook_database(self):
        query_history = AsyncMock()
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "get_chat_history", query_history),
        ):
            result = await main.get_messages("wxid_friend", limit=20)

        self.assertEqual({"data": [], "source": "protocol_empty"}, result)
        query_history.assert_not_awaited()

    async def test_empty_protocol_session_refresh_uses_statusnotify_not_hook_database(self):
        query_sessions = AsyncMock()
        status_notify = AsyncMock(return_value={
            "ok": True,
            "statusnotify": {
                "base_response": {"ret": 0, "ret_signed": 0},
                "chat_contact_count": 0,
                "chat_contact_list": [],
            },
        })
        request = SimpleNamespace(headers={}, query_params={})
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main, "_query_session_list_from_db", query_sessions),
            patch.object(main.wechat_api, "status_notify", status_notify),
        ):
            result = await main.refresh_sessions(request)

        self.assertEqual("protocol_statusnotify", result["source"])
        self.assertEqual({"data": []}, result["sessions"])
        status_notify.assert_awaited_once_with(code=3, function_name="", function_arg="")
        query_sessions.assert_not_awaited()

    async def test_protocol_session_refresh_hydrates_names_avatars_and_pins(self):
        status_notify = AsyncMock(return_value={
            "ok": True,
            "statusnotify": {
                "base_response": {"ret": 0, "ret_signed": 0},
                "chat_contact_count": 3,
                "chat_contact_list": [
                    "wxid_normal",
                    "45996531138@chatroom",
                    "filehelper",
                ],
            },
        })
        batch_brief = AsyncMock(return_value={
            "ok": True,
            "batchgetcontactbriefinfo": {
                "base_response": {"ret": 0, "ret_signed": 0},
                "contacts": [
                    {
                        "user_name": "wxid_normal",
                        "ret": 0,
                        "contact": {
                            "user_name": "wxid_normal",
                            "nick_name": "Normal Nick",
                            "remark": "Normal Remark",
                            "small_head_img_url": "https://example.test/normal.jpg",
                            "bit_val": 3,
                        },
                    },
                    {
                        "user_name": "45996531138@chatroom",
                        "ret": 0,
                        "contact": {
                            "user_name": "45996531138@chatroom",
                            "nick_name": "Pinned Group",
                            "small_head_img_url": "https://example.test/group.jpg",
                            "bit_val": 2050,
                        },
                    },
                    {
                        "user_name": "filehelper",
                        "ret": 0,
                        "contact": {
                            "user_name": "filehelper",
                            "nick_name": "File Helper",
                            "small_head_img_url": "https://example.test/filehelper.jpg",
                            "bit_val": 8390657,
                        },
                    },
                ],
            },
        })
        request = SimpleNamespace(headers={"X-Agent-Id": "SESSION_1"}, query_params={})
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "status_notify", status_notify),
            patch.object(main.wechat_api, "batch_get_contact_brief_info", batch_brief),
        ):
            result = await main.refresh_sessions(request)

        rows = result["sessions"]["data"]
        self.assertEqual(
            ["wxid_normal", "45996531138@chatroom", "filehelper"],
            [row["strUsrName"] for row in rows],
        )
        self.assertEqual("Normal Remark", rows[0]["strNickName"])
        self.assertFalse(rows[0]["pinned"])
        self.assertTrue(rows[1]["pinned"])
        self.assertTrue(rows[2]["pinned"])
        self.assertEqual("Normal Remark", result["contact_profiles"]["wxid_normal"]["name"])
        self.assertEqual(
            "https://example.test/normal.jpg",
            result["contact_profiles"]["wxid_normal"]["avatar"],
        )
        batch_brief.assert_awaited_once()

    async def test_protocol_initcontact_matrix_is_loaded_once_and_all_ids_are_cached(self):
        init_contact = AsyncMock(return_value={
            "ok": True,
            "contact_count": 5,
            "group_count": 2,
            "initcontact": [
                ["wxid_friend", "gh_official", "123456@chatroom"],
                ["wxid_second", "wxid_friend"],
            ],
        })
        schedule_hydration = Mock()
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "init_contact", init_contact),
            patch.object(main, "_schedule_contact_detail_hydration", schedule_hydration),
        ):
            first = await main._refresh_contacts_incremental()
            second = await main._refresh_contacts_incremental()

        init_contact.assert_awaited_once_with()
        cached = main.sqlite_cache.get_contacts(owner_wxid="wxid_owner")
        self.assertEqual(
            {"wxid_friend", "gh_official", "123456@chatroom", "wxid_second"},
            set(cached),
        )
        self.assertEqual("3", first["count_friend"])
        self.assertEqual("1", first["count_chatroom"])
        self.assertEqual(first["count_friend"], second["count_friend"])
        self.assertGreaterEqual(schedule_hydration.call_count, 1)
        self.assertEqual(
            {"wxid_friend", "gh_official", "123456@chatroom", "wxid_second"},
            set(schedule_hydration.call_args_list[0].args[1]),
        )

    async def test_protocol_initcontact_completion_survives_backend_restart(self):
        main.sqlite_cache.upsert_contacts({
            "wxid_friend": {
                "wxid": "wxid_friend",
                "name": "Friend",
                "avatar": "https://example.test/friend.jpg",
                "profile": {"wxid": "wxid_friend"},
            },
        }, owner_wxid="wxid_owner")
        main.sqlite_cache.mark_contact_init_done_v2(owner_wxid="wxid_owner")
        main.app_state["contacts_loaded"] = False
        init_contact = AsyncMock()

        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "init_contact", init_contact),
        ):
            result = await main._refresh_contacts_incremental(hydrate_details=False)

        init_contact.assert_not_awaited()
        self.assertTrue(main.app_state["contacts_loaded"])
        self.assertEqual("1", result["count_friend"])

    def test_protocol_public_account_category_uses_service_type_only(self):
        self.assertEqual(
            "official",
            main._contact_profile_account_category({"service_type": 0}, "gh_official"),
        )
        self.assertEqual(
            "service",
            main._contact_profile_account_category({"service_type": 1}, "gh_service"),
        )
        self.assertEqual(
            "",
            main._contact_profile_account_category({"service_type": 2}, "gh_enterprise"),
        )
        self.assertEqual(
            "",
            main._contact_profile_account_category(
                {"bit_val": 3, "verify_flag": 24},
                "gh_ambiguous",
            ),
        )
        self.assertEqual(
            "",
            main._contact_profile_account_category(
                {"type": "service", "SourceText": "公众号"},
                "gh_text_marker",
            ),
        )
        self.assertFalse(
            main._contact_profile_needs_account_type({"service_type": 3}, "gh_internal")
        )

    async def test_protocol_contact_brief_batches_overlap_and_stay_within_100(self):
        active = 0
        max_active = 0
        requested_batches = []

        async def batch_brief(wxid_list):
            nonlocal active, max_active
            wxids = [value for value in wxid_list.split(",") if value]
            requested_batches.append(wxids)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {
                "ok": True,
                "batchgetcontactbriefinfo": {
                    "base_response": {"ret": 0, "ret_signed": 0},
                    "contacts": [
                        {
                            "user_name": wxid,
                            "ret": 0,
                            "contact": {
                                "user_name": wxid,
                                "nick_name": f"Name {wxid}",
                                "small_head_img_url": f"https://example.test/{wxid}.jpg",
                            },
                        }
                        for wxid in wxids
                    ],
                },
            }

        wxids = [f"wxid_{index}" for index in range(205)]
        with (
            patch.object(main.config, "HOOK_API_CONCURRENCY", 3),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "batch_get_contact_brief_info", side_effect=batch_brief),
        ):
            result = await main._fetch_and_cache_contact_details(
                wxids,
                owner_wxid="wxid_owner",
            )

        self.assertEqual(3, len(requested_batches))
        self.assertTrue(all(len(batch) <= 100 for batch in requested_batches))
        self.assertGreater(max_active, 1)
        self.assertEqual(set(wxids), set(result))

    async def test_protocol_history_reads_only_new_messages_from_protocol_sqlite(self):
        message = {
            "id": "protocol_1",
            "msgtype": "1",
            "msg": "new callback message",
            "timestamp": 200,
            "time_unix": 200,
            "sendorrecv": "2",
        }
        main.sqlite_cache.upsert_messages("wxid_friend", [message], owner_wxid="wxid_owner")
        query_history = AsyncMock()
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "get_chat_history", query_history),
        ):
            result = await main.get_messages("wxid_friend", limit=20)

        self.assertEqual("protocol_sqlite", result["source"])
        self.assertEqual([message], result["data"])
        query_history.assert_not_awaited()

    def test_protocol_internal_sync_events_are_not_user_messages(self):
        cases = [
            {
                "msgtype": "9999",
                "msg": "chatroom member changed",
                "fromid": "wxid_owner",
                "fromgid": "123456@chatroom",
            },
            {
                "msgtype": "10002",
                "msg": '<sysmsg type="dynacfg"><dynacfg /></sysmsg>',
                "fromid": "weixin",
            },
        ]
        with patch.object(main.config, "IS_PROTOCOL", True):
            for message in cases:
                with self.subTest(message=message):
                    self.assertTrue(main._is_protocol_internal_callback_message(message))

            self.assertFalse(main._is_protocol_internal_callback_message({
                "msgtype": "1",
                "msg": "normal group message",
                "fromid": "wxid_friend",
                "fromgid": "123456@chatroom",
            }))

        with patch.object(main.config, "IS_PROTOCOL", False):
            self.assertFalse(main._is_protocol_internal_callback_message(cases[0]))

    def test_getcontact_protocol_envelope_is_parsed(self):
        contacts = main._contacts_from_getcontact_response({
            "ok": True,
            "getcontact": {
                "base_response": {"ret": 0, "ret_signed": 0},
                "contacts": [{
                    "user_name": "wxid_nested_profile",
                    "nick_name": "Nested Profile",
                    "small_head_img_url": "https://example.test/nested.jpg",
                }],
            },
        })

        self.assertEqual(1, len(contacts))
        self.assertEqual("wxid_nested_profile", contacts[0]["user_name"])

    async def test_protocol_getcontact_uses_wxidorgid_payload(self):
        response = SimpleNamespace(json=lambda: {"ok": True, "getcontact": {"contacts": []}})
        post = AsyncMock(return_value=response)
        with (
            patch.object(main.wechat_api, "IS_HOOK", False),
            patch.object(main.wechat_api, "_post", post),
        ):
            await main.wechat_api.get_contact(["wxid_payload_test"])

        post.assert_awaited_once_with(
            "/getcontact",
            json={"wxidorgid": ["wxid_payload_test"]},
        )

    async def test_protocol_profile_batch_fetches_missing_profile_and_caches_it(self):
        get_contact = AsyncMock(return_value={
            "ok": True,
            "getcontact": {
                "base_response": {"ret": 0, "ret_signed": 0},
                "contacts": [{
                    "user_name": "wxid_profile_missing_test",
                    "nick_name": "Fetched Name",
                    "signature": "Fetched signature",
                    "small_head_img_url": "https://example.test/fetched.jpg",
                }],
            },
        })
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "get_contact", get_contact),
            patch.object(main, "_broadcast_contact_profile_updates", AsyncMock()),
        ):
            result = await main.post_contacts_profile_batch(main.ProfileBatchRequest(
                wxids=["wxid_profile_missing_test"],
            ))

        profile = result["members"]["wxid_profile_missing_test"]
        self.assertEqual("Fetched Name", profile["name"])
        self.assertEqual("https://example.test/fetched.jpg", profile["avatar"])
        self.assertEqual("Fetched signature", profile["profile"]["signature"])
        get_contact.assert_awaited_once_with(["wxid_profile_missing_test"])
        cached = main.sqlite_cache.get_contacts(
            ["wxid_profile_missing_test"],
            owner_wxid="wxid_owner",
        )
        self.assertEqual("Fetched Name", cached["wxid_profile_missing_test"]["name"])

    async def test_protocol_profile_batch_uses_sqlite_without_network(self):
        main.sqlite_cache.upsert_contacts({
            "wxid_profile_cached_test": {
                "wxid": "wxid_profile_cached_test",
                "name": "Cached Name",
                "avatar": "https://example.test/cached.jpg",
                "profile": {
                    "user_name": "wxid_profile_cached_test",
                    "nick_name": "Cached Name",
                    "small_head_img_url": "https://example.test/cached.jpg",
                },
            },
        }, owner_wxid="wxid_owner")
        get_contact = AsyncMock()
        with (
            patch.object(main.config, "IS_PROTOCOL", True),
            patch.object(main, "_contact_owner_wxid", return_value="wxid_owner"),
            patch.object(main.wechat_api, "get_contact", get_contact),
            patch.object(main, "_broadcast_contact_profile_updates", AsyncMock()),
        ):
            result = await main.post_contacts_profile_batch(main.ProfileBatchRequest(
                wxids=["wxid_profile_cached_test"],
            ))

        self.assertEqual("Cached Name", result["members"]["wxid_profile_cached_test"]["name"])
        get_contact.assert_not_awaited()

    async def test_protocol_getprofile_caches_self_profile(self):
        response = {
            "ok": True,
            "state": "logged_in",
            "profile": {
                "base_response": {"ret": 0, "ret_signed": 0},
                "user_name": "wxid_self_profile_test",
                "nick_name": "Self Name",
                "phone": "19900000000",
                "big_head_url": "https://example.test/self-big.jpg",
                "small_head_url": "https://example.test/self-small.jpg",
            },
        }
        with (
            patch.object(main, "_protocol_profile_for_session", AsyncMock(return_value=response)),
            patch.object(main, "_activate_runtime"),
        ):
            result = await main.get_protocol_profile(main.ProtocolSessionRequest(session_id="SESSION_PROFILE_TEST"))

        self.assertIs(result, response)
        cached = main.sqlite_cache.get_contacts(
            ["wxid_self_profile_test"],
            owner_wxid="wxid_self_profile_test",
        )["wxid_self_profile_test"]
        self.assertEqual("Self Name", cached["name"])
        self.assertEqual("https://example.test/self-small.jpg", cached["avatar"])
        self.assertEqual("19900000000", cached["profile"]["phone"])


if __name__ == "__main__":
    unittest.main()
