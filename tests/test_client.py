from __future__ import annotations

import hashlib
import json
import unittest

from aibridge.client import CLEAR_MARKER, CloudBridge, CloudBridgeError


class FakeReceiveWebSocket:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = [json.dumps(event) for event in events]

    async def recv(self) -> str:
        if not self.events:
            raise AssertionError("test websocket ran out of events")
        return self.events.pop(0)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def collect(
        self,
        events: list[dict[str, object]],
        request_id: str = "request-1",
    ) -> list[str]:
        bridge = CloudBridge()
        websocket = FakeReceiveWebSocket(events)
        return [
            chunk
            async for chunk in bridge._receive_completion(websocket, request_id)
        ]

    async def test_stream_only_completes_after_non_empty_final(self) -> None:
        chunks = await self.collect(
            [
                {"id": "request-1", "type": "TOKEN", "token": "hello"},
                {
                    "id": "request-1",
                    "type": "FINAL",
                    "content": "hello world",
                },
            ]
        )

        self.assertEqual(chunks, ["hello", CLEAR_MARKER + "hello world"])

    async def test_done_before_final_is_not_success(self) -> None:
        with self.assertRaisesRegex(CloudBridgeError, "DONE before"):
            await self.collect([{"id": "request-1", "type": "DONE"}])

    async def test_empty_final_is_not_success(self) -> None:
        with self.assertRaisesRegex(CloudBridgeError, "FINAL without"):
            await self.collect(
                [{"id": "request-1", "type": "FINAL", "content": "  "}]
            )

    def test_stored_result_validates_digest_and_length(self) -> None:
        content = "persisted text"
        encoded = content.encode("utf-8")
        payload = {
            "id": "job-1",
            "source": "claude",
            "state": "COMPLETED",
            "content": content,
            "contentBytes": len(encoded),
            "contentSha256": hashlib.sha256(encoded).hexdigest(),
            "completedAtMs": 1234,
        }

        result = CloudBridge._validate_stored_result(payload, "claude")

        self.assertEqual(result.content, content)
        self.assertEqual(result.source, "claude")

    def test_stored_result_rejects_tampering(self) -> None:
        content = "persisted text"
        payload = {
            "id": "job-1",
            "source": "claude",
            "state": "COMPLETED",
            "content": content,
            "contentBytes": len(content.encode("utf-8")),
            "contentSha256": "0" * 64,
            "completedAtMs": 1234,
        }

        with self.assertRaisesRegex(CloudBridgeError, "digest verification"):
            CloudBridge._validate_stored_result(payload, "claude")


if __name__ == "__main__":
    unittest.main()
