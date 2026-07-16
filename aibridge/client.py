from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, TypedDict
from urllib.parse import urlparse

import websockets

LOGGER = logging.getLogger(__name__)


class BridgeRequest(TypedDict, total=False):
    id: str
    type: Literal["COMPLETION_REQUEST"]
    context: str
    stream: bool
    source: str


@dataclass(frozen=True)
class CloudBridgeConfig:
    url: str = "ws://127.0.0.1:8765"
    health_url: str = "http://127.0.0.1:8766"
    extension_wait_timeout_seconds: float = 120.0
    response_timeout_seconds: float | None = None
    max_message_bytes: int = 8 * 1024 * 1024

    @property
    def host(self) -> str:
        parsed = urlparse(self.url)
        return parsed.hostname or "127.0.0.1"

    @property
    def port(self) -> int:
        parsed = urlparse(self.url)
        return parsed.port or 8765


class CloudBridgeError(RuntimeError):
    pass


class CloudBridge:
    def __init__(self, config: CloudBridgeConfig | None = None) -> None:
        self._config = config or CloudBridgeConfig()

    async def start(self) -> None:
        LOGGER.info("Cloud bridge client ready for %s", self._config.url)

    async def stop(self) -> None:
        return

    async def get_active_sources(self) -> list[str]:
        import urllib.request
        
        def fetch() -> list[str]:
            url = f"{self._config.health_url.rstrip('/')}/sources"
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if response.status == 200:
                        data = response.read().decode("utf-8")
                        sources = json.loads(data)
                        if isinstance(sources, list):
                            return [str(s) for s in sources]
            except Exception:
                pass
            return []
            
        return await asyncio.to_thread(fetch)

    async def stream_completion(self, context: str, source: str | None = None) -> AsyncIterator[str]:
        request_id = uuid.uuid4().hex
        request: BridgeRequest = {
            "id": request_id,
            "type": "COMPLETION_REQUEST",
            "context": context,
            "stream": True,
        }
        if source:
            request["source"] = source

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
            saw_final = False
            accumulated_text = ""

            while True:
                try:
                    if self._config.response_timeout_seconds is None:
                        raw_message = await websocket.recv()
                    else:
                        async with asyncio.timeout(self._config.response_timeout_seconds):
                            raw_message = await websocket.recv()
                except TimeoutError as exc:
                    raise CloudBridgeError("Cloud bridge response timeout") from exc
                except websockets.ConnectionClosed as exc:
                    raise CloudBridgeError("Cloud bridge connection closed") from exc

                event = self._parse_message(raw_message)
                if event is None or event.get("id") != request_id:
                    continue

                event_type = event.get("type")
                if event_type in {"STATUS", "BRIDGE_READY", "BRIDGE_HEARTBEAT"}:
                    continue
                if event_type in {"TOKEN", "COMPLETION_TOKEN"}:
                    token = event.get("token")
                    if isinstance(token, str) and token:
                        is_snapshot = False
                        clean_acc = accumulated_text.lstrip()
                        clean_tok = token.lstrip()

                        if len(clean_acc) > 0:
                            if clean_tok.startswith(clean_acc):
                                is_snapshot = True
                            elif len(clean_acc) >= 5 and len(clean_tok) >= 5:
                                if clean_tok[:5] == clean_acc[:5] and len(clean_tok) >= len(clean_acc) * 0.7:
                                    is_snapshot = True

                        if is_snapshot:
                            accumulated_text = token
                            yield "\0CLEAR\0" + token
                        else:
                            accumulated_text += token
                            yield token
                    continue
                if event_type in {"REPLACE", "COMPLETION_REPLACE"}:
                    content = event.get("content")
                    if isinstance(content, str):
                        LOGGER.info("Cloud request %s replaced full text", request_id)
                        accumulated_text = content
                        yield "\0CLEAR\0" + content
                    continue
                if event_type in {"FINAL", "COMPLETION_FINAL"}:
                    content = event.get("content")
                    if isinstance(content, str):
                        saw_final = True
                        LOGGER.info("Cloud request %s received final full text", request_id)
                        yield "\0CLEAR\0" + content
                    LOGGER.info("Cloud request %s completed", request_id)
                    break
                if event_type in {"DONE", "COMPLETION_DONE"}:
                    if source == "claude" and not saw_final:
                        LOGGER.warning("Cloud request %s received DONE before FINAL from Claude", request_id)
                    LOGGER.info("Cloud request %s completed", request_id)
                    break
                if event_type in {"ERROR", "COMPLETION_ERROR"}:
                    message = event.get("message")
                    raise CloudBridgeError(str(message or "Bridge returned an error"))
        finally:
            await websocket.close()

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

    @staticmethod
    def _parse_message(raw_message: str | bytes) -> dict[str, object] | None:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8", errors="replace")

        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, dict) else None
