from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.server import WebSocketServerProtocol

from .job_store import (
    CreateKind,
    JobRecord,
    JobStore,
    JobStoreError,
    NON_TERMINAL_STATES,
    TERMINAL_STATES,
    VALID_SOURCES,
    utc_now_ms,
)

LOGGER = logging.getLogger(__name__)

HOST = os.getenv("AIBRIDGE_HOST", "127.0.0.1")
WS_PORT = int(os.getenv("AIBRIDGE_WS_PORT", "8765"))
HTTP_PORT = int(os.getenv("AIBRIDGE_HTTP_PORT", "8766"))
MAX_MESSAGE_BYTES = int(os.getenv("AIBRIDGE_MAX_MESSAGE_BYTES", str(8 * 1024 * 1024)))
MAX_PENDING_REQUESTS = int(os.getenv("AIBRIDGE_MAX_PENDING_REQUESTS", "100"))
PENDING_TTL_SECONDS = float(os.getenv("AIBRIDGE_PENDING_TTL_SECONDS", "300"))
JOB_TIMEOUT_SECONDS = float(os.getenv("AIBRIDGE_JOB_TIMEOUT_SECONDS", str(6 * 60 * 60)))
MAINTENANCE_INTERVAL_SECONDS = float(os.getenv("AIBRIDGE_MAINTENANCE_INTERVAL_SECONDS", "1"))
MAX_HTTP_QUERY_BYTES = 1024
MAX_HTTP_RESPONSE_BYTES = MAX_MESSAGE_BYTES + 64 * 1024
SERVICE_NAME = "aibridge"
SERVICE_VERSION = "0.2.0"

READY_TYPES = {"BRIDGE_READY", "BRIDGE_HEARTBEAT"}
RESPONSE_TYPES = {
    "STATUS",
    "TOKEN",
    "COMPLETION_TOKEN",
    "REPLACE",
    "COMPLETION_REPLACE",
    "FINAL",
    "COMPLETION_FINAL",
    "DONE",
    "COMPLETION_DONE",
    "ERROR",
    "COMPLETION_ERROR",
}
CANONICAL_RESPONSE_TYPES = {
    "STATUS": "STATUS",
    "TOKEN": "TOKEN",
    "COMPLETION_TOKEN": "TOKEN",
    "REPLACE": "REPLACE",
    "COMPLETION_REPLACE": "REPLACE",
    "FINAL": "FINAL",
    "COMPLETION_FINAL": "FINAL",
    "DONE": "DONE",
    "COMPLETION_DONE": "DONE",
    "ERROR": "ERROR",
    "COMPLETION_ERROR": "ERROR",
}


@dataclass
class ExtensionClient:
    id: int
    websocket: WebSocketServerProtocol
    source: str = "none"
    sources: list[str] = field(default_factory=list)


@dataclass
class PendingRequest:
    raw: str
    source: str
    expires_at_ms: int


@dataclass(frozen=True)
class TabReadRequest:
    source: str
    extension_client_id: int


class BridgeBroker:
    def __init__(
        self,
        store: JobStore | None = None,
        *,
        recover_incomplete: bool = True,
    ) -> None:
        self._lock = asyncio.Lock()
        self._next_client_id = 0
        self._extension_clients: dict[int, ExtensionClient] = {}
        self._core_clients_by_request: dict[str, WebSocketServerProtocol] = {}
        self._pending_requests: dict[str, PendingRequest] = {}
        self._tab_read_requests: dict[str, TabReadRequest] = {}
        self._request_ids_by_client: dict[int, set[str]] = {}
        self._extension_sources: list[str] = []
        self._ready = False
        self._store = store or JobStore()

        if recover_incomplete:
            recovered = self._store.recover_incomplete()
            if recovered:
                LOGGER.warning(
                    "Marked %d incomplete AI Bridge jobs as UNKNOWN_OUTCOME after restart",
                    len(recovered),
                )

    async def set_ready(self, ready: bool) -> None:
        async with self._lock:
            self._ready = ready

    async def handle_client(self, websocket: WebSocketServerProtocol) -> None:
        async with self._lock:
            client_id = self._next_client_id
            self._next_client_id += 1
            self._request_ids_by_client[client_id] = set()

        try:
            async for raw_message in websocket:
                await self._handle_raw_message(client_id, websocket, raw_message)
        except websockets.ConnectionClosed as exc:
            LOGGER.info(
                "AI Bridge WebSocket client disconnected: code=%s reason=%s",
                exc.code,
                exc.reason,
            )
        finally:
            await self._remove_client(client_id, websocket)

    async def _handle_raw_message(
        self,
        client_id: int,
        websocket: WebSocketServerProtocol,
        raw_message: str | bytes,
    ) -> None:
        if isinstance(raw_message, bytes):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return

        if len(raw_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            await self._safe_send_json(
                websocket,
                {
                    "type": "ERROR",
                    "code": "MESSAGE_TOO_LARGE",
                    "message": f"Message exceeds {MAX_MESSAGE_BYTES} bytes",
                },
            )
            return

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        if not isinstance(payload, dict):
            return

        event_type = payload.get("type")
        if not isinstance(event_type, str):
            return

        if event_type == "COMPLETION_REQUEST":
            await self._handle_completion_request(client_id, websocket, payload, raw_message)
        elif event_type in READY_TYPES:
            await self._handle_extension_ready(client_id, websocket, payload)
        elif event_type in RESPONSE_TYPES:
            await self._handle_completion_response(payload, raw_message)

    async def _handle_completion_request(
        self,
        client_id: int,
        websocket: WebSocketServerProtocol,
        payload: dict[str, Any],
        raw_message: str,
    ) -> None:
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            await self._send_error(
                websocket,
                None,
                "INVALID_REQUEST_ID",
                "COMPLETION_REQUEST requires an id containing 1 to 128 characters",
            )
            return

        source = payload.get("source")
        if not isinstance(source, str) or source.strip().casefold() not in VALID_SOURCES:
            await self._send_error(
                websocket,
                request_id,
                "INVALID_SOURCE",
                "COMPLETION_REQUEST requires source chatgpt, gemini, or claude",
            )
            return
        source = source.strip().casefold()

        read_current_tab = payload.get("readCurrentTab", False)
        if type(read_current_tab) is not bool:
            await self._send_error(
                websocket,
                request_id,
                "INVALID_READ_CURRENT_TAB",
                "readCurrentTab must be a boolean",
            )
            return

        context = payload.get("context")
        if read_current_tab:
            if context != "":
                await self._send_error(
                    websocket,
                    request_id,
                    "INVALID_TAB_READ_REQUEST",
                    "A current-tab read must have an empty context",
                )
                return
            await self._handle_tab_read_request(
                client_id,
                websocket,
                request_id,
                source,
                raw_message,
            )
            return

        if not isinstance(context, str) or not context.strip():
            await self._send_error(
                websocket,
                request_id,
                "EMPTY_CONTEXT_NOT_SUPPORTED",
                "Prompt context must be non-empty; use ai --get-last to read stored output",
            )
            return

        now_ms = utc_now_ms()
        try:
            created = self._store.create_job(
                job_id=request_id,
                source=source,
                context=context,
                deadline_at_ms=now_ms + max(1, int(JOB_TIMEOUT_SECONDS * 1000)),
                now_ms=now_ms,
            )
        except (JobStoreError, OSError, ValueError) as exc:
            LOGGER.exception("Failed to persist AI Bridge request %s", request_id)
            await self._send_error(
                websocket,
                request_id,
                "STORE_UNAVAILABLE",
                f"AI Bridge could not persist the request: {exc}",
            )
            return

        if created.kind is not CreateKind.CREATED:
            await self._handle_duplicate_request(websocket, created.kind, created.job)
            return

        extension: ExtensionClient | None = None
        error: tuple[str, str] | None = None
        async with self._lock:
            self._core_clients_by_request[request_id] = websocket
            self._request_ids_by_client.setdefault(client_id, set()).add(request_id)
            extension = self._select_extension_locked(source)

            try:
                if extension is None:
                    if len(self._pending_requests) >= MAX_PENDING_REQUESTS:
                        self._store.fail(
                            request_id,
                            code="PENDING_QUEUE_FULL",
                            message="AI Bridge pending request queue is full",
                            now_ms=now_ms,
                        )
                        self._clean_routing_locked(request_id)
                        error = ("PENDING_QUEUE_FULL", "AI Bridge pending request queue is full")
                    else:
                        expires_at_ms = now_ms + max(1, int(PENDING_TTL_SECONDS * 1000))
                        transition = self._store.mark_queued(
                            request_id,
                            expires_at_ms=expires_at_ms,
                            now_ms=now_ms,
                        )
                        if not transition.applied:
                            raise JobStoreError(f"Could not queue job {request_id}")
                        self._pending_requests[request_id] = PendingRequest(
                            raw=raw_message,
                            source=source,
                            expires_at_ms=expires_at_ms,
                        )
                else:
                    transition = self._store.mark_dispatched(request_id, now_ms=now_ms)
                    if not transition.applied:
                        raise JobStoreError(f"Could not mark job {request_id} as dispatched")
            except (JobStoreError, OSError, ValueError) as exc:
                LOGGER.exception("Failed to update durable state for request %s", request_id)
                self._clean_routing_locked(request_id)
                error = ("STORE_UNAVAILABLE", f"AI Bridge could not update request state: {exc}")

        if error is not None:
            await self._send_error(websocket, request_id, *error)
            return
        if extension is None:
            return

        if not await self._safe_send(extension.websocket, raw_message):
            await self._mark_unknown_and_notify(
                request_id,
                code="DISPATCH_CONNECTION_LOST",
                message="The extension connection was lost during prompt dispatch; submission is uncertain",
            )

    async def _handle_tab_read_request(
        self,
        client_id: int,
        websocket: WebSocketServerProtocol,
        request_id: str,
        source: str,
        raw_message: str,
    ) -> None:
        extension: ExtensionClient | None = None
        error: tuple[str, str] | None = None

        async with self._lock:
            if request_id in self._core_clients_by_request:
                error = ("REQUEST_ID_CONFLICT", "The request id is already active")
            elif len(self._tab_read_requests) >= MAX_PENDING_REQUESTS:
                error = ("TAB_READ_LIMIT", "Too many current-tab reads are active")
            else:
                extension = self._select_extension_locked(source)
                if extension is None:
                    error = (
                        "OPEN_TAB_NOT_CONNECTED",
                        f"Open a {source} tab with the AI Bridge extension first",
                    )
                else:
                    self._core_clients_by_request[request_id] = websocket
                    self._request_ids_by_client.setdefault(client_id, set()).add(request_id)
                    self._tab_read_requests[request_id] = TabReadRequest(
                        source=source,
                        extension_client_id=extension.id,
                    )

        if error is not None:
            await self._send_error(websocket, request_id, *error)
            return
        if extension is None:
            return

        if not await self._safe_send(extension.websocket, raw_message):
            async with self._lock:
                self._clean_routing_locked(request_id)
            await self._send_error(
                websocket,
                request_id,
                "TAB_READ_DISPATCH_FAILED",
                "The extension connection was lost while reading the open tab",
            )

    async def _handle_duplicate_request(
        self,
        websocket: WebSocketServerProtocol,
        kind: CreateKind,
        job: JobRecord,
    ) -> None:
        if kind is CreateKind.ID_CONFLICT:
            await self._send_error(
                websocket,
                job.id,
                "REQUEST_ID_CONFLICT",
                "The request id is already associated with a different source or prompt",
            )
            return

        if job.state == "COMPLETED" and job.result_content:
            await self._safe_send_json(
                websocket,
                {
                    "type": "FINAL",
                    "id": job.id,
                    "source": job.source,
                    "content": job.result_content,
                },
            )
            return
        if job.state in TERMINAL_STATES:
            await self._send_error(
                websocket,
                job.id,
                job.error_code or "PRIOR_TERMINAL_OUTCOME",
                job.error_message or f"The previous request ended as {job.state}",
            )
            return

        await self._send_error(
            websocket,
            job.id,
            "REQUEST_ALREADY_IN_PROGRESS",
            "The request id is already active and will not be dispatched twice",
        )

    async def _handle_extension_ready(
        self,
        client_id: int,
        websocket: WebSocketServerProtocol,
        payload: dict[str, Any],
    ) -> None:
        source = self._normalize_source(payload.get("source")) or "none"
        sources = self._normalize_sources(payload.get("sources"), source)
        deliveries: list[tuple[str, PendingRequest]] = []
        store_failures: list[tuple[str, WebSocketServerProtocol | None, str]] = []
        now_ms = utc_now_ms()

        async with self._lock:
            self._extension_clients[client_id] = ExtensionClient(
                id=client_id,
                websocket=websocket,
                source=source,
                sources=sources,
            )
            self._rebuild_sources_locked()

            for request_id, pending in list(self._pending_requests.items()):
                if pending.source not in sources or pending.expires_at_ms <= now_ms:
                    continue
                try:
                    transition = self._store.mark_dispatched(request_id, now_ms=now_ms)
                except (JobStoreError, OSError, ValueError) as exc:
                    LOGGER.exception("Failed to claim pending request %s", request_id)
                    core = self._core_clients_by_request.get(request_id)
                    self._pending_requests.pop(request_id, None)
                    self._clean_routing_locked(request_id)
                    store_failures.append((request_id, core, str(exc)))
                    continue
                if not transition.applied:
                    self._pending_requests.pop(request_id, None)
                    self._clean_routing_locked(request_id)
                    continue
                self._pending_requests.pop(request_id, None)
                deliveries.append((request_id, pending))

        for request_id, core, detail in store_failures:
            if core is not None:
                await self._send_error(
                    core,
                    request_id,
                    "STORE_UNAVAILABLE",
                    f"AI Bridge could not dispatch the queued request: {detail}",
                )

        for request_id, pending in deliveries:
            if not await self._safe_send(websocket, pending.raw):
                await self._mark_unknown_and_notify(
                    request_id,
                    code="DISPATCH_CONNECTION_LOST",
                    message="The extension connection was lost during queued prompt dispatch; submission is uncertain",
                )

    async def _handle_completion_response(
        self,
        payload: dict[str, Any],
        raw_message: str,
    ) -> None:
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            return

        async with self._lock:
            tab_read = self._tab_read_requests.get(request_id)
        if tab_read is not None:
            await self._handle_tab_read_response(tab_read, payload, raw_message)
            return

        try:
            job = self._store.get_job(request_id)
        except (JobStoreError, OSError) as exc:
            await self._handle_store_failure(request_id, exc)
            return
        if job is None or job.state in TERMINAL_STATES:
            return

        response_source = payload.get("source")
        if response_source is not None:
            normalized_response_source = self._normalize_source(response_source)
            if normalized_response_source != job.source:
                await self._fail_job_and_notify(
                    request_id,
                    code="RESPONSE_SOURCE_MISMATCH",
                    message=f"Expected response from {job.source}, received {response_source}",
                )
                return

        wire_type = str(payload.get("type"))
        event_type = CANONICAL_RESPONSE_TYPES.get(wire_type)
        if event_type is None:
            return

        if event_type == "STATUS":
            await self._forward_to_core(request_id, raw_message)
            return

        if event_type == "TOKEN":
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                await self._fail_job_and_notify(
                    request_id,
                    code="INVALID_STREAM_EVENT",
                    message="Browser TOKEN event did not contain text",
                    wire_type=wire_type,
                )
                return
            if not await self._mark_streaming(request_id, wire_type):
                return
            await self._forward_to_core(request_id, raw_message)
            return

        if event_type == "REPLACE":
            content = payload.get("content")
            if not isinstance(content, str):
                await self._fail_job_and_notify(
                    request_id,
                    code="INVALID_STREAM_EVENT",
                    message="Browser REPLACE event did not contain text",
                    wire_type=wire_type,
                )
                return
            if not await self._mark_streaming(request_id, wire_type):
                return
            await self._forward_to_core(request_id, raw_message)
            return

        if event_type == "FINAL":
            content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                await self._fail_job_and_notify(
                    request_id,
                    code="INVALID_FINAL_CONTENT",
                    message="Browser FINAL event did not contain a non-empty full result",
                    wire_type=wire_type,
                )
                return
            try:
                content_size = len(content.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                await self._fail_job_and_notify(
                    request_id,
                    code="INVALID_FINAL_CONTENT",
                    message="Browser FINAL content is not valid UTF-8 text",
                    wire_type=wire_type,
                )
                return
            if content_size > MAX_MESSAGE_BYTES:
                await self._fail_job_and_notify(
                    request_id,
                    code="FINAL_CONTENT_TOO_LARGE",
                    message=f"Browser FINAL content exceeds {MAX_MESSAGE_BYTES} bytes",
                    wire_type=wire_type,
                )
                return
            try:
                transition = self._store.complete(
                    request_id,
                    content=content,
                    wire_type=wire_type,
                )
            except (JobStoreError, OSError, ValueError) as exc:
                await self._handle_store_failure(request_id, exc)
                return
            if not transition.applied:
                return
            async with self._lock:
                core = self._core_clients_by_request.get(request_id)
                self._clean_routing_locked(request_id)
            if core is not None:
                await self._safe_send(core, raw_message)
            return

        if event_type == "ERROR":
            await self._fail_job_and_notify(
                request_id,
                code="BROWSER_ERROR",
                message=str(payload.get("message") or "Browser adapter returned an error"),
                wire_type=wire_type,
            )
            return

        if event_type == "DONE":
            await self._fail_job_and_notify(
                request_id,
                code="DONE_WITHOUT_FINAL",
                message="Browser adapter ended without a valid FINAL result",
                wire_type=wire_type,
            )

    async def _handle_tab_read_response(
        self,
        tab_read: TabReadRequest,
        payload: dict[str, Any],
        raw_message: str,
    ) -> None:
        request_id = str(payload["id"])
        response_source = payload.get("source")
        if response_source is not None:
            normalized_source = self._normalize_source(response_source)
            if normalized_source != tab_read.source:
                await self._finish_tab_read_with_error(
                    request_id,
                    "RESPONSE_SOURCE_MISMATCH",
                    f"Expected response from {tab_read.source}, received {response_source}",
                )
                return

        wire_type = str(payload.get("type"))
        event_type = CANONICAL_RESPONSE_TYPES.get(wire_type)
        if event_type is None:
            return
        if event_type == "STATUS":
            await self._forward_to_core(request_id, raw_message)
            return
        if event_type == "TOKEN":
            token = payload.get("token")
            if not isinstance(token, str) or not token:
                await self._finish_tab_read_with_error(
                    request_id,
                    "INVALID_STREAM_EVENT",
                    "Browser TOKEN event did not contain text",
                )
                return
            await self._forward_to_core(request_id, raw_message)
            return
        if event_type == "REPLACE":
            if not isinstance(payload.get("content"), str):
                await self._finish_tab_read_with_error(
                    request_id,
                    "INVALID_STREAM_EVENT",
                    "Browser REPLACE event did not contain text",
                )
                return
            await self._forward_to_core(request_id, raw_message)
            return
        if event_type == "FINAL":
            content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                await self._finish_tab_read_with_error(
                    request_id,
                    "NO_OPEN_TAB_RESPONSE",
                    "No assistant response was found in the open tab",
                )
                return
            try:
                content_size = len(content.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                await self._finish_tab_read_with_error(
                    request_id,
                    "INVALID_FINAL_CONTENT",
                    "Browser FINAL content is not valid UTF-8 text",
                )
                return
            if content_size > MAX_MESSAGE_BYTES:
                await self._finish_tab_read_with_error(
                    request_id,
                    "FINAL_CONTENT_TOO_LARGE",
                    f"Browser FINAL content exceeds {MAX_MESSAGE_BYTES} bytes",
                )
                return

            async with self._lock:
                core = self._core_clients_by_request.get(request_id)
                self._clean_routing_locked(request_id)
            if core is not None:
                await self._safe_send(core, raw_message)
            return
        if event_type == "ERROR":
            await self._finish_tab_read_with_error(
                request_id,
                "BROWSER_ERROR",
                str(payload.get("message") or "Browser adapter returned an error"),
            )
            return
        if event_type == "DONE":
            await self._finish_tab_read_with_error(
                request_id,
                "DONE_WITHOUT_FINAL",
                "Browser adapter ended without a readable open-tab response",
            )

    async def _finish_tab_read_with_error(
        self,
        request_id: str,
        code: str,
        message: str,
    ) -> None:
        async with self._lock:
            core = self._core_clients_by_request.get(request_id)
            self._clean_routing_locked(request_id)
        if core is not None:
            await self._send_error(core, request_id, code, message)

    async def _mark_streaming(self, request_id: str, wire_type: str) -> bool:
        try:
            transition = self._store.mark_streaming(request_id, wire_type=wire_type)
        except (JobStoreError, OSError, ValueError) as exc:
            await self._handle_store_failure(request_id, exc)
            return False
        if transition.job is None or transition.job.state not in {"STREAMING"}:
            return False
        return True

    async def _fail_job_and_notify(
        self,
        request_id: str,
        *,
        code: str,
        message: str,
        wire_type: str | None = None,
    ) -> None:
        try:
            transition = self._store.fail(
                request_id,
                code=code,
                message=message,
                wire_type=wire_type,
            )
        except (JobStoreError, OSError, ValueError) as exc:
            await self._handle_store_failure(request_id, exc)
            return
        if not transition.applied:
            return

        async with self._lock:
            core = self._core_clients_by_request.get(request_id)
            self._pending_requests.pop(request_id, None)
            self._clean_routing_locked(request_id)
        if core is not None:
            await self._send_error(core, request_id, code, message)

    async def _mark_unknown_and_notify(
        self,
        request_id: str,
        *,
        code: str,
        message: str,
    ) -> None:
        try:
            transition = self._store.mark_unknown(request_id, code=code, message=message)
        except (JobStoreError, OSError, ValueError) as exc:
            await self._handle_store_failure(request_id, exc)
            return
        if not transition.applied:
            return

        async with self._lock:
            core = self._core_clients_by_request.get(request_id)
            self._pending_requests.pop(request_id, None)
            self._clean_routing_locked(request_id)
        if core is not None:
            await self._send_error(core, request_id, code, message)

    async def _handle_store_failure(self, request_id: str, exc: BaseException) -> None:
        LOGGER.error(
            "AI Bridge durable store failed for request %s",
            request_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        async with self._lock:
            core = self._core_clients_by_request.get(request_id)
            self._pending_requests.pop(request_id, None)
            self._clean_routing_locked(request_id)
        if core is not None:
            await self._send_error(
                core,
                request_id,
                "STORE_UNAVAILABLE",
                "AI Bridge could not durably commit the request outcome",
            )

    async def _forward_to_core(self, request_id: str, raw_message: str) -> None:
        async with self._lock:
            core = self._core_clients_by_request.get(request_id)
        if core is not None:
            await self._safe_send(core, raw_message)

    async def maintenance_once(self) -> None:
        try:
            expired = self._store.expire_due()
        except (JobStoreError, OSError, ValueError):
            LOGGER.exception("AI Bridge maintenance could not expire durable jobs")
            return

        notifications: list[tuple[WebSocketServerProtocol, str, str, str]] = []
        async with self._lock:
            for expired_job in expired:
                request_id = expired_job.job.id
                core = self._core_clients_by_request.get(request_id)
                self._pending_requests.pop(request_id, None)
                self._clean_routing_locked(request_id)
                if core is not None:
                    notifications.append(
                        (core, request_id, expired_job.code, expired_job.message)
                    )

        for core, request_id, code, message in notifications:
            await self._send_error(core, request_id, code, message)

    async def maintenance_loop(self) -> None:
        interval = max(0.1, MAINTENANCE_INTERVAL_SECONDS)
        try:
            while True:
                await asyncio.sleep(interval)
                await self.maintenance_once()
        except asyncio.CancelledError:
            raise

    async def _remove_client(
        self,
        client_id: int,
        websocket: WebSocketServerProtocol,
    ) -> None:
        tab_read_failures: list[tuple[WebSocketServerProtocol, str]] = []
        async with self._lock:
            removed_extension = self._extension_clients.pop(client_id, None)
            request_ids = self._request_ids_by_client.pop(client_id, set())
            for request_id in request_ids:
                if self._core_clients_by_request.get(request_id) is websocket:
                    self._core_clients_by_request.pop(request_id, None)
                    self._tab_read_requests.pop(request_id, None)
            if removed_extension is not None:
                for request_id, tab_read in list(self._tab_read_requests.items()):
                    if tab_read.extension_client_id != client_id:
                        continue
                    core = self._core_clients_by_request.get(request_id)
                    self._clean_routing_locked(request_id)
                    if core is not None:
                        tab_read_failures.append((core, request_id))
            self._rebuild_sources_locked()

        for core, request_id in tab_read_failures:
            await self._send_error(
                core,
                request_id,
                "TAB_READ_CONNECTION_LOST",
                "The extension disconnected while reading the open tab",
            )

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "ok": self._ready,
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "sources": list(self._extension_sources),
                "pendingRequests": len(self._pending_requests),
                "extensionClients": len(self._extension_clients),
            }

    async def sources(self) -> list[str]:
        async with self._lock:
            return list(self._extension_sources)

    async def get_last_result(self, source: str) -> JobRecord | None:
        return self._store.latest_completed(source)

    def close(self) -> None:
        self._store.close()

    def _select_extension_locked(self, source: str) -> ExtensionClient | None:
        for client in self._extension_clients.values():
            if client.source == source or source in client.sources:
                return client
        return None

    def _rebuild_sources_locked(self) -> None:
        sources: list[str] = []
        for client in self._extension_clients.values():
            sources.extend(client.sources)
        self._extension_sources = sorted(set(sources))

    def _clean_routing_locked(self, request_id: str) -> None:
        self._core_clients_by_request.pop(request_id, None)
        self._tab_read_requests.pop(request_id, None)
        for request_ids in self._request_ids_by_client.values():
            request_ids.discard(request_id)

    @staticmethod
    def _normalize_source(raw_source: Any) -> str:
        if not isinstance(raw_source, str):
            return ""
        source = raw_source.strip().casefold()
        return source if source in VALID_SOURCES else ""

    @classmethod
    def _normalize_sources(cls, raw_sources: Any, source: str) -> list[str]:
        normalized: set[str] = set()
        if isinstance(raw_sources, list):
            for item in raw_sources:
                candidate = cls._normalize_source(item)
                if candidate:
                    normalized.add(candidate)
        if source in VALID_SOURCES:
            normalized.add(source)
        return sorted(normalized)

    @staticmethod
    async def _safe_send(websocket: WebSocketServerProtocol, raw_message: str) -> bool:
        try:
            await websocket.send(raw_message)
            return True
        except Exception:
            return False

    @classmethod
    async def _safe_send_json(
        cls,
        websocket: WebSocketServerProtocol,
        payload: dict[str, Any],
    ) -> bool:
        return await cls._safe_send(websocket, json.dumps(payload, ensure_ascii=False))

    @classmethod
    async def _send_error(
        cls,
        websocket: WebSocketServerProtocol,
        request_id: str | None,
        code: str,
        message: str,
    ) -> bool:
        payload: dict[str, Any] = {"type": "ERROR", "code": code, "message": message}
        if request_id:
            payload["id"] = request_id
        return await cls._safe_send_json(websocket, payload)


def start_health_server(
    broker: BridgeBroker,
    loop: asyncio.AbstractEventLoop,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if len(self.path.encode("utf-8", errors="replace")) > MAX_HTTP_QUERY_BYTES:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "REQUEST_TARGET_TOO_LONG",
                    "Request target is too long",
                )
                return

            parsed = urlsplit(self.path)
            if parsed.path == "/health" and not parsed.query:
                future = asyncio.run_coroutine_threadsafe(broker.snapshot(), loop)
                try:
                    payload = future.result(timeout=2.0)
                except Exception:
                    payload = {"ok": False, "service": SERVICE_NAME}
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                else:
                    status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
                self._send_json(status, payload, cors=True)
                return

            if parsed.path == "/sources" and not parsed.query:
                future = asyncio.run_coroutine_threadsafe(broker.sources(), loop)
                try:
                    payload = future.result(timeout=2.0)
                except Exception:
                    payload = []
                self._send_json(HTTPStatus.OK, payload, cors=True)
                return

            if parsed.path == "/results/last":
                source = self._parse_last_result_source(parsed.query)
                if source is None:
                    return
                future = asyncio.run_coroutine_threadsafe(broker.get_last_result(source), loop)
                try:
                    result = future.result(timeout=3.0)
                except (FutureTimeoutError, JobStoreError, OSError, ValueError):
                    self._send_error_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "STORE_UNAVAILABLE",
                        "AI Bridge result store is unavailable",
                    )
                    return
                except Exception:
                    LOGGER.exception("Unexpected error while reading the latest AI Bridge result")
                    self._send_error_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "INTERNAL_ERROR",
                        "AI Bridge could not read the latest result",
                    )
                    return

                if result is None:
                    self._send_error_json(
                        HTTPStatus.NOT_FOUND,
                        "NO_COMPLETED_RESULT",
                        f"No completed result is stored for {source}",
                    )
                    return

                self._send_json(
                    HTTPStatus.OK,
                    {
                        "id": result.id,
                        "source": result.source,
                        "state": result.state,
                        "content": result.result_content,
                        "contentBytes": result.result_bytes,
                        "contentSha256": result.result_sha256,
                        "completedAtMs": result.terminal_at_ms,
                    },
                )
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "NOT_FOUND", "Endpoint not found")

        def do_POST(self) -> None:
            self._method_not_allowed()

        def do_PUT(self) -> None:
            self._method_not_allowed()

        def do_DELETE(self) -> None:
            self._method_not_allowed()

        def do_PATCH(self) -> None:
            self._method_not_allowed()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _parse_last_result_source(self, query: str) -> str | None:
            try:
                values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_QUERY",
                    "Malformed result query",
                )
                return None
            if set(values) != {"source"} or len(values["source"]) != 1:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_QUERY",
                    "Exactly one source query parameter is required",
                )
                return None
            source = BridgeBroker._normalize_source(values["source"][0])
            if not source:
                self._send_error_json(
                    HTTPStatus.BAD_REQUEST,
                    "INVALID_SOURCE",
                    "source must be chatgpt, gemini, or claude",
                )
                return None
            return source

        def _method_not_allowed(self) -> None:
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_error_json(self, status: HTTPStatus, code: str, message: str) -> None:
            self._send_json(status, {"error": {"code": code, "message": message}})

        def _send_json(self, status: int, payload: Any, *, cors: bool = False) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(body) > MAX_HTTP_RESPONSE_BYTES:
                status = HTTPStatus.SERVICE_UNAVAILABLE
                body = json.dumps(
                    {
                        "error": {
                            "code": "RESULT_TOO_LARGE_FOR_HTTP",
                            "message": "Stored result exceeds the HTTP response limit",
                        }
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            self.send_response(status)
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def main() -> None:
    broker = BridgeBroker()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def request_stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_args: request_stop())
        except (AttributeError, ValueError):
            pass

    try:
        async with websockets.serve(
            broker.handle_client,
            HOST,
            WS_PORT,
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=20,
            ping_timeout=20,
        ):
            health_server = start_health_server(broker, loop)
            maintenance_task = asyncio.create_task(broker.maintenance_loop())
            await broker.set_ready(True)
            if os.getenv("AIBRIDGE_QUIET") != "1":
                print(
                    f"[aibridge] listening on ws://{HOST}:{WS_PORT} "
                    f"and http://{HOST}:{HTTP_PORT}/health",
                    flush=True,
                )
            try:
                await stop_event.wait()
            finally:
                await broker.set_ready(False)
                maintenance_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintenance_task
                health_server.shutdown()
                health_server.server_close()
    finally:
        broker.close()


if __name__ == "__main__":
    asyncio.run(main())
