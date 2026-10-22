"""Named reports use the documented Hook filename fields."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import wechat_api


class ReportFileSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_report_uses_normal_client_send_and_keeps_source(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "2026-07-01至2026-09-30_问题平台提单统计.xlsx"
            path.write_bytes(b"xlsx contents")
            post = AsyncMock(return_value=httpx.Response(200, json={"SendFileMsg": "1"}))
            with patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "IS_LOCAL_HOOK", True), patch.object(wechat_api, "_post", post):
                result = await wechat_api.send_file("filehelper", str(path), filename=path.name)
            self.assertEqual("1", result["SendFileMsg"])
            self.assertEqual("/SendFileMsg", post.call_args.args[0])
            body = post.call_args.kwargs["json"]
            self.assertEqual(str(path), body["filepath"])
            self.assertEqual("", body["fileData"])
            self.assertTrue(path.exists())


    async def test_ordinary_file_send_keeps_existing_endpoint(self):
        post = AsyncMock(return_value=httpx.Response(200, json={"SendFileMsg": "1"}))
        with patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "_post", post):
            await wechat_api.send_file("filehelper", "ordinary.txt", "74657874")
        self.assertEqual("/SendFileMsg", post.call_args.args[0])
        self.assertNotIn("fileName", post.call_args.kwargs["json"])
