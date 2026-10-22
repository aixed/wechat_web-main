import base64
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

import config
import hook_login
import main
import wechat_api


class HookLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original_state, original_active = main.app_state, main._active_agent_id
        self.addCleanup(lambda: setattr(main, "app_state", original_state))
        self.addCleanup(lambda: setattr(main, "_active_agent_id", original_active))
        self.requests = []
        self.occupied = "0"
        self.start_result = {"success": "1", "进程ID": "20152"}
        self.image = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(self.image, format="PNG")
        def handler(request):
            self.requests.append(request)
            if request.url.path == "/Get_PortOccupiedInfo":
                return httpx.Response(200, json={"Occupied": self.occupied})
            if request.url.path == "/Get_WeChatProcessNumber":
                return httpx.Response(200, json={"List": []})
            if request.url.path == "/StartWechat":
                return httpx.Response(200, json=self.start_result)
            if request.url.path == "/IsLoginStatus":
                return httpx.Response(200, json={"onlinestatus": "0", "msg": "等待扫码"})
            if request.url.path == "/GetLoginQRCode":
                return httpx.Response(200, content=self.image.getvalue(), headers={"content-type": "image/png"})
            return httpx.Response(200, json={})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.client.aclose)
        for p in (patch.object(hook_login, "client", self.client),
            patch.object(hook_login, "REGISTRY_PATH", Path(self.temp.name) / "instances.json"),
            patch.object(config, "IS_HOOK", True), patch.object(config, "IS_PROTOCOL", False),
            patch.object(config, "IS_LOCAL_HOOK", True), patch.object(config, "AGENT_WS_ENABLED", False),
            patch.object(config, "LOGIN_MODE", "local_hook"), patch.object(config, "HOOK_HOST", "127.0.0.1"),
            patch.object(config, "HOOK_PORT", 30001), patch.object(config, "MGR_PORT", 29999),
            patch.object(config, "MGR_BASE_URL", "http://127.0.0.1:29999"),
            patch.object(config, "HOOK_WECHAT_PATH", "")):
            p.start(); self.addCleanup(p.stop)

    async def start(self, **overrides):
        return await hook_login.start(**{"start_port": 30002, "rdv": "127f78ad", "wechat_path": "C:\\WeChat\\", **overrides})

    async def test_local_hook_posts_manager_schema_and_accepts_pid_without_protocol_session(self):
        record = await self.start()
        request = self.requests[-1]
        self.assertEqual("http://127.0.0.1:29999/StartWechat", str(request.url))
        body = json.loads(request.content)
        self.assertEqual("30002", body["StartPort"])
        self.assertEqual("127f78ad", body["RDV"])
        self.assertEqual("C:\\WeChat\\", body["WeChatPath"])
        self.assertNotIn("RemoteWS", body)
        self.assertNotIn("session_id", body)
        self.assertEqual("20152", record["pid"])
        self.assertEqual(record, hook_login.instance(record["id"]))

    async def test_remote_hook_passes_remote_ws_and_omits_http_callback(self):
        with patch.object(config, "IS_LOCAL_HOOK", False), patch.object(config, "AGENT_WS_ENABLED", True), patch.object(config, "LOGIN_MODE", "remote_hook"):
            record = await self.start(remote_ws="ws://127.0.0.1:5000/agent")
            body = json.loads(self.requests[-1].content)
            self.assertEqual("ws://127.0.0.1:5000/agent", body["RemoteWS"])
            self.assertNotIn("CallBackURL", body)
            self.assertEqual("ws", record["transport"])

    async def test_occupied_port_stops_before_start_and_bad_pid_is_not_registered(self):
        self.occupied = "1"
        with patch.object(hook_login, '_tcp_port_is_listening', AsyncMock(return_value=True)), self.assertRaisesRegex(ValueError, "占用"):
            await self.start()
        self.assertNotIn('/StartWechat', [r.url.path for r in self.requests])
        self.occupied = "0"
        self.start_result = {"success": "1", "session_id": "protocol-only"}
        with self.assertRaisesRegex(ValueError, "进程"):
            await self.start()
        self.assertEqual([], hook_login.instances())

    async def test_manager_time_wait_residue_does_not_block_launch(self):
        self.occupied = '1'
        with patch.object(hook_login, '_tcp_port_is_listening', AsyncMock(return_value=False)) as listening:
            record = await self.start()
        self.assertEqual('20152', record['pid'])
        listening.assert_awaited_once_with(30002)
        self.assertEqual('/StartWechat', self.requests[-1].url.path)

    async def test_live_wechat_port_cannot_be_overridden_by_failed_tcp_probe(self):
        self.occupied = '1'
        processes = [{'PID': 99, 'Port': 0, 'Par': 'StartPort=30002&RDV=127f78ad'}]
        with patch.object(hook_login, '_wechat_processes', AsyncMock(return_value=processes)), \
             patch.object(hook_login, '_tcp_port_is_listening', AsyncMock(return_value=False)) as listening:
            with self.assertRaisesRegex(ValueError, '占用'):
                await self.start()
        listening.assert_not_awaited()
        self.assertNotIn('/StartWechat', [r.url.path for r in self.requests])

    async def test_unconfirmed_manager_status_never_starts_a_process(self):
        self.occupied = 'unknown'
        with self.assertRaisesRegex(ValueError, '端口状态'):
            await self.start()
        self.occupied = '1'
        with patch.object(hook_login, '_wechat_processes', AsyncMock(side_effect=ValueError('进程列表无效'))):
            with self.assertRaisesRegex(ValueError, '进程列表'):
                await self.start()
        self.assertNotIn('/StartWechat', [r.url.path for r in self.requests])

    async def test_tcp_probe_requires_explicit_refusal_to_ignore_manager_occupancy(self):
        for error in (ConnectionRefusedError(), TimeoutError(), OSError('network unreachable')):
            with self.subTest(error=type(error).__name__), \
                 patch.object(hook_login.asyncio, 'open_connection', AsyncMock(side_effect=error)):
                if isinstance(error, ConnectionRefusedError):
                    self.assertFalse(await hook_login._tcp_port_is_listening(30002))
                else:
                    with self.assertRaisesRegex(ValueError, '网络'):
                        await hook_login._tcp_port_is_listening(30002)
        writer = AsyncMock()
        writer.close = Mock()
        with patch.object(hook_login.asyncio, 'open_connection', AsyncMock(return_value=(None, writer))):
            self.assertTrue(await hook_login._tcp_port_is_listening(30002))
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    async def test_termination_confirms_process_exit_despite_port_residue_and_connection_reset(self):
        profile = {'port': 30002, 'host': '127.0.0.1', 'transport': 'http'}
        self.occupied = '1'
        with patch.object(hook_login, 'post', AsyncMock(side_effect=httpx.ReadError('instance closed connection'))) as terminate:
            await hook_login._stop_process(profile, {'PID': 99})
        terminate.assert_awaited_once_with(profile, '/TerminateThisWeChat')
        self.assertEqual(['/Get_WeChatProcessNumber'], [r.url.path for r in self.requests])

    async def test_live_process_or_malformed_list_is_not_reported_as_stopped(self):
        profile = {'port': 30002, 'host': '127.0.0.1', 'transport': 'http'}
        with patch.object(hook_login, '_wechat_processes', AsyncMock(return_value=[{'PID': 99}])) as processes, \
             patch.object(hook_login.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(ValueError, '尚未关闭'):
                await hook_login._stop_process(profile, {'PID': 99})
        self.assertEqual(20, processes.await_count)
        with patch.object(hook_login.client, 'post', AsyncMock(return_value=httpx.Response(200,
                request=httpx.Request('POST', 'http://test'), json={'List': [{}]}))):
            with self.assertRaisesRegex(ValueError, '进程列表'):
                await hook_login._stop_process(profile, {'PID': 99})

    async def test_invalid_rdv_and_mixed_remote_ws_never_start_a_process(self):
        for arguments in ({"rdv": "not-hex"}, {"rdv": "ffffffff"}, {"remote_ws": "ws://example.test/agent"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.start(**arguments)
        self.assertEqual([], self.requests)

    async def test_hook_status_and_qrcode_use_new_instance_api_port_without_session_id(self):
        record = await self.start()
        status = await main.hook_login_status(record["id"])
        self.assertEqual("0", status["account"]["login_status"])
        qr = await main.hook_login_qrcode(record["id"], main.HookQRCodeRequest(refresh=True))
        self.assertTrue(qr["ok"])
        self.assertTrue(qr["qrcode"].startswith("data:image/png;base64,"))
        self.assertEqual(["/IsLoginStatus", "/RefreshLoginQRCode", "/GetLoginQRCode"], [r.url.path for r in self.requests[-3:]])
        self.assertTrue(all(r.url.port == 30002 for r in self.requests[-3:]))
        self.assertTrue(all("session_id" not in json.loads(r.content) for r in self.requests))

    def test_qrcode_accepts_binary_base64_and_hex_and_rejects_non_images(self):
        raw = self.image.getvalue()
        for response in (httpx.Response(200, content=raw, headers={"content-type": "image/png"}),
            httpx.Response(200, json={"base64": base64.b64encode(raw).decode()}),
            httpx.Response(200, json={"data": {"img_hex": raw.hex()}})):
            self.assertTrue(hook_login.qr_image(response).startswith("data:image/png;base64,"))
        with self.assertRaises((ValueError, OSError)):
            hook_login.qr_image(httpx.Response(200, json={"base64": base64.b64encode(b"not-an-image").decode()}))

    async def test_registered_http_account_routes_business_api_to_selected_port(self):
        record = await self.start()
        with patch.object(wechat_api, "client", self.client), patch.object(wechat_api, "IS_HOOK", True), patch.object(wechat_api, "IS_LOCAL_HOOK", True), \
            patch.object(wechat_api, "_append_api_log", AsyncMock()):
            with wechat_api.use_agent(record["id"]):
                await wechat_api._post("/IsLoginStatus", json={}, bypass_circuit_breaker=True)
        self.assertEqual(30002, self.requests[-1].url.port)

    async def test_modes_cannot_call_each_others_login_routes(self):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"X-Access-Key": config.WEB_ACCESS_KEY}) as client:
            with patch.object(main.protocol_api, "start_wechat", AsyncMock()) as protocol_start:
                response = await client.post("/api/protocol/start-wechat", json={"rdv": "127f78ad"})
                self.assertEqual(409, response.status_code)
                protocol_start.assert_not_awaited()
            with patch.object(config, "IS_HOOK", False), patch.object(config, "IS_PROTOCOL", True):
                response = await client.post("/api/hook/start-wechat", json={"start_port": 30002, "rdv": "127f78ad"})
                self.assertEqual(409, response.status_code)
        self.assertEqual([], self.requests)

    async def test_unknown_instance_cannot_request_an_arbitrary_api_port(self):
        with self.assertRaises(main.HTTPException) as failure:
            await main.hook_login_status("hook_54321")
        self.assertEqual(404, failure.exception.status_code)
        self.assertEqual([], self.requests)

    async def test_authenticated_requests_keep_the_selected_hook_account_port(self):
        record = await self.start()
        with patch.object(wechat_api, "client", self.client), patch.object(wechat_api, "_append_api_log", AsyncMock()):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test", headers={
                "X-Access-Key": config.WEB_ACCESS_KEY, "X-Agent-Id": record["id"]}) as client:
                response = await client.get("/api/self")
        self.assertEqual(200, response.status_code)
        self.assertEqual("/GetSelfProfile", self.requests[-1].url.path)
        self.assertEqual(30002, self.requests[-1].url.port)

    async def test_switching_back_to_default_probes_target_instead_of_previous_account(self):
        contexts = []
        async def probe():
            contexts.append(wechat_api._CURRENT_AGENT_ID.get())
            return {"id": "default", "login_status": "3"}
        with patch.object(main, "app_state", {"initialized": True}), \
            patch.object(main, "_probe_local_hook_account", AsyncMock(side_effect=probe)), \
            patch.object(main, "_activate_runtime", return_value="default"):
            with wechat_api.use_agent("hook_30002"):
                result = await main.activate_account(main.ActivateAccountRequest(agent_id="default"))
        self.assertTrue(result["ok"])
        self.assertEqual(["default", "default"], contexts)

    async def test_closed_instance_can_reuse_rdv_without_keeping_two_stale_records(self):
        await self.start()
        self.occupied = '1'
        with patch.object(hook_login, '_tcp_port_is_listening', AsyncMock(return_value=False)):
            replacement = await self.start(start_port=30003)
        self.assertEqual([replacement], hook_login.instances())

    async def test_manager_timeout_is_not_retried_or_recorded_as_success(self):
        async def timeout(request):
            self.requests.append(request)
            if request.url.path == "/Get_PortOccupiedInfo":
                return httpx.Response(200, json={"Occupied": "0"})
            raise httpx.ReadTimeout("no confirmed result", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with patch.object(hook_login, "client", client):
                with self.assertRaises(main.HTTPException) as failed:
                    await main.start_hook_wechat(main.HookStartWechatRequest(start_port=30002, rdv="127f78ad"))
        self.assertEqual(502, failed.exception.status_code)
        self.assertEqual(1, sum(r.url.path == "/StartWechat" for r in self.requests))
        self.assertEqual([], hook_login.instances())

    async def test_remote_qrcode_waits_for_agent_then_uses_hook_route(self):
        record = {"id": "hook_30002", "port": 30002, "transport": "ws"}
        with patch.object(hook_login, "connected_agent", return_value=None):
            with self.assertRaises(ConnectionError):
                await hook_login.post(record, "/GetLoginQRCode")
        response = SimpleNamespace(status=200, body=self.image.getvalue(), content_type="image/png")
        with patch.object(hook_login, "connected_agent", return_value={"id": "remote-agent"}), \
            patch.object(hook_login.agent_manager, "request", AsyncMock(return_value=response)) as request:
            raw = await hook_login.post(record, "/GetLoginQRCode")
        self.assertTrue(hook_login.qr_image(raw))
        self.assertEqual("GetLoginQRCode", request.call_args.args[0])
        self.assertEqual("remote-agent", request.call_args.kwargs["agent_id"])


if __name__ == "__main__":
    unittest.main()
