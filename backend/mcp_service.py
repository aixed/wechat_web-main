"""Minimal Streamable HTTP MCP client used by smart-reply Skills."""

from __future__ import annotations

import json
from typing import Any

import httpx


class McpServiceError(RuntimeError):
    pass


def _sse_payloads(text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in str(text or "").splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line.strip() or not data_lines:
            continue
        raw = "\n".join(data_lines)
        data_lines = []
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            payloads.append(value)
    return payloads


def _response_payload(response: httpx.Response, request_id: int | None) -> dict[str, Any]:
    if not response.content:
        return {}
    content_type = response.headers.get("content-type", "").casefold()
    payloads: list[dict[str, Any]] = []
    if "text/event-stream" in content_type:
        payloads = _sse_payloads(response.text)
    else:
        try:
            value = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise McpServiceError("MCP returned an invalid JSON response") from exc
        if isinstance(value, dict):
            payloads = [value]
    if not payloads:
        raise McpServiceError("MCP returned an empty response")
    if request_id is None:
        return payloads[-1]
    matching = next((item for item in payloads if item.get("id") == request_id), None)
    if matching is None:
        raise McpServiceError("MCP response request ID does not match")
    return matching


def _tool_text(result: dict[str, Any]) -> str:
    values: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                values.append(text)
    return "\n".join(values)


def _structured_tool_result(result: dict[str, Any], text: str) -> Any:
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    if text:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


class McpService:
    def __init__(self, *, timeout_seconds: float = 120) -> None:
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _headers(token: str = "", session_id: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-03-26",
        }
        if str(token or "").strip():
            headers["Authorization"] = f"Bearer {str(token).strip()}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    async def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        token: str = "",
        session_id: str = "",
        request_id: int | None,
    ) -> tuple[dict[str, Any], str]:
        try:
            response = await self._client.post(
                url,
                headers=self._headers(token, session_id),
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip()[:500]
            raise McpServiceError(
                f"MCP HTTP {exc.response.status_code}: {detail or exc.response.reason_phrase}"
            ) from exc
        except httpx.HTTPError as exc:
            raise McpServiceError(f"MCP connection failed: {exc}") from exc
        next_session_id = str(response.headers.get("mcp-session-id") or session_id or "").strip()
        if request_id is None and not response.content:
            return {}, next_session_id
        message = _response_payload(response, request_id)
        error = message.get("error")
        if isinstance(error, dict):
            raise McpServiceError(str(error.get("message") or "MCP request failed"))
        return message, next_session_id

    async def _initialize(self, url: str, token: str = "") -> tuple[dict[str, Any], str]:
        message, session_id = await self._post(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "wechat-web", "version": "1.0"},
                },
            },
            token=token,
            request_id=1,
        )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpServiceError("MCP initialize response is invalid")
        await self._post(
            url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=token,
            session_id=session_id,
            request_id=None,
        )
        return result, session_id

    async def discover(self, *, url: str, token: str = "") -> dict[str, Any]:
        normalized_url = str(url or "").strip()
        if not normalized_url:
            raise McpServiceError("MCP URL is required")
        initialized, session_id = await self._initialize(normalized_url, token)
        message, _ = await self._post(
            normalized_url,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            token=token,
            session_id=session_id,
            request_id=2,
        )
        result = message.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpServiceError("MCP tools/list response is invalid")
        return {
            "server": initialized.get("serverInfo") if isinstance(initialized.get("serverInfo"), dict) else {},
            "instructions": str(initialized.get("instructions") or ""),
            "tools": [tool for tool in tools if isinstance(tool, dict)],
        }

    async def call_tool(
        self,
        *,
        url: str,
        tool_name: str,
        arguments: dict[str, Any],
        token: str = "",
    ) -> dict[str, Any]:
        normalized_url = str(url or "").strip()
        normalized_tool = str(tool_name or "").strip()
        if not normalized_url or not normalized_tool:
            raise McpServiceError("MCP URL and tool name are required")
        _, session_id = await self._initialize(normalized_url, token)
        message, _ = await self._post(
            normalized_url,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": normalized_tool, "arguments": arguments},
            },
            token=token,
            session_id=session_id,
            request_id=3,
        )
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpServiceError("MCP tool response is invalid")
        text = _tool_text(result)
        if bool(result.get("isError")):
            raise McpServiceError(text or "MCP tool reported an error")
        return {
            "text": text,
            "structured": _structured_tool_result(result, text),
            "raw": result,
        }
