from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import websockets
from websockets.server import WebSocketServerProtocol

HOST = os.getenv("AIBRIDGE_HOST", "127.0.0.1")
WS_PORT = int(os.getenv("AIBRIDGE_WS_PORT", "8765"))
HTTP_PORT = int(os.getenv("AIBRIDGE_HTTP_PORT", "8766"))
MAX_MESSAGE_BYTES = int(os.getenv("AIBRIDGE_MAX_MESSAGE_BYTES", str(8 * 1024 * 1024)))
MAX_PENDING_REQUESTS = int(os.getenv("AIBRIDGE_MAX_PENDING_REQUESTS", "100"))
PENDING_TTL_SECONDS = float(os.getenv("AIBRIDGE_PENDING_TTL_SECONDS", "300"))
SERVICE_NAME = "aibridge"
SERVICE_VERSION = "0.1.0"

READY_TYPES = {"BRIDGE_READY", "BRIDGE_HEARTBEAT"}
TERMINAL_TYPES = {
    "FINAL",
    "COMPLETION_FINAL",
    "DONE",
    "COMPLETION_DONE",
    "ERROR",
    "COMPLETION_ERROR",
}
RESPONSE_TYPES = {
    "STATUS",
    "TOKEN",
    "COMPLETION_TOKEN",
    "REPLACE",
    "COMPLETION_REPLACE",
    *TERMINAL_TYPES,
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
    expires_at: float


class BridgeBroker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_client_id = 0
        self._extension_clients: dict[int, ExtensionClient] = {}
        self._core_clients_by_request: dict[str, WebSocketServerProtocol] = {}
        self._pending_requests: dict[str, PendingRequest] = {}
        self._request_ids_by_client: dict[int, set[str]] = {}
        self._extension_sources: list[str] = []
        self._ready = False

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
        if not isinstance(request_id, str) or not request_id:
            await self._safe_send_json(
                websocket,
                {
                    "type": "ERROR",
                    "message": "COMPLETION_REQUEST missing id",
                },
            )
            return

        now = time.monotonic()
        async with self._lock:
            self._expire_pending_locked(now)
            self._core_clients_by_request[request_id] = websocket
            self._request_ids_by_client.setdefault(client_id, set()).add(request_id)
            extension = self._select_extension_locked(payload.get("source"))

            if extension is None:
                if len(self._pending_requests) >= MAX_PENDING_REQUESTS:
                    await self._safe_send_json(
                        websocket,
                        {
                            "type": "ERROR",
                            "id": request_id,
                            "message": "AI Bridge pending request queue is full",
                        },
                    )
                    self._core_clients_by_request.pop(request_id, None)
                    self._request_ids_by_client.get(client_id, set()).discard(request_id)
                    return

                self._pending_requests[request_id] = PendingRequest(
                    raw=raw_message,
                    expires_at=now + PENDING_TTL_SECONDS,
                )
                return

        if not await self._safe_send(extension.websocket, raw_message):
            async with self._lock:
                self._pending_requests[request_id] = PendingRequest(
                    raw=raw_message,
                    expires_at=time.monotonic() + PENDING_TTL_SECONDS,
                )

    async def _handle_extension_ready(
        self,
        client_id: int,
        websocket: WebSocketServerProtocol,
        payload: dict[str, Any],
    ) -> None:
        source = str(payload.get("source") or "none")
        sources = self._normalize_sources(payload.get("sources"), source)

        async with self._lock:
            self._extension_clients[client_id] = ExtensionClient(
                id=client_id,
                websocket=websocket,
                source=source,
                sources=sources,
            )
            self._rebuild_sources_locked()
            pending_items = list(self._pending_requests.items())

        for request_id, pending in pending_items:
            if pending.expires_at < time.monotonic():
                async with self._lock:
                    self._pending_requests.pop(request_id, None)
                continue

            if await self._safe_send(websocket, pending.raw):
                async with self._lock:
                    self._pending_requests.pop(request_id, None)

    async def _handle_completion_response(self, payload: dict[str, Any], raw_message: str) -> None:
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            return

        async with self._lock:
            core_websocket = self._core_clients_by_request.get(request_id)
            if payload.get("type") in TERMINAL_TYPES:
                self._core_clients_by_request.pop(request_id, None)
                self._pending_requests.pop(request_id, None)
                for request_ids in self._request_ids_by_client.values():
                    request_ids.discard(request_id)

        if core_websocket is not None:
            await self._safe_send(core_websocket, raw_message)

    async def _remove_client(self, client_id: int, websocket: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._extension_clients.pop(client_id, None)
            request_ids = self._request_ids_by_client.pop(client_id, set())
            for request_id in request_ids:
                if self._core_clients_by_request.get(request_id) is websocket:
                    self._core_clients_by_request.pop(request_id, None)
                    self._pending_requests.pop(request_id, None)

            self._rebuild_sources_locked()

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            self._expire_pending_locked(time.monotonic())
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

    def _select_extension_locked(self, source: Any) -> ExtensionClient | None:
        if not self._extension_clients:
            return None

        if isinstance(source, str) and source.strip():
            wanted = source.strip().casefold()
            for client in self._extension_clients.values():
                client_sources = [item.casefold() for item in client.sources]
                if client.source.casefold() == wanted or wanted in client_sources:
                    return client

        return next(iter(self._extension_clients.values()))

    def _rebuild_sources_locked(self) -> None:
        sources: list[str] = []
        for client in self._extension_clients.values():
            sources.extend(client.sources)
        self._extension_sources = sorted({source for source in sources if source and source != "none"})

    def _expire_pending_locked(self, now: float) -> None:
        expired_ids = [
            request_id
            for request_id, pending in self._pending_requests.items()
            if pending.expires_at < now
        ]
        for request_id in expired_ids:
            self._pending_requests.pop(request_id, None)
            self._core_clients_by_request.pop(request_id, None)
            for request_ids in self._request_ids_by_client.values():
                request_ids.discard(request_id)

    @staticmethod
    def _normalize_sources(raw_sources: Any, source: str) -> list[str]:
        if isinstance(raw_sources, list):
            return [
                str(item)
                for item in raw_sources
                if str(item) and str(item) != "none"
            ]
        if source and source != "none":
            return [source]
        return []

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


def start_health_server(
    broker: BridgeBroker,
    loop: asyncio.AbstractEventLoop,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/health"):
                future = asyncio.run_coroutine_threadsafe(broker.snapshot(), loop)
                try:
                    payload = future.result(timeout=2.0)
                except Exception:
                    payload = {"ok": False, "service": SERVICE_NAME}
                    status = 503
                else:
                    status = 200 if payload.get("ok") else 503

                self._send_json(status, payload)
                return

            if self.path.startswith("/sources"):
                future = asyncio.run_coroutine_threadsafe(broker.sources(), loop)
                try:
                    payload = future.result(timeout=2.0)
                except Exception:
                    payload = []
                self._send_json(200, payload)
                return

            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
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

    async with websockets.serve(
        broker.handle_client,
        HOST,
        WS_PORT,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ):
        health_server = start_health_server(broker, loop)
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
            health_server.shutdown()
            health_server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
