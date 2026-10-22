import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from fastapi.responses import FileResponse
import main
from sqlite_cache import SqliteMessageCache


class QuoteImageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.image = self.folder / "original.png"
        Image.new("RGB", (20, 30), "white").save(self.image)
        self.cache = SqliteMessageCache(str(self.folder / "cache.sqlite3"))
        for p in (patch.object(main, "sqlite_cache", self.cache), patch.object(main, "_contact_owner_wxid", return_value="owner"),
            patch.object(main, "_IMG_CACHE_DIR", str(self.folder / "images"))):
            p.start(); self.addCleanup(p.stop)

    async def test_placeholder_quote_fetches_original_structure_and_uses_svrid_download(self):
        original = '<msg><img aeskey="key" cdnmidimgurl="file-id"/></msg>'
        with (patch.object(main.config, "IS_HOOK", True), patch.object(main.config, "IS_PROTOCOL", False),
            patch.object(main.wechat_api, "get_msg_struct", AsyncMock(return_value={"content": original, "msgtype": "3"})) as struct,
            patch.object(main.wechat_api, "down_file_or_pic", AsyncMock(return_value={"savepath": str(self.image)})) as download,
            patch.object(main.wechat_api, "cdn_download_pic", AsyncMock()) as cdn):
            result = await main._download_image_payload({"msg_id": "900", "msg_xml": "[图片]", "quoted": True}, "agent")
        self.assertIsInstance(result, FileResponse)
        self.assertEqual(self.image.read_bytes(), Path(result.path).read_bytes())
        struct.assert_awaited_once_with("900"); download.assert_awaited_once_with("900"); cdn.assert_not_awaited()

    async def test_original_image_cache_is_reused_without_network(self):
        self.cache.upsert_messages("room@chatroom", [{"id": "900", "msgtype": "3", "img_path": str(self.image)}], owner_wxid="owner")
        with patch.object(main.wechat_api, "get_msg_struct", AsyncMock()) as struct, patch.object(main.wechat_api, "down_file_or_pic", AsyncMock()) as download:
            result = await main._download_image_payload({"msg_id": "900", "msg_xml": "", "quoted": True}, "agent")
        self.assertIsInstance(result, FileResponse)
        struct.assert_not_awaited(); download.assert_not_awaited()

    async def test_frontend_and_workflow_share_one_quote_download(self):
        async def download(_):
            await asyncio.sleep(.02)
            return {"savepath": str(self.image)}
        with patch.object(main.wechat_api, "down_file_or_pic", AsyncMock(side_effect=download)) as request:
            paths = await asyncio.gather(*(main._download_quoted_image_by_id("900", str(self.folder / "download.png"), "agent") for _ in range(2)))
        self.assertEqual(paths[0], paths[1]); self.assertTrue(paths[0])
        request.assert_awaited_once_with("900")
