from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import websockets

from aibridge import broker as broker_module
from aibridge.broker import BridgeBroker, start_health_server
from aibridge.client import CLEAR_MARKER, CloudBridge, CloudBridgeConfig
from aibridge.job_store import JobStore


class BridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_is_committed_before_client_success_and_served_by_get_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir) / "jobs.sqlite3")
            broker = BridgeBroker(store)
            ws_server = await websockets.serve(
                broker.handle_client,
                "127.0.0.1",
                0,
                max_size=broker_module.MAX_MESSAGE_BYTES,
            )
            ws_port = ws_server.sockets[0].getsockname()[1]
            with mock.patch.object(broker_module, "HTTP_PORT", 0):
                http_server = start_health_server(broker, asyncio.get_running_loop())
            http_port = http_server.server_port

            async def fake_extension() -> None:
                async with websockets.connect(f"ws://127.0.0.1:{ws_port}") as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "BRIDGE_READY",
                                "source": "chatgpt",
                                "sources": ["chatgpt"],
                            }
                        )
                    )
                    raw_request = await websocket.recv()
                    request = json.loads(raw_request)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "TOKEN",
                                "id": request["id"],
                                "source": "chatgpt",
                                "token": "partial",
                            }
                        )
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "FINAL",
                                "id": request["id"],
                                "source": "chatgpt",
                                "content": "durable result",
                            }
                        )
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "DONE",
                                "id": request["id"],
                                "source": "chatgpt",
                            }
                        )
                    )

            extension_task = asyncio.create_task(fake_extension())
            try:
                for _ in range(100):
                    if "chatgpt" in await broker.sources():
                        break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("fake extension did not become ready")

                bridge = CloudBridge(
                    CloudBridgeConfig(
                        url=f"ws://127.0.0.1:{ws_port}",
                        health_url=f"http://127.0.0.1:{http_port}",
                        response_timeout_seconds=5,
                    )
                )
                chunks = [
                    chunk
                    async for chunk in bridge.stream_completion("prompt", source="chatgpt")
                ]
                await extension_task

                self.assertEqual(chunks[-1], CLEAR_MARKER + "durable result")
                stored = await bridge.get_last_result("chatgpt")
                self.assertEqual(stored.content, "durable result")
                self.assertEqual(store.get_job(stored.id).state, "COMPLETED")
            finally:
                if not extension_task.done():
                    extension_task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await extension_task
                ws_server.close()
                await ws_server.wait_closed()
                await asyncio.to_thread(http_server.shutdown)
                http_server.server_close()
                broker.close()


if __name__ == "__main__":
    unittest.main()
