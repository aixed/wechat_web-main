import asyncio
import json
import unittest

from agent_ws import AgentConnection, AgentWebSocketManager


class AgentWebSocketCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_is_dispatched_without_blocking_receive_loop(self):
        manager = AgentWebSocketManager()
        started = asyncio.Event()
        release = asyncio.Event()
        received = []

        async def callback_handler(payload):
            received.append(payload)
            started.set()
            await release.wait()

        manager.set_callback_handler(callback_handler)
        conn = AgentConnection(
            id="agent-test",
            websocket=object(),
            peer="test",
            connected_at=0,
            last_seen_at=0,
            pending={},
            send_lock=asyncio.Lock(),
            wxid="wxid_test",
            server_port="30001",
            registered=True,
        )
        message = {
            "type": "callback",
            "route": "receiveChatBotMsg",
            "body": {
                "sendorrecv": "2",
                "msglist": [{"msgtype": "1", "msg": "hello"}],
            },
        }

        await asyncio.wait_for(
            manager._handle_message(conn, json.dumps(message)),
            timeout=0.5,
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)

        self.assertEqual("agent-test", received[0]["agent_id"])
        self.assertEqual("wxid_test", received[0]["selfwxid"])
        self.assertEqual("30001", received[0]["ServerPort"])

        release.set()
        await asyncio.gather(*list(manager._callback_tasks))


if __name__ == "__main__":
    unittest.main()
