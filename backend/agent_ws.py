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


@dataclass
class AgentConnection:
    id: str
    websocket: WebSocket
    peer: str
    connected_at: float
    last_seen_at: float
    pending: dict[str, asyncio.Future[dict[str, Any]]]
    send_lock: asyncio.Lock
    account_id: str = ""
    nickname: str = ""
    wxid: str = ""
    avatar: str = ""
    server_port: str = ""
    initialized: bool = False
    registered: bool = False


class AgentWebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, AgentConnection] = {}
        self._active_id: str = ""
        self._lock = asyncio.Lock()

    def is_connected(self, agent_id: str | None = None) -> bool:
        if agent_id:
            conn = self._connections.get(agent_id)
            return bool(conn and conn.registered)
        return any(conn.registered for conn in self._connections.values())

    def active_id(self) -> str:
        conn = self._connections.get(self._active_id)
        return self._active_id if conn and conn.registered else ""

    async def set_active(self, agent_id: str) -> bool:
        async with self._lock:
            conn = self._connections.get(agent_id)
            if not conn or not conn.registered:
                return False
            self._active_id = agent_id
            return True

    async def update_account(
        self,
        agent_id: str,
        *,
        wxid: str = "",
        nickname: str = "",
        avatar: str = "",
        initialized: bool | None = None,
    ) -> None:
        async with self._lock:
            conn = self._connections.get(agent_id)
            if not conn:
                return
            if wxid:
                conn.wxid = wxid
                conn.account_id = wxid
                if conn.nickname == conn.id:
                    conn.nickname = ""
            if nickname:
                conn.nickname = nickname
            if avatar:
                conn.avatar = avatar
            if initialized is not None:
                conn.initialized = initialized

    def status(self) -> dict[str, Any]:
        agents = self.agents()
        active_conn = self._connections.get(self._active_id)
        active = self._active_id if active_conn and active_conn.registered else (agents[0]["id"] if agents else "")
        return {
            "connected": self.is_connected(),
            "peer": agents[0]["peer"] if agents else "",
            "connected_at": agents[0]["connected_at"] if agents else 0.0,
            "last_seen_at": max((a["last_seen_at"] for a in agents), default=0.0),
            "pending": sum(int(a["pending"]) for a in agents),
            "active_id": active,
            "count": len(agents),
            "pending_registration": sum(1 for conn in self._connections.values() if not conn.registered),
            "agents": agents,
        }

    def agents(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for conn in self._connections.values():
            if not conn.registered:
                continue
            rows.append({
                "id": conn.id,
                "account_id": conn.account_id or conn.wxid or conn.id,
                "wxid": conn.wxid,
                "nickname": conn.nickname,
                "avatar": conn.avatar,
                "server_port": conn.server_port,
                "peer": conn.peer,
                "connected_at": conn.connected_at,
                "last_seen_at": conn.last_seen_at,
                "pending": len(conn.pending),
                "initialized": conn.initialized,
                "active": conn.id == self._active_id,
            })
        rows.sort(key=lambda x: x.get("connected_at", 0))
        return rows

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        conn = self._connections.get(agent_id)
        if not conn or not conn.registered:
            return None
        return {
            "id": conn.id,
            "account_id": conn.account_id or conn.wxid or conn.id,
            "wxid": conn.wxid,
            "nickname": conn.nickname,
            "avatar": conn.avatar,
            "server_port": conn.server_port,
            "peer": conn.peer,
            "connected_at": conn.connected_at,
            "last_seen_at": conn.last_seen_at,
            "pending": len(conn.pending),
            "initialized": conn.initialized,
            "active": conn.id == self._active_id,
        }

    def agent_id_for_wxid(self, wxid: str) -> str:
        wxid = str(wxid or "").strip()
        if not wxid:
            return ""
        for conn in self._connections.values():
            if conn.registered and conn.wxid == wxid:
                return conn.id
        return ""

    def uninitialized_agent_ids(self) -> list[str]:
        return [conn.id for conn in self._connections.values() if conn.registered and not conn.initialized]

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        peer = self._peer_name(websocket)
        query_id = str(websocket.query_params.get("agent_id") or "").strip()
        conn_id = query_id or f"agent-{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn = AgentConnection(
            id=conn_id,
            websocket=websocket,
            peer=peer,
            connected_at=now,
            last_seen_at=now,
            pending={},
            send_lock=asyncio.Lock(),
            registered=True,
        )
        old_conn: AgentConnection | None = None

        async with self._lock:
            old_conn = self._connections.get(conn_id)
            self._connections[conn_id] = conn
            if conn.registered and (not self._active_id or self._active_id not in self._connections):
                self._active_id = conn_id

        if old_conn is not None and old_conn.websocket is not websocket:
            for future in old_conn.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("agent websocket was replaced"))
            await self._close_socket(old_conn.websocket, code=1012, reason="agent reconnected")

        print(f"[AGENT_WS] connected id={conn_id} peer={peer}", flush=True)
        try:
            while True:
                payload = await self._receive_payload(websocket)
                if payload is None:
                    break
                conn.last_seen_at = time.time()
                await self._handle_message(conn, payload)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            print(f"[AGENT_WS] receive loop error: {type(exc).__name__}: {exc}", flush=True)
        finally:
            await self._detach(conn, "agent websocket disconnected")
            print(f"[AGENT_WS] disconnected id={conn.id} peer={peer}", flush=True)

    async def request(
        self,
        route: str,
        body: Any,
        *,
        method: str = "POST",
        timeout: float = 30.0,
        agent_id: str | None = None,
    ) -> AgentCallResponse:
        request_id = f"req-{uuid.uuid4().hex}"
        route_name = route.strip("/")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        async with self._lock:
            body_agent_id = self._agent_id_from_body(body)
            target_id = agent_id or body_agent_id or self._active_id
            explicit_target = bool(agent_id or body_agent_id)
            if target_id not in self._connections or not self._connections[target_id].registered:
                if explicit_target:
                    target_id = ""
                else:
                    registered_ids = [cid for cid, c in self._connections.items() if c.registered]
                    target_id = registered_ids[0] if registered_ids else ""
            conn = self._connections.get(target_id)
            if conn is None or not conn.registered:
                raise ConnectionError("agent websocket is not connected")
            conn.pending[request_id] = future

        request_body = body if body is not None else {}
        if isinstance(request_body, dict):
            request_body = dict(request_body)
            request_body.setdefault("agent_id", target_id)

        message = {
            "type": "request",
            "id": request_id,
            "route": route_name,
            "method": method,
            "agent_id": target_id,
            "body": request_body,
        }

        try:
            async with conn.send_lock:
                await conn.websocket.send_text(json.dumps(message, ensure_ascii=False))
            raw_response = await asyncio.wait_for(future, timeout=timeout)
            return self._coerce_response(raw_response)
        except Exception:
            async with self._lock:
                conn.pending.pop(request_id, None)
            raise

    async def close(self) -> None:
        async with self._lock:
            conns = list(self._connections.values())
            self._connections = {}
            self._active_id = ""

        for conn in conns:
            for future in conn.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("agent websocket closed"))
            await self._close_socket(conn.websocket, code=1001, reason="server shutdown")

    async def _handle_message(self, conn: AgentConnection, payload: str | bytes) -> None:
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            message = json.loads(payload)
        except Exception:
            await conn.websocket.send_text(json.dumps({
                "type": "error",
                "ok": False,
                "status": 400,
                "body": {"msg": "agent message must be JSON"},
            }, ensure_ascii=False))
            return

        if not isinstance(message, dict):
            return

        msg_type = str(message.get("type", "")).lower()
        message_agent_id = self._agent_id_from_message(message)
        if message_agent_id:
            await self._bind_agent_id(conn, message_agent_id)
        self._update_metadata_from_message(conn, message)
        await self._dedupe_same_wxid(conn)

        if msg_type == "hello" or (msg_type in {"", "register"} and message_agent_id):
            body = self._metadata_for_connection(conn)
            await conn.websocket.send_text(json.dumps({
                "type": "hello_ack" if msg_type != "register" else "register_ack",
                "agent_id": conn.id,
                "body": body,
            }, ensure_ascii=False))
            return
        if msg_type == "ping":
            body = self._metadata_for_connection(conn)
            pong = {
                "type": "pong",
                "agent_id": conn.id,
                "body": body,
            }
            if message.get("id"):
                pong["id"] = message.get("id")
            await conn.websocket.send_text(json.dumps(pong, ensure_ascii=False))
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
            future = conn.pending.pop(request_id, None)

        if future is None:
            print(f"[AGENT_WS] late/unknown response id={request_id}", flush=True)
            return
        if not future.done():
            future.set_result(message)

    def _agent_id_from_body(self, body: Any) -> str:
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                return ""
        if not isinstance(body, dict):
            return ""
        return str(
            body.get("agent_id")
            or body.get("AgentId")
            or body.get("agentId")
            or body.get("account_id")
            or self._agent_id_from_body(body.get("body"))
            or self._agent_id_from_body(body.get("data"))
            or self._agent_id_from_body(body.get("payload"))
            or self._agent_id_from_body(body.get("params"))
            or ""
        ).strip()

    def _agent_id_from_message(self, message: dict[str, Any]) -> str:
        direct = str(
            message.get("agent_id")
            or message.get("AgentId")
            or message.get("agentId")
            or message.get("account_id")
            or ""
        ).strip()
        if direct:
            return direct
        return self._agent_id_from_body(message.get("body"))

    def _metadata_value_from_body(self, body: Any, *keys: str) -> str:
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                return ""
        if not isinstance(body, dict):
            return ""
        for key in keys:
            value = body.get(key)
            if value not in (None, ""):
                return str(value).strip()
        for nested_key in ("body", "data", "payload", "params"):
            value = self._metadata_value_from_body(body.get(nested_key), *keys)
            if value:
                return value
        return ""

    def _update_metadata_from_message(self, conn: AgentConnection, message: dict[str, Any]) -> None:
        self_wxid = str(
            message.get("selfwxid")
            or message.get("selfWxid")
            or message.get("self_wxid")
            or self._metadata_value_from_body(message.get("body"), "selfwxid", "selfWxid", "self_wxid")
            or ""
        ).strip()
        if self_wxid:
            conn.wxid = self_wxid
            conn.account_id = self_wxid
            if conn.nickname == conn.id:
                conn.nickname = ""

        server_port = str(
            message.get("ServerPort")
            or message.get("server_port")
            or self._metadata_value_from_body(message.get("body"), "ServerPort", "server_port")
            or ""
        ).strip()
        if server_port:
            conn.server_port = server_port

    def _metadata_for_connection(self, conn: AgentConnection) -> dict[str, Any]:
        body: dict[str, Any] = {"agent_id": conn.id}
        if conn.wxid:
            body["selfwxid"] = conn.wxid
        if conn.server_port:
            body["ServerPort"] = conn.server_port
        return body

    async def _dedupe_same_wxid(self, conn: AgentConnection) -> None:
        wxid = str(conn.wxid or "").strip()
        if not wxid:
            return

        old_conns: list[AgentConnection] = []
        async with self._lock:
            for cid, item in list(self._connections.items()):
                if item is conn or not item.registered or item.wxid != wxid:
                    continue
                self._connections.pop(cid, None)
                old_conns.append(item)
                if self._active_id == cid:
                    self._active_id = conn.id

        for old_conn in old_conns:
            for future in old_conn.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("wechat account reconnected"))
            old_conn.pending = {}
            await self._close_socket(old_conn.websocket, code=1012, reason="wechat account reconnected")
            print(f"[AGENT_WS] replaced wxid={wxid} old_id={old_conn.id} new_id={conn.id}", flush=True)

    async def _bind_agent_id(self, conn: AgentConnection, agent_id: str) -> None:
        agent_id = str(agent_id or "").strip()
        if not agent_id or agent_id == conn.id:
            return

        old_conn: AgentConnection | None = None
        async with self._lock:
            if self._connections.get(conn.id) is conn:
                self._connections.pop(conn.id, None)

            old_conn = self._connections.get(agent_id)
            if old_conn is not None and old_conn is not conn:
                self._connections.pop(agent_id, None)

            old_id = conn.id
            conn.id = agent_id
            conn.registered = True
            self._connections[agent_id] = conn
            if not self._active_id or self._active_id == old_id or self._active_id not in self._connections:
                self._active_id = agent_id

        if old_conn is not None and old_conn is not conn:
            for future in old_conn.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("agent websocket was replaced"))
            old_conn.pending = {}
            await self._close_socket(old_conn.websocket, code=1012, reason="agent reconnected")
            print(f"[AGENT_WS] replaced id={agent_id} peer={old_conn.peer}", flush=True)

        print(f"[AGENT_WS] registered id={agent_id} peer={conn.peer}", flush=True)

    async def _detach(self, conn: AgentConnection, reason: str) -> None:
        async with self._lock:
            current = self._connections.get(conn.id)
            if current is not conn:
                return
            self._connections.pop(conn.id, None)
            if self._active_id == conn.id:
                self._active_id = next((cid for cid, item in self._connections.items() if item.registered), "")
            pending = conn.pending
            conn.pending = {}

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
