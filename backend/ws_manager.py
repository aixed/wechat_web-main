"""WebSocket connection manager - pushes real-time messages to frontend clients."""

from fastapi import WebSocket
import asyncio
import json


class ConnectionManager:
    """Manages WebSocket connections to frontend clients."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"[WS] Frontend connected. Total: {len(self.active_connections)}", flush=True)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"[WS] Frontend disconnected. Total: {len(self.active_connections)}", flush=True)

    async def broadcast(self, data: dict):
        """Send data to all connected frontend clients."""
        message = json.dumps(data, ensure_ascii=False)
        connections = list(self.active_connections)
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
            if conn in self.active_connections:
                self.active_connections.remove(conn)


manager = ConnectionManager()
