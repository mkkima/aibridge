from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aibridge import broker as broker_module
from aibridge.broker import BridgeBroker, ExtensionClient, SERVICE_NAME
from aibridge.job_store import JobStore, JobStoreError


class FakeWebSocket:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.messages: list[str] = []
        self.fail_send = fail_send

    async def send(self, message: str) -> None:
        if self.fail_send:
            raise ConnectionError("fake websocket disconnected")
        self.messages.append(message)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite3")
        self.broker = BridgeBroker(self.store)

    def tearDown(self) -> None:
        self.broker.close()
        self.temp_dir.cleanup()

    def attach_extension(
        self,
        websocket: FakeWebSocket,
        *,
        source: str = "chatgpt",
        sources: list[str] | None = None,
    ) -> None:
        advertised_sources = sources or [source]
        self.broker._extension_clients[1] = ExtensionClient(
            id=1,
            websocket=websocket,
            source=source,
            sources=advertised_sources,
        )

    @staticmethod
    def request_payload(
        request_id: str = "request-1",
        *,
        source: str = "chatgpt",
        context: str = "hello",
    ) -> tuple[dict[str, object], str]:
        payload: dict[str, object] = {
            "id": request_id,
            "type": "COMPLETION_REQUEST",
            "context": context,
            "source": source,
        }
        return payload, json.dumps(payload)

    async def dispatch(
        self,
        core: FakeWebSocket,
        *,
        request_id: str = "request-1",
        source: str = "chatgpt",
        context: str = "hello",
        client_id: int = 7,
    ) -> str:
        request, request_raw = self.request_payload(
            request_id,
            source=source,
            context=context,
        )
        self.broker._request_ids_by_client.setdefault(client_id, set())
        await self.broker._handle_completion_request(
            client_id,
            core,
            request,
            request_raw,
        )
        return request_raw

    async def test_routes_request_and_persists_final_by_id(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension, sources=["chatgpt", "gemini"])

        request_raw = await self.dispatch(core, source="gemini")
        self.assertEqual(extension.messages, [request_raw])
        self.assertEqual(self.store.get_job("request-1").state, "DISPATCHED")

        response = {
            "id": "request-1",
            "type": "FINAL",
            "source": "gemini",
            "content": "world",
        }
        response_raw = json.dumps(response)
        await self.broker._handle_completion_response(response, response_raw)

        self.assertEqual(core.messages, [response_raw])
        self.assertNotIn("request-1", self.broker._core_clients_by_request)
        job = self.store.get_job("request-1")
        self.assertEqual(job.state, "COMPLETED")
        self.assertEqual(job.result_content, "world")

    async def test_current_tab_read_bypasses_store_and_forwards_final(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension)
        payload = {
            "id": "tab-read-1",
            "type": "COMPLETION_REQUEST",
            "context": "",
            "source": "chatgpt",
            "readCurrentTab": True,
        }
        raw = json.dumps(payload)
        self.broker._request_ids_by_client[7] = set()

        await self.broker._handle_completion_request(7, core, payload, raw)

        self.assertEqual(extension.messages, [raw])
        self.assertIsNone(self.store.get_job("tab-read-1"))
        final = {
            "id": "tab-read-1",
            "type": "FINAL",
            "source": "chatgpt",
            "content": "visible response",
        }
        final_raw = json.dumps(final)
        await self.broker._handle_completion_response(final, final_raw)

        self.assertEqual(core.messages, [final_raw])
        self.assertNotIn("tab-read-1", self.broker._tab_read_requests)
        self.assertIsNone(self.store.get_job("tab-read-1"))

    async def test_current_tab_read_requires_connected_source(self) -> None:
        core = FakeWebSocket()
        payload = {
            "id": "tab-read-1",
            "type": "COMPLETION_REQUEST",
            "context": "",
            "source": "gemini",
            "readCurrentTab": True,
        }
        self.broker._request_ids_by_client[7] = set()

        await self.broker._handle_completion_request(7, core, payload, json.dumps(payload))

        error = json.loads(core.messages[0])
        self.assertEqual(error["code"], "OPEN_TAB_NOT_CONNECTED")
        self.assertIsNone(self.store.get_job("tab-read-1"))

    async def test_done_without_final_is_failed_and_reported_as_error(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension)
        await self.dispatch(core)

        await self.broker._handle_completion_response(
            {"id": "request-1", "type": "DONE", "source": "chatgpt"},
            json.dumps({"id": "request-1", "type": "DONE", "source": "chatgpt"}),
        )

        job = self.store.get_job("request-1")
        self.assertEqual(job.state, "FAILED")
        self.assertEqual(job.error_code, "DONE_WITHOUT_FINAL")
        self.assertEqual(len(core.messages), 1)
        error = json.loads(core.messages[0])
        self.assertEqual(error["type"], "ERROR")
        self.assertEqual(error["code"], "DONE_WITHOUT_FINAL")

    async def test_done_after_final_is_ignored(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension)
        await self.dispatch(core)

        final = {
            "id": "request-1",
            "type": "FINAL",
            "source": "chatgpt",
            "content": "durable",
        }
        final_raw = json.dumps(final)
        await self.broker._handle_completion_response(final, final_raw)
        await self.broker._handle_completion_response(
            {"id": "request-1", "type": "DONE", "source": "chatgpt"},
            json.dumps({"id": "request-1", "type": "DONE", "source": "chatgpt"}),
        )

        self.assertEqual(core.messages, [final_raw])
        self.assertEqual(self.store.get_job("request-1").state, "COMPLETED")

    async def test_empty_final_is_failed(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension)
        await self.dispatch(core)

        await self.broker._handle_completion_response(
            {"id": "request-1", "type": "FINAL", "source": "chatgpt", "content": "  "},
            "unused",
        )

        job = self.store.get_job("request-1")
        self.assertEqual(job.state, "FAILED")
        self.assertEqual(job.error_code, "INVALID_FINAL_CONTENT")
        self.assertEqual(json.loads(core.messages[0])["type"], "ERROR")

    async def test_final_is_not_reported_as_success_when_commit_fails(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension)
        await self.dispatch(core)
        final = {
            "id": "request-1",
            "type": "FINAL",
            "source": "chatgpt",
            "content": "must not leak as success",
        }

        with (
            mock.patch.object(
                self.store,
                "complete",
                side_effect=JobStoreError("simulated disk failure"),
            ),
            mock.patch.object(broker_module.LOGGER, "error"),
        ):
            await self.broker._handle_completion_response(final, json.dumps(final))

        self.assertEqual(len(core.messages), 1)
        error = json.loads(core.messages[0])
        self.assertEqual(error["type"], "ERROR")
        self.assertEqual(error["code"], "STORE_UNAVAILABLE")
        self.assertNotEqual(core.messages[0], json.dumps(final))

    async def test_duplicate_request_replays_completed_result_without_dispatch(self) -> None:
        first_core = FakeWebSocket()
        second_core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension)
        request_raw = await self.dispatch(first_core)
        final = {
            "id": "request-1",
            "type": "FINAL",
            "source": "chatgpt",
            "content": "saved",
        }
        await self.broker._handle_completion_response(final, json.dumps(final))

        request, _ = self.request_payload()
        self.broker._request_ids_by_client[8] = set()
        await self.broker._handle_completion_request(8, second_core, request, request_raw)

        self.assertEqual(extension.messages, [request_raw])
        replay = json.loads(second_core.messages[0])
        self.assertEqual(replay["type"], "FINAL")
        self.assertEqual(replay["content"], "saved")

    async def test_dispatch_connection_loss_becomes_unknown_outcome(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket(fail_send=True)
        self.attach_extension(extension)

        await self.dispatch(core)

        job = self.store.get_job("request-1")
        self.assertEqual(job.state, "UNKNOWN_OUTCOME")
        self.assertEqual(job.error_code, "DISPATCH_CONNECTION_LOST")
        self.assertEqual(json.loads(core.messages[0])["type"], "ERROR")

    async def test_pending_ttl_is_durable_and_notifies_waiting_client(self) -> None:
        core = FakeWebSocket()
        with mock.patch.object(broker_module, "PENDING_TTL_SECONDS", 0.001):
            await self.dispatch(core)

        self.assertEqual(self.store.get_job("request-1").state, "QUEUED")
        await asyncio.sleep(0.01)
        await self.broker.maintenance_once()

        job = self.store.get_job("request-1")
        self.assertEqual(job.state, "FAILED")
        self.assertEqual(job.error_code, "PENDING_TIMEOUT")
        self.assertEqual(json.loads(core.messages[0])["code"], "PENDING_TIMEOUT")
        self.assertNotIn("request-1", self.broker._pending_requests)

    async def test_request_waits_when_only_wrong_source_is_available(self) -> None:
        core = FakeWebSocket()
        extension = FakeWebSocket()
        self.attach_extension(extension, source="chatgpt")

        await self.dispatch(core, source="gemini")

        self.assertEqual(extension.messages, [])
        self.assertEqual(self.store.get_job("request-1").state, "QUEUED")

    async def test_snapshot_uses_standalone_service_name(self) -> None:
        await self.broker.set_ready(True)
        snapshot = await self.broker.snapshot()

        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["service"], SERVICE_NAME)
        self.assertEqual(SERVICE_NAME, "aibridge")


if __name__ == "__main__":
    unittest.main()
