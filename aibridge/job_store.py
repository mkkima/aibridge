from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Literal

SCHEMA_VERSION = 1
APPLICATION_ID = 0x41494252  # "AIBR"
VALID_SOURCES = frozenset({"chatgpt", "gemini", "claude"})
TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "UNKNOWN_OUTCOME"})
NON_TERMINAL_STATES = frozenset({"ACCEPTED", "QUEUED", "DISPATCHED", "STREAMING"})
MAX_EVENT_DETAILS_BYTES = 16 * 1024
MAX_ERROR_MESSAGE_CHARS = 4096


class JobStoreError(RuntimeError):
    pass


class CreateKind(str, Enum):
    CREATED = "created"
    EXISTING_IDENTICAL = "existing_identical"
    ID_CONFLICT = "id_conflict"


@dataclass(frozen=True)
class JobRecord:
    id: str
    source: str
    context_sha256: str
    context_bytes: int
    state: str
    result_content: str | None
    result_sha256: str | None
    result_bytes: int | None
    error_code: str | None
    error_message: str | None
    created_at_ms: int
    updated_at_ms: int
    pending_expires_at_ms: int | None
    deadline_at_ms: int
    terminal_at_ms: int | None
    terminal_event_id: int | None


@dataclass(frozen=True)
class CreateResult:
    kind: CreateKind
    job: JobRecord


@dataclass(frozen=True)
class TransitionResult:
    applied: bool
    job: JobRecord | None


@dataclass(frozen=True)
class ExpiredJob:
    job: JobRecord
    code: str
    message: str


def utc_now_ms() -> int:
    return time.time_ns() // 1_000_000


def default_job_store_path() -> Path:
    configured = os.getenv("AIBRIDGE_DB_PATH")
    if configured:
        return Path(configured).expanduser()

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path(tempfile.gettempdir())
    return base / "AIBridge" / "data" / "jobs-v1.sqlite3"


class JobStore:
    """Single-thread-owned durable job journal for the broker event loop."""

    def __init__(self, path: str | Path | None = None) -> None:
        resolved_path = default_job_store_path() if path is None else path
        self.path = str(resolved_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        try:
            self._connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                check_same_thread=True,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate()
        except (OSError, sqlite3.Error) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise JobStoreError(f"Failed to open AI Bridge job store at {self.path}: {exc}") from exc
        except JobStoreError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise

    def _configure(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")

    def _migrate(self) -> None:
        application_id = int(self._connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])

        if application_id not in (0, APPLICATION_ID):
            raise JobStoreError(
                f"Database at {self.path} belongs to another application "
                f"(application_id={application_id})"
            )
        if application_id == 0 and user_version != 0:
            raise JobStoreError(
                f"Database at {self.path} has a schema version but no AI Bridge application id"
            )
        if user_version > SCHEMA_VERSION:
            raise JobStoreError(
                f"Database schema {user_version} is newer than supported schema {SCHEMA_VERSION}"
            )

        if user_version == 0:
            self._connection.executescript(
                f"""
                    BEGIN IMMEDIATE;
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL CHECK (source IN ('chatgpt', 'gemini', 'claude')),
                        context_sha256 TEXT NOT NULL CHECK (length(context_sha256) = 64),
                        context_bytes INTEGER NOT NULL CHECK (context_bytes > 0),
                        state TEXT NOT NULL CHECK (state IN (
                            'ACCEPTED', 'QUEUED', 'DISPATCHED', 'STREAMING',
                            'COMPLETED', 'FAILED', 'UNKNOWN_OUTCOME'
                        )),
                        result_content TEXT,
                        result_sha256 TEXT,
                        result_bytes INTEGER,
                        error_code TEXT,
                        error_message TEXT,
                        created_at_ms INTEGER NOT NULL,
                        updated_at_ms INTEGER NOT NULL,
                        pending_expires_at_ms INTEGER,
                        deadline_at_ms INTEGER NOT NULL,
                        terminal_at_ms INTEGER,
                        terminal_event_id INTEGER,
                        CHECK (deadline_at_ms > created_at_ms),
                        CHECK (
                            (state = 'QUEUED' AND pending_expires_at_ms IS NOT NULL)
                            OR state <> 'QUEUED'
                        ),
                        CHECK (
                            (state IN ('COMPLETED', 'FAILED', 'UNKNOWN_OUTCOME')
                                AND terminal_at_ms IS NOT NULL
                                AND terminal_event_id IS NOT NULL)
                            OR
                            (state NOT IN ('COMPLETED', 'FAILED', 'UNKNOWN_OUTCOME')
                                AND terminal_at_ms IS NULL
                                AND terminal_event_id IS NULL)
                        ),
                        CHECK (
                            (state = 'COMPLETED'
                                AND result_content IS NOT NULL
                                AND length(trim(result_content)) > 0
                                AND result_sha256 IS NOT NULL
                                AND length(result_sha256) = 64
                                AND result_bytes > 0
                                AND error_code IS NULL
                                AND error_message IS NULL)
                            OR state <> 'COMPLETED'
                        ),
                        CHECK (
                            state = 'COMPLETED'
                            OR (result_content IS NULL
                                AND result_sha256 IS NULL
                                AND result_bytes IS NULL)
                        ),
                        CHECK (
                            (state IN ('FAILED', 'UNKNOWN_OUTCOME') AND error_code IS NOT NULL)
                            OR state NOT IN ('FAILED', 'UNKNOWN_OUTCOME')
                        )
                    );

                    CREATE TABLE job_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT,
                        applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
                        created_at_ms INTEGER NOT NULL,
                        details_json TEXT
                    );

                    CREATE INDEX jobs_by_state ON jobs(state);
                    CREATE INDEX queued_jobs_by_expiry
                        ON jobs(pending_expires_at_ms) WHERE state = 'QUEUED';
                    CREATE INDEX active_jobs_by_deadline
                        ON jobs(deadline_at_ms)
                        WHERE state IN ('ACCEPTED', 'DISPATCHED', 'STREAMING');
                    CREATE INDEX last_completed_by_source
                        ON jobs(source, terminal_event_id DESC) WHERE state = 'COMPLETED';
                    CREATE INDEX events_by_job ON job_events(job_id, event_id);
                    PRAGMA application_id = {APPLICATION_ID};
                    PRAGMA user_version = {SCHEMA_VERSION};
                    COMMIT;
                """
            )
        elif application_id != APPLICATION_ID:
            raise JobStoreError(f"Database at {self.path} is missing the AI Bridge application id")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise JobStoreError(f"Could not start a job store transaction: {exc}") from exc
        try:
            yield
        except sqlite3.Error as exc:
            self._connection.execute("ROLLBACK")
            raise JobStoreError(f"Job store transaction failed: {exc}") from exc
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def create_job(
        self,
        *,
        job_id: str,
        source: str,
        context: str,
        deadline_at_ms: int,
        now_ms: int | None = None,
    ) -> CreateResult:
        now = utc_now_ms() if now_ms is None else now_ms
        normalized_source = self._validate_source(source)
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            raise ValueError("job_id must contain 1 to 128 characters")
        if not isinstance(context, str) or not context.strip():
            raise ValueError("context must be a non-empty string")
        if deadline_at_ms <= now:
            raise ValueError("deadline_at_ms must be in the future")

        context_raw = context.encode("utf-8", errors="strict")
        context_hash = hashlib.sha256(context_raw).hexdigest()
        context_bytes = len(context_raw)

        with self._transaction():
            try:
                self._connection.execute(
                    """
                    INSERT INTO jobs (
                        id, source, context_sha256, context_bytes, state,
                        created_at_ms, updated_at_ms, deadline_at_ms
                    ) VALUES (?, ?, ?, ?, 'ACCEPTED', ?, ?, ?)
                    """,
                    (
                        job_id,
                        normalized_source,
                        context_hash,
                        context_bytes,
                        now,
                        now,
                        deadline_at_ms,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self._get_job_locked(job_id)
                if existing is None:
                    raise
                identical = (
                    existing.source == normalized_source
                    and existing.context_sha256 == context_hash
                    and existing.context_bytes == context_bytes
                )
                return CreateResult(
                    CreateKind.EXISTING_IDENTICAL if identical else CreateKind.ID_CONFLICT,
                    existing,
                )

            self._insert_event_locked(
                job_id=job_id,
                event_type="JOB_ACCEPTED",
                from_state=None,
                to_state="ACCEPTED",
                applied=True,
                now_ms=now,
            )
            created = self._get_job_locked(job_id)
            assert created is not None
            return CreateResult(CreateKind.CREATED, created)

    def get_job(self, job_id: str) -> JobRecord | None:
        try:
            return self._get_job_locked(job_id)
        except sqlite3.Error as exc:
            raise JobStoreError(f"Could not read job {job_id}: {exc}") from exc

    def mark_queued(
        self,
        job_id: str,
        *,
        expires_at_ms: int,
        now_ms: int | None = None,
    ) -> TransitionResult:
        return self._transition(
            job_id,
            to_state="QUEUED",
            allowed_from=("ACCEPTED",),
            event_type="JOB_QUEUED",
            now_ms=now_ms,
            fields={"pending_expires_at_ms": expires_at_ms},
        )

    def mark_dispatched(self, job_id: str, *, now_ms: int | None = None) -> TransitionResult:
        return self._transition(
            job_id,
            to_state="DISPATCHED",
            allowed_from=("ACCEPTED", "QUEUED"),
            event_type="DISPATCH_ATTEMPTED",
            now_ms=now_ms,
            fields={"pending_expires_at_ms": None},
        )

    def mark_streaming(
        self,
        job_id: str,
        *,
        wire_type: str,
        now_ms: int | None = None,
    ) -> TransitionResult:
        current = self.get_job(job_id)
        if current is not None and current.state == "STREAMING":
            return TransitionResult(False, current)
        return self._transition(
            job_id,
            to_state="STREAMING",
            allowed_from=("DISPATCHED",),
            event_type="STREAM_STARTED",
            now_ms=now_ms,
            details={"wireType": wire_type},
        )

    def complete(
        self,
        job_id: str,
        *,
        content: str,
        wire_type: str,
        now_ms: int | None = None,
    ) -> TransitionResult:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("completed content must be a non-empty string")
        content_raw = content.encode("utf-8", errors="strict")
        return self._transition(
            job_id,
            to_state="COMPLETED",
            allowed_from=("DISPATCHED", "STREAMING"),
            event_type="JOB_COMPLETED",
            now_ms=now_ms,
            fields={
                "result_content": content,
                "result_sha256": hashlib.sha256(content_raw).hexdigest(),
                "result_bytes": len(content_raw),
                "error_code": None,
                "error_message": None,
            },
            details={"wireType": wire_type, "contentBytes": len(content_raw)},
        )

    def fail(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        wire_type: str | None = None,
        now_ms: int | None = None,
    ) -> TransitionResult:
        details: dict[str, Any] = {"code": code}
        if wire_type:
            details["wireType"] = wire_type
        return self._transition(
            job_id,
            to_state="FAILED",
            allowed_from=tuple(sorted(NON_TERMINAL_STATES)),
            event_type="JOB_FAILED",
            now_ms=now_ms,
            fields={
                "pending_expires_at_ms": None,
                "error_code": self._validate_error_code(code),
                "error_message": self._normalize_error_message(message),
            },
            details=details,
        )

    def mark_unknown(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        now_ms: int | None = None,
    ) -> TransitionResult:
        return self._transition(
            job_id,
            to_state="UNKNOWN_OUTCOME",
            allowed_from=tuple(sorted(NON_TERMINAL_STATES)),
            event_type="JOB_MARKED_UNKNOWN",
            now_ms=now_ms,
            fields={
                "pending_expires_at_ms": None,
                "error_code": self._validate_error_code(code),
                "error_message": self._normalize_error_message(message),
            },
            details={"code": code},
        )

    def recover_incomplete(self, *, now_ms: int | None = None) -> list[JobRecord]:
        now = utc_now_ms() if now_ms is None else now_ms
        recovered: list[JobRecord] = []
        with self._transaction():
            rows = self._connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN ('ACCEPTED', 'QUEUED', 'DISPATCHED', 'STREAMING')
                ORDER BY created_at_ms, id
                """
            ).fetchall()
            for row in rows:
                current = self._row_to_job(row)
                result = self._transition_locked(
                    current,
                    to_state="UNKNOWN_OUTCOME",
                    allowed_from=tuple(sorted(NON_TERMINAL_STATES)),
                    event_type="JOB_MARKED_UNKNOWN",
                    now_ms=now,
                    fields={
                        "pending_expires_at_ms": None,
                        "error_code": "BROKER_RESTARTED",
                        "error_message": "Broker restarted before a terminal result was committed",
                    },
                    details={"code": "BROKER_RESTARTED"},
                )
                if result.applied and result.job is not None:
                    recovered.append(result.job)
        return recovered

    def expire_due(self, *, now_ms: int | None = None) -> list[ExpiredJob]:
        now = utc_now_ms() if now_ms is None else now_ms
        expired: list[ExpiredJob] = []
        with self._transaction():
            rows = self._connection.execute(
                """
                SELECT * FROM jobs
                WHERE
                    (state = 'QUEUED' AND pending_expires_at_ms <= ?)
                    OR
                    (state IN ('ACCEPTED', 'DISPATCHED', 'STREAMING') AND deadline_at_ms <= ?)
                ORDER BY updated_at_ms, id
                """,
                (now, now),
            ).fetchall()
            for row in rows:
                current = self._row_to_job(row)
                if current.state == "QUEUED" and current.pending_expires_at_ms is not None:
                    code = "PENDING_TIMEOUT"
                    message = "No matching browser tab accepted the request before the pending deadline"
                    result = self._transition_locked(
                        current,
                        to_state="FAILED",
                        allowed_from=("QUEUED",),
                        event_type="JOB_FAILED",
                        now_ms=now,
                        fields={
                            "pending_expires_at_ms": None,
                            "error_code": code,
                            "error_message": message,
                        },
                        details={"code": code},
                    )
                else:
                    code = "JOB_DEADLINE_EXCEEDED"
                    message = "The request deadline elapsed without a terminal browser result"
                    result = self._transition_locked(
                        current,
                        to_state="UNKNOWN_OUTCOME",
                        allowed_from=("ACCEPTED", "DISPATCHED", "STREAMING"),
                        event_type="JOB_MARKED_UNKNOWN",
                        now_ms=now,
                        fields={
                            "pending_expires_at_ms": None,
                            "error_code": code,
                            "error_message": message,
                        },
                        details={"code": code},
                    )
                if result.applied and result.job is not None:
                    expired.append(ExpiredJob(result.job, code, message))
        return expired

    def latest_completed(self, source: str) -> JobRecord | None:
        normalized_source = self._validate_source(source)
        try:
            row = self._connection.execute(
                """
                SELECT * FROM jobs
                WHERE source = ? AND state = 'COMPLETED'
                ORDER BY terminal_event_id DESC
                LIMIT 1
                """,
                (normalized_source,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise JobStoreError(
                f"Could not read the latest completed result for {normalized_source}: {exc}"
            ) from exc
        return self._row_to_job(row) if row is not None else None

    def close(self) -> None:
        self._connection.close()

    def _transition(
        self,
        job_id: str,
        *,
        to_state: str,
        allowed_from: tuple[str, ...],
        event_type: str,
        now_ms: int | None,
        fields: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> TransitionResult:
        now = utc_now_ms() if now_ms is None else now_ms
        with self._transaction():
            current = self._get_job_locked(job_id)
            if current is None:
                return TransitionResult(False, None)
            return self._transition_locked(
                current,
                to_state=to_state,
                allowed_from=allowed_from,
                event_type=event_type,
                now_ms=now,
                fields=fields,
                details=details,
            )

    def _transition_locked(
        self,
        current: JobRecord,
        *,
        to_state: str,
        allowed_from: tuple[str, ...],
        event_type: str,
        now_ms: int,
        fields: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> TransitionResult:
        if current.state not in allowed_from:
            self._insert_event_locked(
                job_id=current.id,
                event_type=event_type,
                from_state=current.state,
                to_state=to_state,
                applied=False,
                now_ms=now_ms,
                details=details,
            )
            return TransitionResult(False, current)

        event_id = self._insert_event_locked(
            job_id=current.id,
            event_type=event_type,
            from_state=current.state,
            to_state=to_state,
            applied=True,
            now_ms=now_ms,
            details=details,
        )
        updates: dict[str, Any] = {
            "state": to_state,
            "updated_at_ms": now_ms,
            **(fields or {}),
        }
        if to_state in TERMINAL_STATES:
            updates["terminal_at_ms"] = now_ms
            updates["terminal_event_id"] = event_id

        assignments = ", ".join(f"{column} = ?" for column in updates)
        values = [*updates.values(), current.id, current.state]
        cursor = self._connection.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ? AND state = ?",
            values,
        )
        if cursor.rowcount != 1:
            raise JobStoreError(f"Concurrent state transition detected for job {current.id}")

        updated = self._get_job_locked(current.id)
        assert updated is not None
        return TransitionResult(True, updated)

    def _insert_event_locked(
        self,
        *,
        job_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        applied: bool,
        now_ms: int,
        details: dict[str, Any] | None = None,
    ) -> int:
        details_json = None
        if details is not None:
            details_json = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
            if len(details_json.encode("utf-8")) > MAX_EVENT_DETAILS_BYTES:
                raise ValueError("job event details are too large")
        cursor = self._connection.execute(
            """
            INSERT INTO job_events (
                job_id, event_type, from_state, to_state, applied, created_at_ms, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, event_type, from_state, to_state, int(applied), now_ms, details_json),
        )
        return int(cursor.lastrowid)

    def _get_job_locked(self, job_id: str) -> JobRecord | None:
        row = self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row is not None else None

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            source=str(row["source"]),
            context_sha256=str(row["context_sha256"]),
            context_bytes=int(row["context_bytes"]),
            state=str(row["state"]),
            result_content=row["result_content"],
            result_sha256=row["result_sha256"],
            result_bytes=row["result_bytes"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
            pending_expires_at_ms=row["pending_expires_at_ms"],
            deadline_at_ms=int(row["deadline_at_ms"]),
            terminal_at_ms=row["terminal_at_ms"],
            terminal_event_id=row["terminal_event_id"],
        )

    @staticmethod
    def _validate_source(source: str) -> str:
        if not isinstance(source, str):
            raise ValueError("source must be a string")
        normalized = source.strip().casefold()
        if normalized not in VALID_SOURCES:
            raise ValueError(f"unsupported source: {source}")
        return normalized

    @staticmethod
    def _validate_error_code(code: str) -> str:
        if not isinstance(code, str) or not code or len(code) > 64:
            raise ValueError("error code must contain 1 to 64 characters")
        if not all(
            "A" <= character <= "Z" or "0" <= character <= "9" or character == "_"
            for character in code
        ):
            raise ValueError("error code must use uppercase ASCII-style identifiers")
        return code

    @staticmethod
    def _normalize_error_message(message: str) -> str:
        normalized = str(message or "AI Bridge job failed")
        return normalized[:MAX_ERROR_MESSAGE_CHARS]
