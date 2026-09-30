from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, TypedDict
from urllib.parse import urlparse

import websockets

LOGGER = logging.getLogger(__name__)
DEFAULT_RESPONSE_TIMEOUT_SECONDS = float(
    os.getenv("AIBRIDGE_RESPONSE_TIMEOUT_SECONDS", str(6 * 60 * 60 + 60))
)
VALID_SOURCES = frozenset({"chatgpt", "gemini", "claude"})
CLEAR_MARKER = "\0CLEAR\0"


class BridgeRequest(TypedDict, total=False):
    id: str
    type: Literal["COMPLETION_REQUEST"]
    context: str
    stream: bool
    source: str
    readCurrentTab: bool


@dataclass(frozen=True)
class CloudBridgeConfig:
    url: str = "ws://127.0.0.1:8765"
    health_url: str = "http://127.0.0.1:8766"
    extension_wait_timeout_seconds: float = 120.0
    response_timeout_seconds: float | None = DEFAULT_RESPONSE_TIMEOUT_SECONDS
    max_message_bytes: int = 8 * 1024 * 1024

    @property
    def host(self) -> str:
        parsed = urlparse(self.url)
        return parsed.hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        parsed = urlparse(self.url)
        return parsed.port or 8765


@dataclass(frozen=True)
class StoredResult:
    id: str
    source: str
    content: str
    content_bytes: int
    content_sha256: str
    completed_at_ms: int


class CloudBridgeError(RuntimeError):
    pass


class NoStoredResultError(CloudBridgeError):
    pass


class CloudBridge:
    def __init__(self, config: CloudBridgeConfig | None = None) -> None:
        self._config = config or CloudBridgeConfig()

    async def start(self) -> None:
        LOGGER.info("Cloud bridge client ready for %s", self._config.url)

    async def stop(self) -> None:
        return

    async def get_active_sources(self) -> list[str]:
        def fetch() -> list[str]:
            url = f"{self._config.health_url.rstrip('/')}/sources"
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if response.status != 200:
                        return []
                    payload = self._read_json_response(response)
                    if isinstance(payload, list):
                        return [str(source) for source in payload]
            except Exception:
                pass
            return []

        return await asyncio.to_thread(fetch)

    async def get_last_result(self, source: str) -> StoredResult:
        normalized_source = source.strip().casefold() if isinstance(source, str) else ""
        if normalized_source not in VALID_SOURCES:
            raise CloudBridgeError(f"Unsupported AI source: {source}")

        def fetch() -> StoredResult:
            query = urllib.parse.urlencode({"source": normalized_source})
            url = f"{self._config.health_url.rstrip('/')}/results/last?{query}"
            try:
                with urllib.request.urlopen(url, timeout=3.0) as response:
                    payload = self._read_json_response(response)
            except urllib.error.HTTPError as exc:
                payload = self._read_http_error(exc)
                code = self._error_code(payload)
                if exc.code == 404 and code == "NO_COMPLETED_RESULT":
                    raise NoStoredResultError(
                        f"No completed result is stored for {normalized_source}"
                    ) from exc
                raise CloudBridgeError(
                    f"AI Bridge get-last failed with HTTP {exc.code}"
                    + (f" ({code})" if code else "")
                ) from exc
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise CloudBridgeError(f"AI Bridge get-last failed: {exc}") from exc

            return self._validate_stored_result(payload, normalized_source)

        return await asyncio.to_thread(fetch)

    async def stream_completion(
        self,
        context: str,
        source: str | None = None,
    ) -> AsyncIterator[str]:
        async for chunk in self._stream_request(context, source, read_current_tab=False):
            yield chunk

    async def stream_current_tab(self, source: str) -> AsyncIterator[str]:
        normalized_source = source.strip().casefold() if isinstance(source, str) else ""
        if normalized_source not in VALID_SOURCES:
            raise CloudBridgeError(f"Unsupported AI source: {source}")

        async for chunk in self._stream_request(
            "",
            normalized_source,
            read_current_tab=True,
        ):
            yield chunk

    async def _stream_request(
        self,
        context: str,
        source: str | None,
        *,
        read_current_tab: bool,
    ) -> AsyncIterator[str]:
        request_id = uuid.uuid4().hex
        request: BridgeRequest = {
            "id": request_id,
            "type": "COMPLETION_REQUEST",
            "context": context,
            "stream": True,
        }
        if source:
            request["source"] = source
        if read_current_tab:
            request["readCurrentTab"] = True

        try:
            websocket = await self._connect()
        except Exception as exc:
            raise CloudBridgeError(
                "AI Bridge is not available at "
                f"ws://{self._config.host}:{self._config.port}"
            ) from exc

        try:
            LOGGER.info("Sending browser request %s to AI Bridge", request_id)
            await websocket.send(json.dumps(request, ensure_ascii=False))
            timeout = self._config.response_timeout_seconds
            try:
                if timeout is None:
                    async for chunk in self._receive_completion(websocket, request_id):
                        yield chunk
                else:
                    async with asyncio.timeout(timeout):
                        async for chunk in self._receive_completion(websocket, request_id):
                            yield chunk
            except TimeoutError as exc:
                raise CloudBridgeError(
                    f"Cloud bridge response deadline exceeded after {timeout:g} seconds"
                ) from exc
        finally:
            await websocket.close()

    async def _receive_completion(
        self,
        websocket: websockets.WebSocketClientProtocol,
        request_id: str,
    ) -> AsyncIterator[str]:
        accumulated_text = ""
        while True:
            try:
                raw_message = await websocket.recv()
            except websockets.ConnectionClosed as exc:
                raise CloudBridgeError("Cloud bridge connection closed before FINAL") from exc

            event = self._parse_message(raw_message)
            if event is None or event.get("id") != request_id:
                continue

            event_type = event.get("type")
            if event_type in {"STATUS", "BRIDGE_READY", "BRIDGE_HEARTBEAT"}:
                continue
            if event_type in {"TOKEN", "COMPLETION_TOKEN"}:
                token = event.get("token")
                if not isinstance(token, str) or not token:
                    raise CloudBridgeError("Bridge returned an invalid TOKEN event")

                clean_accumulated = accumulated_text.lstrip()
                clean_token = token.lstrip()
                is_snapshot = False
                if clean_accumulated:
                    if clean_token.startswith(clean_accumulated):
                        is_snapshot = True
                    elif len(clean_accumulated) >= 5 and len(clean_token) >= 5:
                        is_snapshot = (
                            clean_token[:5] == clean_accumulated[:5]
                            and len(clean_token) >= len(clean_accumulated) * 0.7
                        )

                if is_snapshot:
                    accumulated_text = token
                    yield CLEAR_MARKER + token
                else:
                    accumulated_text += token
                    yield token
                continue
            if event_type in {"REPLACE", "COMPLETION_REPLACE"}:
                content = event.get("content")
                if not isinstance(content, str):
                    raise CloudBridgeError("Bridge returned an invalid REPLACE event")
                accumulated_text = content
                yield CLEAR_MARKER + content
                continue
            if event_type in {"FINAL", "COMPLETION_FINAL"}:
                content = event.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise CloudBridgeError("Bridge returned FINAL without a non-empty full result")
                LOGGER.info("Cloud request %s received durable final text", request_id)
                yield CLEAR_MARKER + content
                return
            if event_type in {"DONE", "COMPLETION_DONE"}:
                raise CloudBridgeError("Bridge returned DONE before a valid FINAL result")
            if event_type in {"ERROR", "COMPLETION_ERROR"}:
                message = event.get("message")
                code = event.get("code")
                detail = str(message or "Bridge returned an error")
                if isinstance(code, str) and code:
                    detail = f"{detail} [{code}]"
                raise CloudBridgeError(detail)

    async def _connect(self) -> websockets.WebSocketClientProtocol:
        deadline = asyncio.get_running_loop().time() + self._config.extension_wait_timeout_seconds
        last_error: Exception | None = None

        while True:
            try:
                return await websockets.connect(
                    self._config.url,
                    max_size=self._config.max_message_bytes,
                    ping_interval=20,
                    ping_timeout=20,
                )
            except Exception as exc:
                last_error = exc

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise CloudBridgeError(
                    "AI Bridge is not available at "
                    f"ws://{self._config.host}:{self._config.port}: {last_error}"
                )
            await asyncio.sleep(min(1.0, remaining))

    def _read_json_response(self, response: object) -> object:
        max_bytes = self._config.max_message_bytes + 64 * 1024
        headers = getattr(response, "headers", None)
        content_length = headers.get("Content-Length") if headers is not None else None
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise CloudBridgeError("AI Bridge returned an invalid Content-Length") from exc
            if declared_size < 0 or declared_size > max_bytes:
                raise CloudBridgeError("AI Bridge result response is too large")

        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CloudBridgeError("AI Bridge result response is too large")
        return json.loads(data.decode("utf-8", errors="strict"))

    def _read_http_error(self, error: urllib.error.HTTPError) -> object:
        try:
            return self._read_json_response(error)
        except Exception:
            return None

    @staticmethod
    def _error_code(payload: object) -> str:
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if not isinstance(error, dict):
            return ""
        code = error.get("code")
        return code if isinstance(code, str) else ""

    @staticmethod
    def _validate_stored_result(payload: object, requested_source: str) -> StoredResult:
        if not isinstance(payload, dict):
            raise CloudBridgeError("AI Bridge get-last returned a non-object JSON response")

        result_id = payload.get("id")
        source = payload.get("source")
        state = payload.get("state")
        content = payload.get("content")
        content_bytes = payload.get("contentBytes")
        content_sha256 = payload.get("contentSha256")
        completed_at_ms = payload.get("completedAtMs")

        if not isinstance(result_id, str) or not result_id:
            raise CloudBridgeError("Stored result has an invalid id")
        if source != requested_source:
            raise CloudBridgeError("Stored result source does not match the requested source")
        if state != "COMPLETED":
            raise CloudBridgeError("Stored result is not completed")
        if not isinstance(content, str) or not content.strip():
            raise CloudBridgeError("Stored result content is empty")
        if type(content_bytes) is not int or content_bytes <= 0:
            raise CloudBridgeError("Stored result has an invalid byte length")
        if not isinstance(content_sha256, str) or len(content_sha256) != 64:
            raise CloudBridgeError("Stored result has an invalid SHA-256 digest")
        if type(completed_at_ms) is not int or completed_at_ms <= 0:
            raise CloudBridgeError("Stored result has an invalid completion timestamp")

        encoded = content.encode("utf-8", errors="strict")
        if len(encoded) != content_bytes:
            raise CloudBridgeError("Stored result byte length verification failed")
        if hashlib.sha256(encoded).hexdigest() != content_sha256:
            raise CloudBridgeError("Stored result digest verification failed")

        return StoredResult(
            id=result_id,
            source=source,
            content=content,
            content_bytes=content_bytes,
            content_sha256=content_sha256,
            completed_at_ms=completed_at_ms,
        )

    @staticmethod
    def _parse_message(raw_message: str | bytes) -> dict[str, object] | None:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8", errors="replace")

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, dict) else None
