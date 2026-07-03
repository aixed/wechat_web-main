"""Server-side WebSocket bridge for DLL agents.

The local DLL connects to /agent and waits for JSON requests. The backend keeps
that WebSocket and uses it as the transport for Hook API calls.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect


@dataclass
class AgentCallResponse:
    status: int
    content_type: str
    body: bytes
    reason: str = ""


class AgentWebSocketManager:
    def __init__(self) -> None:
        self._websocket: WebSocket | None = None
        self._peer: str = ""
        self._connected_at: float = 0.0
        self._last_seen_at: float = 0.0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    def is_connected(self) -> bool:
        return self._websocket is not None

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.is_connected(),
            "peer": self._peer,
            "connected_at": self._connected_at,
            "last_seen_at": self._last_seen_at,
            "pending": len(self._pending),
        }

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        peer = self._peer_name(websocket)
        old_websocket: WebSocket | None = None
        old_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

        async with self._lock:
            old_websocket = self._websocket
            old_pending = self._pending
            self._pending = {}
            self._websocket = websocket
            self._peer = peer
            self._connected_at = time.time()
            self._last_seen_at = self._connected_at

        for future in old_pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent websocket was replaced"))
        if old_websocket is not None and old_websocket is not websocket:
            await self._close_socket(old_websocket, code=1012, reason="agent reconnected")

        print(f"[AGENT_WS] connected peer={peer}", flush=True)
        try:
            while True:
                payload = await self._receive_payload(websocket)
                if payload is None:
                    break
                self._last_seen_at = time.time()
                await self._handle_message(websocket, payload)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            print(f"[AGENT_WS] receive loop error: {type(exc).__name__}: {exc}", flush=True)
        finally:
            await self._detach(websocket, "agent websocket disconnected")
            print(f"[AGENT_WS] disconnected peer={peer}", flush=True)

    async def request(
        self,
        route: str,
        body: Any,
        *,
        method: str = "POST",
        timeout: float = 30.0,
    ) -> AgentCallResponse:
        request_id = f"req-{uuid.uuid4().hex}"
        route_name = route.strip("/")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        async with self._lock:
            websocket = self._websocket
            if websocket is None:
                raise ConnectionError("agent websocket is not connected")
            self._pending[request_id] = future

        message = {
            "type": "request",
            "id": request_id,
            "route": route_name,
            "method": method,
            "body": body if body is not None else {},
        }

        try:
            async with self._send_lock:
                await websocket.send_text(json.dumps(message, ensure_ascii=False))
            raw_response = await asyncio.wait_for(future, timeout=timeout)
            return self._coerce_response(raw_response)
        except Exception:
            async with self._lock:
                self._pending.pop(request_id, None)
            raise

    async def close(self) -> None:
        async with self._lock:
            websocket = self._websocket
            self._websocket = None
            pending = self._pending
            self._pending = {}
            self._peer = ""
            self._connected_at = 0.0
            self._last_seen_at = 0.0

        for future in pending.values():
            if not future.done():
                future.set_exception(ConnectionError("agent websocket closed"))
        if websocket is not None:
            await self._close_socket(websocket, code=1001, reason="server shutdown")

    async def _handle_message(self, websocket: WebSocket, payload: str | bytes) -> None:
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            message = json.loads(payload)
        except Exception:
            await websocket.send_text(json.dumps({
                "type": "error",
                "ok": False,
                "status": 400,
                "body": {"msg": "agent message must be JSON"},
            }, ensure_ascii=False))
            return

        if not isinstance(message, dict):
            return

        msg_type = str(message.get("type", "")).lower()
        if msg_type == "ping":
            pong = {"type": "pong"}
            if message.get("id"):
                pong["id"] = message.get("id")
            await websocket.send_text(json.dumps(pong, ensure_ascii=False))
            return
        if msg_type == "pong":
            return
        if msg_type != "response":
            print(f"[AGENT_WS] ignored message type={msg_type or 'unknown'}", flush=True)
            return

        request_id = str(message.get("id", "") or "")
        if not request_id:
            print("[AGENT_WS] response without id ignored", flush=True)
            return

        async with self._lock:
            future = self._pending.pop(request_id, None)

        if future is None:
            print(f"[AGENT_WS] late/unknown response id={request_id}", flush=True)
            return
        if not future.done():
            future.set_result(message)

    async def _detach(self, websocket: WebSocket, reason: str) -> None:
        async with self._lock:
            if self._websocket is not websocket:
                return
            self._websocket = None
            self._peer = ""
            self._connected_at = 0.0
            self._last_seen_at = 0.0
            pending = self._pending
            self._pending = {}

        for future in pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))

    async def _receive_payload(self, websocket: WebSocket) -> str | bytes | None:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return None
        if message.get("text") is not None:
            return message["text"]
        if message.get("bytes") is not None:
            return message["bytes"]
        return None

    async def _close_socket(self, websocket: WebSocket, *, code: int, reason: str) -> None:
        try:
            await websocket.close(code=code, reason=reason)
        except Exception:
            pass

    def _coerce_response(self, message: dict[str, Any]) -> AgentCallResponse:
        status = int(message.get("status") or (200 if message.get("ok", True) else 500))
        content_type = str(message.get("contentType") or message.get("content_type") or "application/json;charset=utf-8")
        reason = str(message.get("reason") or "")
        body = message.get("body", {})

        if str(message.get("bodyEncoding", "")).lower() == "base64":
            if isinstance(body, str):
                content = base64.b64decode(body)
            else:
                content = b""
        elif isinstance(body, (dict, list)):
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif body is None:
            content = b""
        elif isinstance(body, str):
            content = body.encode("utf-8")
        else:
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")

        return AgentCallResponse(
            status=status,
            content_type=content_type,
            body=content,
            reason=reason,
        )

    def _peer_name(self, websocket: WebSocket) -> str:
        client = websocket.client
        if not client:
            return "unknown"
        return f"{client.host}:{client.port}"


agent_manager = AgentWebSocketManager()
