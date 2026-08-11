import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

import config
from mcp_service import McpService


class McpServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_tools_and_preserves_session_header(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            payload = json.loads(request.content or b"{}")
            if payload.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "session-1"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": "2025-03-26",
                            "serverInfo": {"name": "test-server", "version": "1"},
                            "capabilities": {"tools": {}},
                        },
                    },
                )
            if payload.get("method") == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [{
                            "name": "submit_data_maintenance",
                            "description": "submit",
                            "inputSchema": {"type": "object"},
                        }],
                    },
                },
            )

        service = McpService(timeout_seconds=5)
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await service.discover(url="http://mcp.test/mcp", token="secret")
        finally:
            await service.close()

        self.assertEqual("test-server", result["server"]["name"])
        self.assertEqual("submit_data_maintenance", result["tools"][0]["name"])
        self.assertEqual("Bearer secret", requests[0].headers["Authorization"])
        self.assertEqual("session-1", requests[1].headers["Mcp-Session-Id"])
        self.assertEqual("session-1", requests[2].headers["Mcp-Session-Id"])

    async def test_call_tool_returns_structured_content(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content or b"{}")
            method = payload.get("method")
            if method == "initialize":
                return httpx.Response(200, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "serverInfo": {"name": "test-server", "version": "1"},
                        "capabilities": {"tools": {}},
                    },
                })
            if method == "notifications/initialized":
                return httpx.Response(202)
            self.assertEqual("tools/call", method)
            self.assertEqual(
                {"identifier": "40386", "force": False},
                payload["params"]["arguments"],
            )
            return httpx.Response(200, json={
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "content": [{"type": "text", "text": "submitted"}],
                    "structuredContent": {
                        "identifier": "40386",
                        "submitted": True,
                        "work_num": "5400000000382472",
                    },
                },
            })

        service = McpService(timeout_seconds=5)
        await service._client.aclose()
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await service.call_tool(
                url="http://mcp.test/mcp",
                tool_name="submit_data_maintenance",
                arguments={"identifier": "40386", "force": False},
            )
        finally:
            await service.close()

        self.assertEqual("5400000000382472", result["structured"]["work_num"])


class McpSmartReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_identifier_is_sent_to_mcp_and_reply_is_rendered(self):
        import main

        calls: list[dict] = []

        class FakeMcpService:
            async def call_tool(self, **kwargs):
                calls.append(kwargs)
                return {
                    "text": "submitted",
                    "structured": {
                        "identifier": "40386",
                        "submitted": True,
                        "work_num": "5400000000382472",
                    },
                    "raw": {},
                }

        task = {
            "confidence": 85,
            "output_mode": "result",
            "preserve_formatting": True,
            "mcp_enabled": True,
            "mcp_connection_id": "local_mcp",
            "mcp_tool_name": "submit_data_maintenance",
            "mcp_arguments_template": '{"identifier":"{{identifier}}","force":false}',
            "mcp_reply_template": "需求 {{identifier}} 保存并提交成功，需求编号：{{work_num}}",
        }
        ai_result = {
            "matched": True,
            "confidence": 95,
            "result": "40386\n该 SQL 用于查询待处理需求。",
            "items": [],
            "reply": "40386\n该 SQL 用于查询待处理需求。",
        }
        old_service = main.mcp_service
        old_connections = config.MCP_CONNECTIONS
        try:
            main.mcp_service = FakeMcpService()
            config.MCP_CONNECTIONS = [{
                "id": "local_mcp",
                "name": "本地 MCP",
                "url": "http://127.0.0.1:8765/mcp",
                "token": "",
                "enabled": True,
            }]
            replies = await main._mcp_task_reply(task, ai_result)
        finally:
            main.mcp_service = old_service
            config.MCP_CONNECTIONS = old_connections

        self.assertEqual(
            ("需求 40386 保存并提交成功，需求编号：5400000000382472",),
            replies,
        )
        self.assertEqual(
            {"identifier": "40386", "force": False},
            calls[0]["arguments"],
        )
        self.assertEqual("submit_data_maintenance", calls[0]["tool_name"])


class McpConfigTests(unittest.TestCase):
    def test_mcp_connections_are_saved_and_existing_token_is_preserved(self):
        old_path = config._CONFIG_PATH
        old_connections = config.MCP_CONNECTIONS
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "config.yaml"
                path.write_text(yaml.safe_dump({
                    "ai_profiles": [{
                        "id": "ai",
                        "name": "AI",
                        "base_url": "https://ai.test/v1",
                        "api_key": "key",
                        "model": "model",
                    }],
                    "ai_active_profile_id": "ai",
                    "mcp_connections": [{
                        "id": "local_mcp",
                        "name": "Local",
                        "url": "http://127.0.0.1:8765/mcp",
                        "token": "saved-token",
                        "enabled": True,
                    }],
                }), encoding="utf-8")
                config._CONFIG_PATH = os.fspath(path)
                config.reload_ai_settings()
                config.save_ai_settings(
                    profiles=[{
                        "id": "ai",
                        "name": "AI",
                        "base_url": "https://ai.test/v1",
                        "api_key": "key",
                        "model": "model",
                    }],
                    active_profile_id="ai",
                    mcp_connections=[{
                        "id": "local_mcp",
                        "name": "Updated",
                        "url": "http://127.0.0.1:8765/mcp",
                        "token": "",
                        "enabled": True,
                    }],
                )
                saved = yaml.safe_load(path.read_text(encoding="utf-8"))
                self.assertEqual("saved-token", saved["mcp_connections"][0]["token"])
                self.assertEqual("Updated", saved["mcp_connections"][0]["name"])
        finally:
            config._CONFIG_PATH = old_path
            config.MCP_CONNECTIONS = old_connections
            config.reload_ai_settings()


if __name__ == "__main__":
    unittest.main()
