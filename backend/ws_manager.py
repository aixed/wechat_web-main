"""WebSocket connection manager - pushes real-time messages to frontend clients."""

from fastapi import WebSocket
import asyncio
import json
import access_store


class ConnectionManager:
    """Manages WebSocket connections to frontend clients."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.scopes = {}

    async def connect(self, websocket: WebSocket, *, user_id='', account_id=''):
        await websocket.accept()
        self.active_connections.append(websocket)
        self.scopes[websocket] = (user_id, account_id)
        print(f"[WS] Frontend connected. Total: {len(self.active_connections)}", flush=True)

    def disconnect(self, websocket: WebSocket):
        self.scopes.pop(websocket, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"[WS] Frontend disconnected. Total: {len(self.active_connections)}", flush=True)

    async def broadcast(self, data: dict):
        """Send data to all connected frontend clients."""
        message = json.dumps(data, ensure_ascii=False)
        account_id = str((data.get('data') or {}).get('account_id') or access_store.current_account.get() or '')
        owner = access_store.owner(account_id)
        connections = [connection for connection in self.active_connections
                       if self.scopes.get(connection) == (owner, account_id) and owner]
        if not connections:
            return

        async def send_one(connection: WebSocket):
            try:
                await asyncio.wait_for(connection.send_text(message), timeout=1.5)
                return None
            except Exception as exc:
                print(f"[WS] Drop slow/broken frontend connection: {type(exc).__name__}: {exc}", flush=True)
                return connection

        disconnected = [
            conn for conn in await asyncio.gather(*(send_one(conn) for conn in connections))
            if conn is not None
        ]
        for conn in disconnected:
            self.scopes.pop(conn, None)
            if conn in self.active_connections:
                self.active_connections.remove(conn)


manager = ConnectionManager()
