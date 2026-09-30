from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from aibridge import broker as broker_module
from aibridge.broker import BridgeBroker, start_health_server
from aibridge.job_store import JobStore, utc_now_ms


class HttpApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite3")
        self.broker = BridgeBroker(store)
        with mock.patch.object(broker_module, "HTTP_PORT", 0):
            self.server = start_health_server(self.broker, asyncio.get_running_loop())
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    async def asyncTearDown(self) -> None:
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.broker.close()
        self.temp_dir.cleanup()

    async def fetch(
        self,
        path: str,
        *,
        method: str = "GET",
    ) -> tuple[int, dict[str, object] | None, dict[str, str]]:
        def perform() -> tuple[int, dict[str, object] | None, dict[str, str]]:
            request = urllib.request.Request(self.base_url + path, method=method)
            try:
                response = urllib.request.urlopen(request, timeout=3.0)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                payload = json.loads(body.decode("utf-8")) if body else None
                return exc.code, payload, dict(exc.headers.items())
            with response:
                body = response.read()
                payload = json.loads(body.decode("utf-8")) if body else None
                return response.status, payload, dict(response.headers.items())

        return await asyncio.to_thread(perform)

    def persist_result(self, source: str, content: str) -> None:
        now = utc_now_ms()
        job_id = f"{source}-job"
        self.broker._store.create_job(
            job_id=job_id,
            source=source,
            context="prompt",
            now_ms=now,
            deadline_at_ms=now + 60_000,
        )
        self.broker._store.mark_dispatched(job_id, now_ms=now + 1)
        self.broker._store.complete(
            job_id,
            content=content,
            wire_type="FINAL",
            now_ms=now + 2,
        )

    async def test_last_result_returns_durable_content_and_metadata(self) -> None:
        self.persist_result("chatgpt", "stored answer")

        status, payload, headers = await self.fetch("/results/last?source=chatgpt")

        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "chatgpt")
        self.assertEqual(payload["state"], "COMPLETED")
        self.assertEqual(payload["content"], "stored answer")
        self.assertEqual(payload["contentBytes"], len("stored answer".encode("utf-8")))
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    async def test_last_result_reports_missing_and_invalid_queries(self) -> None:
        missing_status, missing, _ = await self.fetch("/results/last?source=gemini")
        duplicate_status, duplicate, _ = await self.fetch(
            "/results/last?source=gemini&source=claude"
        )
        extra_status, extra, _ = await self.fetch(
            "/results/last?source=gemini&other=x"
        )

        self.assertEqual(missing_status, 404)
        self.assertEqual(missing["error"]["code"], "NO_COMPLETED_RESULT")
        self.assertEqual(duplicate_status, 400)
        self.assertEqual(duplicate["error"]["code"], "INVALID_QUERY")
        self.assertEqual(extra_status, 400)
        self.assertEqual(extra["error"]["code"], "INVALID_QUERY")

    async def test_non_get_method_is_rejected(self) -> None:
        status, payload, headers = await self.fetch("/results/last?source=chatgpt", method="POST")

        self.assertEqual(status, 405)
        self.assertIsNone(payload)
        self.assertEqual(headers["Allow"], "GET")


if __name__ == "__main__":
    unittest.main()
