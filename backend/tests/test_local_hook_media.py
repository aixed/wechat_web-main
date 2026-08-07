import os
import tempfile
import time
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

    async def test_generic_callback_path_is_mapped_by_message_type(self):
        cases = {
            "3": "img_path",
            "34": "voice_path",
            "43": "video_path",
            "49": "file_path",
        }
        for msgtype, field in cases.items():
            with self.subTest(msgtype=msgtype):
                _, normalized = main._normalize_callback_message({
                    "msgtype": msgtype,
                    "msgsvrid": f"message-{msgtype}",
                    "fromid": "wxid_friend",
                    "path": rf"C:\Media\message-{msgtype}.bin",
                }, "2", "wxid_self")
                self.assertEqual(rf"C:\Media\message-{msgtype}.bin", normalized[field])

    async def test_revoke_voice_call_placeholder_is_filtered(self):
        message = {
            "msgtype": "1",
            "msg": "system message revoke/voice call",
            "fromid": "wxid_friend",
        }

        self.assertTrue(main._is_callback_status_echo(message, "2", "wxid_self"))

    async def test_message_struct_falls_back_to_cached_recalled_text(self):
        with (
            patch.object(
                wechat_api,
                "get_msg_struct",
                AsyncMock(return_value={
                    "msgtype": "10000",
                    "content": "<revokemsg>你撤回了一条消息</revokemsg>",
                }),
            ),
            patch.object(main, "_cached_recalled_text", return_value="撤回前的原消息"),
        ):
            result = await main.get_message_struct(main.MessageStructRequest(
                msg_id="3015177577691070980",
                chat_id="wxid_friend",
                timestamp=int(time.time()),
            ))

        self.assertTrue(result["ok"])
        self.assertEqual("撤回前的原消息", result["content"])
        self.assertEqual("message_cache", result["source"])

    async def test_message_struct_rejects_re_edit_after_two_minutes(self):
        with patch.object(wechat_api, "get_msg_struct", AsyncMock()) as get_struct:
            result = await main.get_message_struct(main.MessageStructRequest(
                msg_id="3015177577691070980",
                chat_id="wxid_friend",
                timestamp=int(time.time()) - 121,
            ))

        self.assertFalse(result["ok"])
        self.assertEqual("re_edit_expired", result["error"])
        get_struct.assert_not_awaited()

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

    async def test_history_file_matches_recvtype_one_path_by_name_and_time(self):
        history = {
            "id": "database-id",
            "msgtype": "49",
            "msg": "<msg><appmsg><title>report.pdf</title><type>6</type><appattach><totallen>12</totallen></appattach></appmsg></msg>",
            "timestamp": 100,
            "sendorrecv": "2",
        }
        callback = {
            "id": "callback-id",
            "msgtype": "49",
            "timestamp": 100,
            "sendorrecv": "2",
            "file_path": r"C:\Downloads\report.pdf",
        }

        self.assertEqual(callback, main._find_history_file_media(history, [callback]))

    async def test_generic_cdn_download_uses_requested_path(self):
        response = httpx.Response(
            200,
            json={"ret": 0, "retmsg": "ok"},
            request=httpx.Request("POST", "http://127.0.0.1:30001/download"),
        )
        with (
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "IS_LOCAL_HOOK", True),
            patch.object(wechat_api, "_post", AsyncMock(return_value=response)) as post,
        ):
            result = await wechat_api.cdn_download("aes-key", "file-id", r"C:\Cache\report.pdf")

        post.assert_awaited_once_with(
            "/download",
            json={
                "savePath": r"C:\Cache\report.pdf",
                "aeskey": "aes-key",
                "fileid": "file-id",
                "chatType": 0,
                "largesVideo": 0,
                "fileType": 2,
            },
            timeout=120.0,
            bypass_circuit_breaker=True,
        )
        self.assertEqual(r"C:\Cache\report.pdf", result["requested_save_path"])

    async def test_send_voice_uses_hook_payload(self):
        response = httpx.Response(
            200,
            json={"ret": 1},
            request=httpx.Request("POST", "http://127.0.0.1:30001/SendVoiceMsg"),
        )
        with (
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "IS_LOCAL_HOOK", True),
            patch.object(wechat_api, "_post", AsyncMock(return_value=response)) as post,
        ):
            await wechat_api.send_voice("filehelper", r"C:\Voice\sample.silk", 2400, "abcd")

        post.assert_awaited_once_with(
            "/SendVoiceMsg",
            json={
                "toid": "filehelper",
                "voice_file": r"C:\Voice\sample.silk",
                "time_ms": 2400,
                "fileData": "abcd",
            },
            timeout=120.0,
        )

    async def test_download_voice_uses_numeric_message_id(self):
        response = httpx.Response(
            200,
            json={"voice_hex": "0223"},
            request=httpx.Request("POST", "http://127.0.0.1:30001/DownloadVoice"),
        )
        with (
            patch.object(wechat_api, "IS_HOOK", True),
            patch.object(wechat_api, "IS_LOCAL_HOOK", True),
            patch.object(wechat_api, "_post", AsyncMock(return_value=response)) as post,
        ):
            await wechat_api.download_voice("client-id", 3737, "group@chatroom", "6176870848319479859")

        post.assert_awaited_once_with(
            "/DownloadVoice",
            json={
                "clientmsgid": "client-id",
                "length": 3737,
                "fromgid": "group@chatroom",
                "msgsvrid": 6176870848319479859,
            },
            timeout=30.0,
        )

    async def test_media_resolver_prefers_existing_local_path(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as media_file:
            media_file.write(b"local-file")
            media_path = media_file.name
        try:
            with (
                patch.object(main, "_persist_media_path") as persist,
                patch.object(wechat_api, "cdn_download", AsyncMock()) as download,
            ):
                path, name = await main._resolve_media_path(
                    msg_id="message-1",
                    msg_type="49",
                    local_path=media_path,
                    msg_xml="",
                    filename="report.pdf",
                )
            self.assertEqual(media_path, path)
            self.assertEqual("report.pdf", name)
            persist.assert_called_once_with("message-1", "49", media_path)
            download.assert_not_awaited()
        finally:
            os.unlink(media_path)

    async def test_media_resolver_downloads_when_local_path_is_missing(self):
        xml = (
            "<msg><appmsg><title>report.pdf</title><appattach>"
            "<cdnattachurl>file-id</cdnattachurl><aeskey>file-key</aeskey>"
            "</appattach></appmsg></msg>"
        )
        with tempfile.TemporaryDirectory() as cache_dir:
            async def download(_aeskey, _fileid, save_path, **_kwargs):
                with open(save_path, "wb") as output:
                    output.write(b"downloaded-file")
                return {"ret": 0, "savePath": save_path}

            with (
                patch.object(main, "_MEDIA_CACHE_DIR", cache_dir),
                patch.object(main.config, "IS_LOCAL_HOOK", True),
                patch.object(main, "_cached_media_path", return_value=""),
                patch.object(main, "_persist_media_path") as persist,
                patch.object(wechat_api, "cdn_download", side_effect=download) as cdn_download,
            ):
                path, name = await main._resolve_media_path(
                    msg_id="message-1",
                    msg_type="49",
                    local_path=r"C:\Missing\report.pdf",
                    msg_xml=xml,
                    filename="report.pdf",
                )

            self.assertTrue(os.path.isfile(path))
            self.assertEqual("report.pdf", name)
            cdn_download.assert_awaited_once()
            persist.assert_called_once_with("message-1", "49", path)

    async def test_file_and_video_cdn_params_are_parsed(self):
        file_params = main._parse_media_download_params(
            "49",
            "<msg><appmsg><title>report.pdf</title><appattach><cdnattachurl>file-id</cdnattachurl><aeskey>file-key</aeskey></appattach></appmsg></msg>",
        )
        video_params = main._parse_media_download_params(
            "43",
            '<msg><videomsg aeskey="video-key" cdnvideourl="video-id" /></msg>',
        )
        self.assertEqual(("file-key", "file-id", "report.pdf"), (
            file_params["aeskey"], file_params["fileid"], file_params["filename"],
        ))
        self.assertEqual(("video-key", "video-id"), (video_params["aeskey"], video_params["fileid"]))


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

    def test_sqlite_updates_and_reads_generic_media_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SqliteMessageCache(os.path.join(temp_dir, "messages.sqlite3"))
            cache.upsert_messages(
                "wxid_friend",
                [{"id": "message-1", "msgtype": "49", "msg": "file", "timestamp": 100}],
                owner_wxid="wxid_self",
            )

            updated = cache.update_media_path_by_msg_id(
                "message-1",
                "file_path",
                r"C:\Temp\report.pdf",
                owner_wxid="wxid_self",
            )
            stored = cache.get_message_by_id("message-1", owner_wxid="wxid_self")

            self.assertEqual(1, updated)
            self.assertEqual(r"C:\Temp\report.pdf", stored["file_path"])


class LocalDataSearchTests(unittest.TestCase):
    def test_cached_search_finds_contacts_group_members_and_message_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SqliteMessageCache(os.path.join(temp_dir, "messages.sqlite3"))
            owner_wxid = "wxid_self"
            cache.upsert_contacts({
                "wxid_xed": {
                    "name": "Xed",
                    "profile": {"Alias": "thexed", "NickName": "Xed"},
                },
                "group@chatroom": {
                    "name": "测试群",
                    "is_group": True,
                    "profile": {"NickName": "测试群"},
                },
            }, owner_wxid=owner_wxid)
            cache.upsert_group_members(
                "group@chatroom",
                [{"wxid": "wxid_xed", "name": "Xed", "profile": {"Alias": "thexed"}}],
                owner_wxid=owner_wxid,
            )
            cache.upsert_messages(
                "group@chatroom",
                [{"id": "message-1", "msgtype": "1", "msg": "需求 40399 已处理", "timestamp": 100}],
                owner_wxid=owner_wxid,
            )

            with patch.object(main, "sqlite_cache", cache):
                contacts, groups, _ = main._cached_search_results("thexed", 30, owner_wxid)
                _, _, messages = main._cached_search_results("40399", 30, owner_wxid)

            self.assertEqual(["wxid_xed"], [item["wxid"] for item in contacts])
            self.assertEqual("group@chatroom", groups[0]["wxid"])
            self.assertEqual("Xed", groups[0]["matched_members"][0]["name"])
            self.assertEqual("group@chatroom", messages[0]["wxid"])
            self.assertEqual(1, messages[0]["match_count"])


if __name__ == "__main__":
    unittest.main()
