from __future__ import annotations

import json
import unittest

from aibridge.broker import BridgeBroker, ExtensionClient, SERVICE_NAME


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_request_and_response_by_id(self) -> None:
        broker = BridgeBroker()
        core = FakeWebSocket()
        extension = FakeWebSocket()
        broker._extension_clients[1] = ExtensionClient(
            id=1,
            websocket=extension,
            source="chatgpt",
            sources=["chatgpt", "gemini"],
        )
        broker._request_ids_by_client[7] = set()

        request = {
            "id": "request-1",
            "type": "COMPLETION_REQUEST",
            "context": "hello",
            "source": "gemini",
        }
        request_raw = json.dumps(request)
        await broker._handle_completion_request(7, core, request, request_raw)
        self.assertEqual(extension.messages, [request_raw])

        response = {"id": "request-1", "type": "FINAL", "content": "world"}
        response_raw = json.dumps(response)
        await broker._handle_completion_response(response, response_raw)

        self.assertEqual(core.messages, [response_raw])
        self.assertNotIn("request-1", broker._core_clients_by_request)

    async def test_snapshot_uses_standalone_service_name(self) -> None:
        broker = BridgeBroker()
        await broker.set_ready(True)
        snapshot = await broker.snapshot()

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["service"], SERVICE_NAME)
        self.assertEqual(SERVICE_NAME, "aibridge")


if __name__ == "__main__":
    unittest.main()
