# AI Bridge v2: первый совместимый срез — durable jobs и корректный `get-last`

## Суть задачи

AI Bridge — локальная Windows-программа, связывающая CLI `ai` с уже открытыми и авторизованными вкладками ChatGPT, Gemini и Claude через Chrome MV3 extension. Официальные API провайдеров полностью исключены как продуктовый вариант. На этом этапе нельзя менять концепцию на API и нельзя требовать отдельный автоматизированный профиль браузера.

Нужно начать глубокую переделку с небольшого, но production-ready вертикального среза: сделать исход задания и финальный ответ долговечными, перестать считать `DONE` успешным результатом без `FINAL`, а `ai --get-last --<source>` читать из локального хранилища, а не отправлять пустой prompt в текущую вкладку.

Это именно первая миграционная фаза. Native Messaging, точная привязка вкладок и разделение DOM-адаптеров будут последующими фазами. Текущий WebSocket transport и текущий extension должны остаться совместимыми на этом шаге.

## Что пользователь хочет получить сейчас

Конкретный безопасный план и рекомендации по реализации первого среза, пригодные для немедленного внесения в приведённый Python-код:

1. SQLite-хранилище заданий и событий без новых внешних Python-зависимостей (stdlib `sqlite3`).
2. Состояния минимум `ACCEPTED`, `QUEUED`, `DISPATCHED`, `STREAMING`, `COMPLETED`, `FAILED`, `UNKNOWN_OUTCOME`.
3. Только `FINAL` с валидным полным содержимым означает `COMPLETED`; `ERROR` означает `FAILED`; `DONE` — совместимый non-terminal маркер и не может давать CLI ложный успех.
4. Один terminal outcome на job; поздние/дублированные terminal events не должны менять уже зафиксированный исход.
5. После рестарта незавершённые задания должны становиться `UNKNOWN_OUTCOME`, чтобы их нельзя было молча повторно отправить.
6. Финальный результат хранится по `source`; `get-last` возвращает последний успешно завершённый непустой результат выбранной модели независимо от состояния вкладки.
7. Брокер по-прежнему слушает только loopback. HTTP endpoint для чтения последнего результата допустим, но нужен строгий разбор пути/query и понятные 404/400/503 ответы.
8. Сохранить текущие CLI-команды и extension wire format, насколько возможно.
9. Исправить существующий случай: pending request TTL сейчас удаляется молча, после чего CLI может ждать бесконечно.
10. Добавить пропорциональные unit/integration tests.

## Текущее поведение и подтверждённые проблемы

- Вся маршрутизация job хранится только в словарях `BridgeBroker`; рестарт теряет исход и результат.
- `TERMINAL_TYPES` включает и `FINAL`, и `DONE`; первый из них удаляет routing entry. Если пришёл `DONE` без `FINAL`, CLI считает job успешным даже без полного результата.
- `content.js` обычно отправляет `FINAL`, затем `DONE`; для получения последнего текста пустой `context` также заставляет extension скрейпить текущий DOM и отправлять `FINAL`/`DONE`.
- `ai --get-last` сейчас реализован именно через пустой browser request, поэтому зависит от активной/первой найденной вкладки и может вернуть не тот разговор.
- pending TTL просто удаляет записи из памяти и не сообщает ошибку ожидающему CLI.
- В логах наблюдались разрывы WebSocket `ConnectionClosedError: no close frame received or sent` (WinError 64), поэтому память процесса и живое соединение не могут быть источником истины.
- Семь текущих Python-тестов проходят; browser E2E отсутствует.
- В рабочем дереве уже есть пользовательские изменения в README и extension (в основном Gemini Deep Research). Первый срез не должен их перетирать.

## Структура релевантных файлов

```text
aibridge/
  broker.py       # loopback WebSocket broker + HTTP health server
  client.py       # WebSocket CLI client, parses streaming events
  command.py      # CLI semantics, including --get-last
  lifecycle.py    # starts/reuses broker process
tests/
  test_broker.py
  test_lifecycle.py
  test_task_runner.py
  mock_extension.py
extension/src/
  background.js   # transport and tab dispatch
  content.js      # emits STATUS/TOKEN/REPLACE/FINAL/DONE/ERROR
requirements.txt  # websockets>=12.0,<14
```

## Текущий `aibridge/broker.py` (полностью)

```python
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
    "FINAL", "COMPLETION_FINAL", "DONE", "COMPLETION_DONE",
    "ERROR", "COMPLETION_ERROR",
}
RESPONSE_TYPES = {
    "STATUS", "TOKEN", "COMPLETION_TOKEN", "REPLACE",
    "COMPLETION_REPLACE", *TERMINAL_TYPES,
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

    async def _handle_raw_message(self, client_id, websocket, raw_message) -> None:
        if isinstance(raw_message, bytes):
            try:
                raw_message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                return
        if len(raw_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            await self._safe_send_json(websocket, {"type": "ERROR", "message": f"Message exceeds {MAX_MESSAGE_BYTES} bytes"})
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

    async def _handle_completion_request(self, client_id, websocket, payload, raw_message) -> None:
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            await self._safe_send_json(websocket, {"type": "ERROR", "message": "COMPLETION_REQUEST missing id"})
            return
        now = time.monotonic()
        async with self._lock:
            self._expire_pending_locked(now)
            self._core_clients_by_request[request_id] = websocket
            self._request_ids_by_client.setdefault(client_id, set()).add(request_id)
            extension = self._select_extension_locked(payload.get("source"))
            if extension is None:
                if len(self._pending_requests) >= MAX_PENDING_REQUESTS:
                    await self._safe_send_json(websocket, {"type": "ERROR", "id": request_id, "message": "AI Bridge pending request queue is full"})
                    self._core_clients_by_request.pop(request_id, None)
                    self._request_ids_by_client.get(client_id, set()).discard(request_id)
                    return
                self._pending_requests[request_id] = PendingRequest(raw=raw_message, expires_at=now + PENDING_TTL_SECONDS)
                return
        if not await self._safe_send(extension.websocket, raw_message):
            async with self._lock:
                self._pending_requests[request_id] = PendingRequest(raw=raw_message, expires_at=time.monotonic() + PENDING_TTL_SECONDS)

    async def _handle_extension_ready(self, client_id, websocket, payload) -> None:
        source = str(payload.get("source") or "none")
        sources = self._normalize_sources(payload.get("sources"), source)
        async with self._lock:
            self._extension_clients[client_id] = ExtensionClient(id=client_id, websocket=websocket, source=source, sources=sources)
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

    async def _handle_completion_response(self, payload, raw_message) -> None:
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

    async def _remove_client(self, client_id, websocket) -> None:
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
            return {"ok": self._ready, "service": SERVICE_NAME, "version": SERVICE_VERSION, "sources": list(self._extension_sources), "pendingRequests": len(self._pending_requests), "extensionClients": len(self._extension_clients)}

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
        expired_ids = [request_id for request_id, pending in self._pending_requests.items() if pending.expires_at < now]
        for request_id in expired_ids:
            self._pending_requests.pop(request_id, None)
            self._core_clients_by_request.pop(request_id, None)
            for request_ids in self._request_ids_by_client.values():
                request_ids.discard(request_id)

    @staticmethod
    def _normalize_sources(raw_sources: Any, source: str) -> list[str]:
        if isinstance(raw_sources, list):
            return [str(item) for item in raw_sources if str(item) and str(item) != "none"]
        if source and source != "none":
            return [source]
        return []

    @staticmethod
    async def _safe_send(websocket, raw_message: str) -> bool:
        try:
            await websocket.send(raw_message)
            return True
        except Exception:
            return False

    @classmethod
    async def _safe_send_json(cls, websocket, payload) -> bool:
        return await cls._safe_send(websocket, json.dumps(payload, ensure_ascii=False))
```

Оставшаяся часть `broker.py` создаёт `ThreadingHTTPServer`: `/health` вызывает `broker.snapshot()` через `asyncio.run_coroutine_threadsafe`, `/sources` вызывает `broker.sources()`, остальные пути дают 404. `main()` создаёт `BridgeBroker`, запускает `websockets.serve`, затем health server. Это важно для thread-safety: доступ к SQLite разумно держать в broker event-loop, а HTTP handler должен обращаться к coroutine через тот же loop.

## Текущий ключевой код `aibridge/client.py`

```python
@dataclass(frozen=True)
class CloudBridgeConfig:
    url: str = "ws://127.0.0.1:8765"
    health_url: str = "http://127.0.0.1:8766"
    extension_wait_timeout_seconds: float = 120.0
    response_timeout_seconds: float | None = None
    max_message_bytes: int = 8 * 1024 * 1024

class CloudBridgeError(RuntimeError):
    pass

class CloudBridge:
    async def get_active_sources(self) -> list[str]:
        import urllib.request
        def fetch() -> list[str]:
            url = f"{self._config.health_url.rstrip('/')}/sources"
            try:
                with urllib.request.urlopen(url, timeout=2.0) as response:
                    if response.status == 200:
                        sources = json.loads(response.read().decode("utf-8"))
                        if isinstance(sources, list):
                            return [str(s) for s in sources]
            except Exception:
                pass
            return []
        return await asyncio.to_thread(fetch)

    async def stream_completion(self, context: str, source: str | None = None) -> AsyncIterator[str]:
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "type": "COMPLETION_REQUEST", "context": context, "stream": True}
        if source:
            request["source"] = source
        websocket = await self._connect()
        try:
            await websocket.send(json.dumps(request, ensure_ascii=False))
            saw_final = False
            accumulated_text = ""
            while True:
                raw_message = await websocket.recv()  # optional configured timeout omitted here
                event = self._parse_message(raw_message)
                if event is None or event.get("id") != request_id:
                    continue
                event_type = event.get("type")
                if event_type in {"STATUS", "BRIDGE_READY", "BRIDGE_HEARTBEAT"}:
                    continue
                if event_type in {"TOKEN", "COMPLETION_TOKEN"}:
                    token = event.get("token")
                    # Current code heuristically guesses delta vs snapshot and yields delta or CLEAR+snapshot.
                    # Details are unchanged for this phase.
                    continue
                if event_type in {"REPLACE", "COMPLETION_REPLACE"}:
                    content = event.get("content")
                    if isinstance(content, str):
                        accumulated_text = content
                        yield "\0CLEAR\0" + content
                    continue
                if event_type in {"FINAL", "COMPLETION_FINAL"}:
                    content = event.get("content")
                    if isinstance(content, str):
                        saw_final = True
                        yield "\0CLEAR\0" + content
                    break
                if event_type in {"DONE", "COMPLETION_DONE"}:
                    if source == "claude" and not saw_final:
                        LOGGER.warning("DONE before FINAL")
                    break  # BUG: false success without final
                if event_type in {"ERROR", "COMPLETION_ERROR"}:
                    raise CloudBridgeError(str(event.get("message") or "Bridge returned an error"))
        finally:
            await websocket.close()
```

В полном файле `_connect()` повторяет соединение до `extension_wait_timeout_seconds`, а `_parse_message()` безопасно разбирает JSON object.

## Текущая семантика `aibridge/command.py`

```python
def validate_get_last_args(args):
    if not args.get_last:
        return None
    if args.file or args.prompt:
        return 1
    if len(target_sources_from_args(args)) != 1:
        return 1
    return None

async def main():
    args = build_parser().parse_args()
    # validation omitted
    if args.get_last:
        prompt = ""
    elif args.file:
        prompt = open(args.file, encoding="utf-8").read()
    else:
        prompt = " ".join(args.prompt)

    bridge = CloudBridge(CloudBridgeConfig(url=args.url, health_url=args.health_url))
    target_sources = target_sources_from_args(args)
    if not target_sources:
        target_sources = await wait_for_active_sources(bridge)

    async def collect_source(source, capture_text):
        text = ""
        saw_text = False
        try:
            async for token in bridge.stream_completion(prompt, source=source):
                chunk = token_text(token)
                if chunk.strip():
                    saw_text = True
                if not capture_text:
                    continue
                text = chunk if token.startswith("\0CLEAR\0") else text + chunk
            cleaned = clean_source_text(text, source) if capture_text else ""
            if not saw_text or (capture_text and not cleaned.strip()):
                raise RuntimeError("ответ не получен")
            return source, cleaned, None
        except Exception as e:
            return source, None, e

    if len(target_sources) == 1:
        source, text, err = await collect_source(target_sources[0], args.get_last)
        if err:
            sys.exit(1)
        elif args.get_last:
            print(text)
        else:
            print_completion_summary([source])
    else:
        # Sends all in parallel, hides full outputs, reports which sources returned non-empty text.
        ...
```

`aibridge.cli.run()` сначала вызывает `ensure_bridge()`, поэтому `get-last` может читать результат через loopback HTTP даже если extension сейчас не подключён. `requirements.txt` содержит только `websockets>=12.0,<14`; Python 3.11+.

## Текущий extension wire behavior

```javascript
// content.js, simplified exact order
if (!context || context.trim() === "") {
  const latestText = getLatestAssistantText(source);
  emit({ type: "FINAL", id, source, content: latestText });
  emit({ type: "DONE", id, source });
  return;
}

await streamResponse(...); // on success emits FINAL with full content
emit({ type: "DONE", id, source });

// During stream: STATUS, TOKEN deltas, REPLACE snapshots.
// On exception: ERROR.
```

`background.js` принимает `COMPLETION_REQUEST`, находит активную подходящую вкладку или первую подходящую фоновую вкладку, посылает туда `{type: AIBRIDGE_RUN_PROMPT, id, context}`, а события content script возвращает брокеру. Изменять этот код в первой фазе желательно только если без этого невозможно обеспечить протокольную корректность.

## Текущие тесты

```python
class FakeWebSocket:
    def __init__(self): self.messages = []
    async def send(self, message): self.messages.append(message)

class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_request_and_response_by_id(self):
        broker = BridgeBroker()
        core = FakeWebSocket()
        extension = FakeWebSocket()
        broker._extension_clients[1] = ExtensionClient(id=1, websocket=extension, source="chatgpt", sources=["chatgpt", "gemini"])
        broker._request_ids_by_client[7] = set()
        request = {"id": "request-1", "type": "COMPLETION_REQUEST", "context": "hello", "source": "gemini"}
        request_raw = json.dumps(request)
        await broker._handle_completion_request(7, core, request, request_raw)
        self.assertEqual(extension.messages, [request_raw])
        response = {"id": "request-1", "type": "FINAL", "content": "world"}
        response_raw = json.dumps(response)
        await broker._handle_completion_response(response, response_raw)
        self.assertEqual(core.messages, [response_raw])
        self.assertNotIn("request-1", broker._core_clients_by_request)

    async def test_snapshot_uses_standalone_service_name(self):
        broker = BridgeBroker()
        await broker.set_ready(True)
        snapshot = await broker.snapshot()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["service"], "aibridge")
```

Другие тесты проверяют только lifecycle startup lock и task runner. Mock extension на каждый request сразу отправляет `FINAL` с `AIBRIDGE_E2E_OK`.

## Предварительные гипотезы локального агента (не доказанные факты)

1. Отдельный `aibridge/job_store.py` с одной `sqlite3.Connection` на broker event-loop, WAL, foreign keys и busy timeout будет достаточно простым и надёжным для первого шага; HTTP thread не должен обращаться к connection напрямую.
2. Две таблицы (`jobs` для materialized current state/result и append-only `job_events` для аудита) предпочтительнее хранения только последнего результата.
3. `FINAL` с пустым/нестроковым `content` лучше превращать в `FAILED` с protocol error, а не `COMPLETED`.
4. `DONE` до `FINAL` лучше немедленно превращать в явную ошибку/failed outcome, чем оставлять job бесконечно активным. Но это спорно: возможен поздний `FINAL`, поэтому требуется критика и выбор корректного compatibility behavior.
5. При duplicate request id нельзя повторно dispatch: completed job можно вернуть из store, активный/unknown job должен получить детерминированную ошибку.
6. Синхронные короткие SQLite-транзакции в event-loop допустимы при таком объёме, но это нужно проверить; возможно, нужен другой ownership/locking pattern.

Проверьте эти гипотезы по контексту. Не считайте их выбранной архитектурой.

## Конкретные вопросы

1. Какую минимальную схему SQLite и API `JobStore` вы рекомендуете? Укажите constraints, indexes, транзакционные границы и migration/version strategy.
2. Как точно сопоставить текущие wire events со state machine, включая `STATUS`, `TOKEN`, `REPLACE`, `FINAL`, `DONE`, `ERROR`, поздние события и дубликаты?
3. Что делать с `DONE` без `FINAL`: немедленный `FAILED`, ожидание до deadline, либо иной совместимый механизм? Первый срез не должен снова давать ложный успех и не должен зависать бесконечно.
4. Как безопасно обрабатывать duplicate request id и рестарт брокера между dispatch и final, не допуская автоматической повторной отправки prompt?
5. Как исправить pending TTL, чтобы ожидающий CLI гарантированно получил terminal error и job был сохранён?
6. Каким должен быть read-only HTTP endpoint последнего результата и клиентский контракт (`404` при отсутствии, JSON validation, query parsing, size limits)? Есть ли более простой вариант в рамках текущей архитектуры?
7. Какие изменения нужны в `client.py` и `command.py`, чтобы normal request считался успешным только после непустого full final, а `get-last` не касался браузера?
8. Какие точные тесты обязательны для этого среза (store persistence/recovery, broker event ordering, duplicate terminals, TTL, HTTP, CLI)?
9. Есть ли в предложенном scope скрытая архитектурная ошибка или слишком большой объём? Предложите независимо лучший первый срез, если он отличается.

Дайте конкретный план изменений по файлам и достаточно точный псевдокод/сигнатуры, чтобы решение можно было реализовать без догадок. Отдельно перечислите риски и edge cases. Не предлагайте официальные API. Не предлагайте полный rewrite на другом языке только ради rewrite. Если контекста не хватает, не гадайте — явно укажите, чего именно не хватает.

## Общие требования к качеству кода

Пишите production-ready код: безопасный, быстрый, читаемый и поддерживаемый. Выбирайте простое решение без оверинжиниринга. Учитывайте edge cases, валидацию, ошибки и производительность. Соблюдайте SOLID, DRY и KISS там, где это уместно. Добавляйте тесты или примеры использования только тогда, когда это действительно нужно для задачи.

Не продвигайте собственные идеи как заведомо правильные: любые гипотезы, планы и архитектурные предложения предварительны и должны проверяться. Самостоятельно проанализируйте код и контекст, определите реальные дефекты и предложите более безопасный, производительный или современный вариант, если он отличается от гипотез локального агента.
