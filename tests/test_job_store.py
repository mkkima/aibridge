from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from aibridge.job_store import CreateKind, JobStore, JobStoreError


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "jobs.sqlite3"
        self.store = JobStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def create_job(
        self,
        job_id: str,
        *,
        source: str = "chatgpt",
        context: str = "prompt",
        now_ms: int = 1_000,
        deadline_at_ms: int = 10_000,
    ) -> None:
        created = self.store.create_job(
            job_id=job_id,
            source=source,
            context=context,
            now_ms=now_ms,
            deadline_at_ms=deadline_at_ms,
        )
        self.assertEqual(created.kind, CreateKind.CREATED)

    def test_completed_result_survives_reopen(self) -> None:
        self.create_job("job-1")
        self.assertTrue(self.store.mark_dispatched("job-1", now_ms=2_000).applied)
        self.assertTrue(
            self.store.mark_streaming("job-1", wire_type="TOKEN", now_ms=3_000).applied
        )
        self.assertTrue(
            self.store.complete(
                "job-1",
                content="full result",
                wire_type="FINAL",
                now_ms=4_000,
            ).applied
        )
        self.store.close()

        self.store = JobStore(self.db_path)
        result = self.store.latest_completed("chatgpt")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "job-1")
        self.assertEqual(result.state, "COMPLETED")
        self.assertEqual(result.result_content, "full result")
        self.assertEqual(result.result_bytes, len("full result".encode("utf-8")))
        self.assertEqual(len(result.result_sha256), 64)

    def test_first_terminal_outcome_wins(self) -> None:
        self.create_job("job-1")
        self.store.mark_dispatched("job-1", now_ms=2_000)
        first = self.store.complete(
            "job-1",
            content="first",
            wire_type="FINAL",
            now_ms=3_000,
        )
        second = self.store.fail(
            "job-1",
            code="LATE_ERROR",
            message="must not replace the result",
            now_ms=4_000,
        )
        third = self.store.complete(
            "job-1",
            content="second",
            wire_type="FINAL",
            now_ms=5_000,
        )

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        self.assertFalse(third.applied)
        job = self.store.get_job("job-1")
        self.assertEqual(job.state, "COMPLETED")
        self.assertEqual(job.result_content, "first")

    def test_duplicate_id_distinguishes_identical_request_from_conflict(self) -> None:
        self.create_job("job-1", context="same")

        identical = self.store.create_job(
            job_id="job-1",
            source="chatgpt",
            context="same",
            now_ms=2_000,
            deadline_at_ms=11_000,
        )
        context_conflict = self.store.create_job(
            job_id="job-1",
            source="chatgpt",
            context="different",
            now_ms=2_000,
            deadline_at_ms=11_000,
        )
        source_conflict = self.store.create_job(
            job_id="job-1",
            source="claude",
            context="same",
            now_ms=2_000,
            deadline_at_ms=11_000,
        )

        self.assertEqual(identical.kind, CreateKind.EXISTING_IDENTICAL)
        self.assertEqual(context_conflict.kind, CreateKind.ID_CONFLICT)
        self.assertEqual(source_conflict.kind, CreateKind.ID_CONFLICT)

    def test_recovery_marks_all_non_terminal_states_unknown(self) -> None:
        self.create_job("accepted")
        self.create_job("queued")
        self.store.mark_queued("queued", expires_at_ms=8_000, now_ms=2_000)
        self.create_job("dispatched")
        self.store.mark_dispatched("dispatched", now_ms=2_000)
        self.create_job("streaming")
        self.store.mark_dispatched("streaming", now_ms=2_000)
        self.store.mark_streaming("streaming", wire_type="TOKEN", now_ms=3_000)

        recovered = self.store.recover_incomplete(now_ms=4_000)

        self.assertEqual({job.id for job in recovered}, {
            "accepted",
            "queued",
            "dispatched",
            "streaming",
        })
        for job_id in ("accepted", "queued", "dispatched", "streaming"):
            job = self.store.get_job(job_id)
            self.assertEqual(job.state, "UNKNOWN_OUTCOME")
            self.assertEqual(job.error_code, "BROKER_RESTARTED")

    def test_expiry_distinguishes_pending_timeout_from_uncertain_dispatch(self) -> None:
        self.create_job("queued", deadline_at_ms=20_000)
        self.store.mark_queued("queued", expires_at_ms=2_000, now_ms=1_100)
        self.create_job("dispatched", deadline_at_ms=3_000)
        self.store.mark_dispatched("dispatched", now_ms=1_100)

        expired = self.store.expire_due(now_ms=4_000)

        self.assertEqual({item.job.id for item in expired}, {"queued", "dispatched"})
        queued = self.store.get_job("queued")
        dispatched = self.store.get_job("dispatched")
        self.assertEqual(queued.state, "FAILED")
        self.assertEqual(queued.error_code, "PENDING_TIMEOUT")
        self.assertEqual(dispatched.state, "UNKNOWN_OUTCOME")
        self.assertEqual(dispatched.error_code, "JOB_DEADLINE_EXCEEDED")

    def test_latest_completed_uses_terminal_event_order_per_source(self) -> None:
        self.create_job("chatgpt-old", source="chatgpt")
        self.store.mark_dispatched("chatgpt-old", now_ms=2_000)
        self.store.complete(
            "chatgpt-old",
            content="old",
            wire_type="FINAL",
            now_ms=5_000,
        )
        self.create_job("claude", source="claude")
        self.store.mark_dispatched("claude", now_ms=2_000)
        self.store.complete("claude", content="other", wire_type="FINAL", now_ms=6_000)
        self.create_job("chatgpt-new", source="chatgpt")
        self.store.mark_dispatched("chatgpt-new", now_ms=2_000)
        self.store.complete(
            "chatgpt-new",
            content="new",
            wire_type="FINAL",
            now_ms=4_000,
        )

        self.assertEqual(self.store.latest_completed("chatgpt").id, "chatgpt-new")
        self.assertEqual(self.store.latest_completed("claude").id, "claude")
        self.assertIsNone(self.store.latest_completed("gemini"))

    def test_newer_schema_version_is_rejected(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA user_version = 999")
        connection.close()

        with self.assertRaises(JobStoreError):
            JobStore(self.db_path)


if __name__ == "__main__":
    unittest.main()
