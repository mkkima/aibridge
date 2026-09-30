# Архитектурный аудит и радикальная переработка AI Bridge

## Роль и формат ответа

Вы — независимый principal engineer / системный архитектор. Нужен строгий архитектурный аудит локального Windows-инструмента, который отправляет один prompt в открытые авторизованные веб-вкладки ChatGPT, Gemini и Claude, ждёт ответы и возвращает их в CLI.

Не считайте текущую архитектуру или перечисленные ниже гипотезы правильными. Самостоятельно определите фундаментальные причины хрупкости по коду, протоколу, логам и ограничениям. Если лучший вариант принципиально отличается от текущей концепции, прямо предложите отказаться от расширения или от браузерной автоматизации. Если данных недостаточно, не гадайте: перечислите, чего именно не хватает и как это измерить.

Ответ дайте на русском языке. Код сейчас менять не требуется. Нужен конкретный, технически исполнимый план, а не общие советы.

## Product task

Текущий AI Bridge считается крайне ненадёжным: ошибочные клики, неверная вставка prompt, потеря/обрезка/дублирование текста ответа, ложное определение завершения, вылеты, зависания и успех «по удаче». Разрешена глубокая переработка вплоть до смены языка, процесса установки, протокола и отказа от Chrome extension.

Нужно решить:

1. Стоит ли вообще сохранять идею управления уже открытыми consumer-web вкладками через Manifest V3 extension.
2. Если нет, чем заменить: официальными API, Playwright-managed Chromium с persistent profile, CDP-подключением к отдельному браузеру, Native Messaging + extension, гибридом или иным вариантом.
3. Если браузерная автоматизация остаётся, как сделать её максимально детерминированной, тестируемой, наблюдаемой и восстанавливаемой при изменениях SPA.
4. Какую целевую архитектуру и язык выбрать для production-ready локального продукта на Windows.
5. Как мигрировать от текущего решения поэтапно, не делая большой непроверяемый rewrite.

Текущий CLI-контракт полезен и его желательно сохранить либо предоставить совместимый слой:

```text
ai --file prompt.md
ai --file prompt.md --chatgpt --gemini --claude
ai --get-last --chatgpt
ai --status
```

По умолчанию запрос всем трём источникам выполняется параллельно; обычный запрос скрывает тексты и печатает сводку ответивших моделей; `--get-last` читает последний ответ ровно одной модели. Есть дополнительный режим `ai debate`, но он вторичен: сначала нужен надёжный транспорт одного запроса/ответа.

Неизвестное продуктовое ограничение, которое нельзя молча предполагать: допустимы ли API-ключи и отдельная плата за API. Поэтому дайте рекомендацию в двух ветках:

- если официальные API и их стоимость допустимы;
- если обязательна работа через уже оплаченные/авторизованные веб-интерфейсы без API-ключей.

Также оцените риски Terms of Service, блокировок/антибот-защиты и невозможность гарантировать стабильность автоматизации чужого consumer UI.

## Текущее и ожидаемое поведение

### Текущее

- Python CLI запускает по требованию фоновый Python broker через Windows Scheduled Task.
- Broker слушает loopback WebSocket `ws://127.0.0.1:8765` и HTTP health/sources на `127.0.0.1:8766`.
- Manifest V3 service worker расширения держит WebSocket к broker, ищет подходящую вкладку и посылает команду content script.
- Один большой content script на 1710 строк содержит логику трёх разных сайтов: поиск composer, вставка, выбор send, Deep Research в Gemini, определение нового ответа, DOM polling/MutationObserver, парсинг Markdown-подобного текста и эвристики завершения.
- CLI открывает отдельное WebSocket-соединение на каждый запрос/источник.
- Нет надёжного конечного deadline для ответа по умолчанию (`response_timeout_seconds=None`).
- Нет устойчивого request journal, attempt/lease, ACK протокола, cancel, capability negotiation, идемпотентности отправки, отдельной очереди на вкладку или защиты от повторной отправки после reconnect.
- Есть in-memory outbound queue service worker и in-memory pending queue broker. Состояние теряется при рестарте процесса/service worker.
- UI-автоматизация опирается на широкие CSS-селекторы, `innerText`, синтетические paste/input/keyboard/pointer события, последний видимый button и DOM quiet-time.
- Browser-side тестов нет.

### Ожидаемое

- Каждый запрос имеет наблюдаемый жизненный цикл и конечный deadline.
- Нельзя печатать успех, пока не доказано, что prompt действительно отправлен в нужную модель и полный ответ действительно зафиксирован.
- Сбой одного provider не блокирует остальные.
- Повтор/перезапуск не должен молча создать второй запрос в UI.
- Отмена CLI должна доходить до исполнителя, насколько это возможно.
- Нужны структурированные ошибки по стадиям, request-correlated логи, диагностические артефакты (DOM snapshot/screenshot/trace с контролем приватности), метрики успешности и времени стадий.
- Каждый provider — отдельный адаптер и отдельный автомат состояний, а не условные ветки в одном глобальном скрипте.
- Нужны контрактные тесты протокола, unit-тесты адаптеров на сохранённых DOM fixtures и реальные Playwright E2E/canary тесты на специальных тестовых профилях.
- Изменение DOM одного сайта не должно ломать остальные.
- Установка, обновление и откат должны быть воспроизводимыми.

## Структура релевантных файлов и объём

```text
ai.cmd
python_runtime.cmd
requirements.txt                         # websockets>=12,<14
aibridge/
  cli.py                    30 lines      # ensure bridge, then command
  command.py               388 lines      # argparse, parallel fan-out, summary/get-last
  lifecycle.py             309 lines      # health, lock, Scheduled Task/direct spawn
  task_runner.py             87 lines
  broker.py                 400 lines      # WebSocket router and HTTP health
  client.py                 200 lines      # one WS connection per completion
  debate_*.py, report_writer.py            # secondary orchestration/reporting
extension/
  manifest.json
  src/background.js         436 lines      # MV3 worker, WebSocket, routing/tab selection
  src/content.js           1710 lines      # all three site adapters and DOM parsing
tests/
  test_broker.py             58 lines
  test_lifecycle.py         113 lines
  test_task_runner.py        36 lines
  mock_extension.py          39 lines
```

Незакоммиченные локальные изменения уже добавляют Gemini Deep Research UI flow, reinjection stale content script и ChatGPT large-paste workaround. Это не выбранная целевая архитектура; это дополнительное свидетельство роста эвристик.

## Конфигурация и manifest

`requirements.txt`:

```text
websockets>=12.0,<14
```

`extension/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "AI Browser Bridge",
  "version": "0.1.11",
  "description": "Routes local ai commands to open ChatGPT, Gemini, or Claude tabs.",
  "permissions": ["alarms", "tabs", "scripting"],
  "host_permissions": [
    "http://127.0.0.1/*",
    "ws://127.0.0.1/*",
    "https://chatgpt.com/*",
    "https://chat.openai.com/*",
    "https://gemini.google.com/*",
    "https://claude.ai/*"
  ],
  "background": {
    "service_worker": "src/background.js",
    "type": "module"
  },
  "content_scripts": [{
    "matches": [
      "https://chatgpt.com/*",
      "https://chat.openai.com/*",
      "https://gemini.google.com/*",
      "https://claude.ai/*"
    ],
    "js": ["src/content.js"],
    "run_at": "document_idle"
  }]
}
```

## Текущий wire protocol и broker

Типы сообщений фактически не имеют version/schema validation. Основной request:

```json
{
  "id": "uuid-hex",
  "type": "COMPLETION_REQUEST",
  "context": "prompt text",
  "stream": true,
  "source": "chatgpt|gemini|claude"
}
```

Ответы: `STATUS`, `TOKEN`, `REPLACE`, `FINAL`, `DONE`, `ERROR` и дубли с `COMPLETION_` prefix. `FINAL` и `DONE` оба terminal в broker; при первом terminal routing state удаляется.

Ключевые части `aibridge/broker.py`:

```python
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
        self._extension_clients = {}
        self._core_clients_by_request = {}
        self._pending_requests = {}
        self._request_ids_by_client = {}
        self._extension_sources = []
        self._ready = False

    async def handle_client(self, websocket):
        async with self._lock:
            client_id = self._next_client_id
            self._next_client_id += 1
            self._request_ids_by_client[client_id] = set()
        try:
            async for raw_message in websocket:
                await self._handle_raw_message(client_id, websocket, raw_message)
        finally:
            await self._remove_client(client_id, websocket)

    async def _handle_completion_request(self, client_id, websocket, payload, raw_message):
        request_id = payload.get("id")
        now = time.monotonic()
        async with self._lock:
            self._expire_pending_locked(now)
            self._core_clients_by_request[request_id] = websocket
            self._request_ids_by_client.setdefault(client_id, set()).add(request_id)
            extension = self._select_extension_locked(payload.get("source"))
            if extension is None:
                if len(self._pending_requests) >= MAX_PENDING_REQUESTS:
                    await self._safe_send_json(websocket, {
                        "type": "ERROR", "id": request_id,
                        "message": "AI Bridge pending request queue is full",
                    })
                    self._core_clients_by_request.pop(request_id, None)
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

    async def _handle_extension_ready(self, client_id, websocket, payload):
        source = str(payload.get("source") or "none")
        sources = self._normalize_sources(payload.get("sources"), source)
        async with self._lock:
            self._extension_clients[client_id] = ExtensionClient(
                id=client_id, websocket=websocket, source=source, sources=sources,
            )
            self._rebuild_sources_locked()
            pending_items = list(self._pending_requests.items())
        for request_id, pending in pending_items:
            if await self._safe_send(websocket, pending.raw):
                async with self._lock:
                    self._pending_requests.pop(request_id, None)

    async def _handle_completion_response(self, payload, raw_message):
        request_id = payload.get("id")
        async with self._lock:
            core_websocket = self._core_clients_by_request.get(request_id)
            if payload.get("type") in TERMINAL_TYPES:
                self._core_clients_by_request.pop(request_id, None)
                self._pending_requests.pop(request_id, None)
                for request_ids in self._request_ids_by_client.values():
                    request_ids.discard(request_id)
        if core_websocket is not None:
            await self._safe_send(core_websocket, raw_message)

    def _select_extension_locked(self, source):
        if isinstance(source, str) and source.strip():
            wanted = source.strip().casefold()
            for client in self._extension_clients.values():
                if client.source.casefold() == wanted or wanted in [s.casefold() for s in client.sources]:
                    return client
        return next(iter(self._extension_clients.values()), None)
```

Важная деталь: один extension client рекламирует список всех открытых sources, поэтому broker выбирает один service worker; уже worker затем выбирает первую active/background tab нужного host. Нет стабильного tab/session identity.

## Client и CLI fan-out

Ключевые части `aibridge/client.py`:

```python
@dataclass(frozen=True)
class CloudBridgeConfig:
    url: str = "ws://127.0.0.1:8765"
    health_url: str = "http://127.0.0.1:8766"
    extension_wait_timeout_seconds: float = 120.0
    response_timeout_seconds: float | None = None
    max_message_bytes: int = 8 * 1024 * 1024

async def stream_completion(self, context: str, source: str | None = None):
    request_id = uuid.uuid4().hex
    request = {"id": request_id, "type": "COMPLETION_REQUEST",
               "context": context, "stream": True}
    if source:
        request["source"] = source
    websocket = await self._connect()
    try:
        await websocket.send(json.dumps(request, ensure_ascii=False))
        saw_final = False
        accumulated_text = ""
        while True:
            if self._config.response_timeout_seconds is None:
                raw_message = await websocket.recv()
            else:
                async with asyncio.timeout(self._config.response_timeout_seconds):
                    raw_message = await websocket.recv()
            event = self._parse_message(raw_message)
            if event is None or event.get("id") != request_id:
                continue
            event_type = event.get("type")
            if event_type in {"TOKEN", "COMPLETION_TOKEN"}:
                token = event.get("token")
                # Heuristic: detect whether a 'token' is actually a full snapshot.
                # If so, emit a private CLEAR marker to CLI.
                clean_acc = accumulated_text.lstrip()
                clean_tok = token.lstrip()
                is_snapshot = bool(clean_acc) and (
                    clean_tok.startswith(clean_acc) or
                    (len(clean_acc) >= 5 and len(clean_tok) >= 5 and
                     clean_tok[:5] == clean_acc[:5] and
                     len(clean_tok) >= len(clean_acc) * 0.7)
                )
                if is_snapshot:
                    accumulated_text = token
                    yield "\0CLEAR\0" + token
                else:
                    accumulated_text += token
                    yield token
                continue
            if event_type in {"REPLACE", "COMPLETION_REPLACE"}:
                content = event.get("content")
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
                break
            if event_type in {"ERROR", "COMPLETION_ERROR"}:
                raise CloudBridgeError(str(event.get("message") or "Bridge returned an error"))
    finally:
        await websocket.close()
```

Ключевой CLI fan-out:

```python
async def collect_source(source, capture_text: bool):
    text = ""
    saw_text = False
    try:
        async for token in bridge.stream_completion(prompt, source=source):
            chunk = token_text(token)
            if chunk.strip():
                saw_text = True
            if capture_text:
                text = chunk if token.startswith("\0CLEAR\0") else text + chunk
        cleaned = clean_source_text(text, source) if capture_text else ""
        if not saw_text or (capture_text and not cleaned.strip()):
            raise RuntimeError("ответ не получен")
        return source, cleaned, None
    except Exception as e:
        return source, None, e

tasks = [asyncio.create_task(collect_source(s, False)) for s in target_sources]
results = await asyncio.gather(*tasks)
```

Обычный multi-source запуск не сохраняет response в Python: `capture_text=False`; `--get-last` позже отправляет в вкладку request с пустым context, и content script заново парсит последний видимый assistant response из DOM. Поэтому «last» не привязан к исходному request id и может вернуть другой turn.

## MV3 service worker

Существенные части `extension/src/background.js`:

```javascript
const BROKER_URL = "ws://127.0.0.1:8765";
const BROKER_HEALTH_URL = "http://127.0.0.1:8766/health";
const HEARTBEAT_INTERVAL_MS = 15_000;
const MAX_OUTBOUND_QUEUE_SIZE = 200;

let socket;
let isConnecting = false;
let reconnectTimer;
let reconnectAttempt = 0;
let heartbeatTimer;
const outboundQueue = [];

void connect();
chrome.alarms.create("aibridge-reconnect", { periodInMinutes: 0.5 });

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "AIBRIDGE_COMPLETION_EVENT") return false;
  sendToBroker(message.payload);
  return false;
});

async function connect() {
  if (isConnecting || socket?.readyState === WebSocket.OPEN ||
      socket?.readyState === WebSocket.CONNECTING) return;
  isConnecting = true;
  if (!(await isBrokerReady())) {
    isConnecting = false;
    scheduleReconnect();
    return;
  }
  socket = new WebSocket(BROKER_URL);
  const activeSocket = socket;
  activeSocket.addEventListener("open", async () => {
    isConnecting = false;
    reconnectAttempt = 0;
    startHeartbeat();
    await sendBridgeReady();
    flushOutboundQueue();
    await sendHeartbeat();
    flushOutboundQueue();
  });
  activeSocket.addEventListener("message", (event) => {
    void handleBrokerMessage(event.data);
  });
  activeSocket.addEventListener("close", () => {
    socket = undefined;
    stopHeartbeat();
    scheduleReconnect();
  });
}

let handleBrokerMessagePromise = Promise.resolve();
async function handleBrokerMessage(rawMessage) {
  const payload = JSON.parse(rawMessage);
  if (payload.type !== "COMPLETION_REQUEST") return;
  handleBrokerMessagePromise = handleBrokerMessagePromise.then(async () => {
    try {
      const tab = await resolveTargetTab(payload.source);
      const source = sourceFromUrl(tab.url);
      await sendPromptToTab(tab.id, {
        type: "AIBRIDGE_RUN_PROMPT", id: payload.id, context: payload.context,
      });
    } catch (error) {
      sendToBroker({type: "ERROR", id: payload.id, message: String(error)});
    }
  });
  await handleBrokerMessagePromise;
}

async function sendPromptToTab(tabId, message) {
  try {
    await chrome.tabs.sendMessage(tabId, message);
  } catch (error) {
    if (!/receiving end does not exist|could not establish connection/i.test(String(error))) {
      throw error;
    }
    await chrome.scripting.executeScript({target: {tabId}, files: ["src/content.js"]});
    await chrome.tabs.sendMessage(tabId, message);
  }
}

function sendToBroker(payload, options = {queue: true}) {
  if (socket?.readyState !== WebSocket.OPEN) {
    if (options.queue) enqueueOutbound(payload);
    void connect();
    return;
  }
  try {
    socket.send(JSON.stringify(payload));
  } catch (error) {
    if (options.queue) enqueueOutbound(payload);
    socket.close();
    scheduleReconnect();
  }
}

async function resolveTargetTab(targetSource) {
  const activeTabs = await chrome.tabs.query({active: true, currentWindow: true});
  const activeAiTab = activeTabs.find(tab => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (activeAiTab?.id) return activeAiTab;
  const allTabs = await chrome.tabs.query({});
  const aiTab = allTabs.find(tab => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (aiTab?.id) return aiTab;
  throw new Error(`Open ${targetSource || "Gemini, ChatGPT, or Claude"} tab first`);
}
```

Все три requests сериализуются одной глобальной promise chain, хотя они адресованы разным вкладкам. Ошибка в самой chain потенциально влияет на последующие элементы; queue не содержит устойчивых attempt/sequence metadata. Результат `tabs.sendMessage()` означает доставку сообщения content script, но не подтверждает submit в UI.

## Content script: общий workflow

Начало `extension/src/content.js`:

```javascript
const CONTENT_SCRIPT_VERSION = "2026.07.16-gemini-deep-research-pointer";
const HOSTNAME = window.location.hostname;
const GEMINI_RESEARCH_FINAL_QUIET_MS = 10_000;
let lastGeminiResearchReportText = "";

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "AIBRIDGE_RUN_PROMPT") return false;
  void runPrompt(message.id, String(message.context || ""));
  return false;
});

async function runPrompt(id, context) {
  try {
    const source = currentSource();
    if (!context || context.trim() === "") {
      const latestText = getLatestAssistantText(source);
      emit({type: "FINAL", id, source, content: latestText});
      emit({type: "DONE", id, source});
      return;
    }
    if (source === "gemini") await prepareGeminiAfterOpenResearch(id, source);
    const input = await waitForElement(() => findPromptInput(source), 15_000);
    const baselineInfo = source === "claude" ? captureClaudeBaseline() : undefined;
    const baselineText = getLatestAssistantText(source);
    insertPrompt(input, context);
    await delay(350);
    if (source === "chatgpt") await expandChatGptPastedTextIfNeeded(input, context);
    const submitMethod = await submitPrompt(source, input);
    await streamResponse({id, source, baselineText, baselineInfo, promptText: context});
    emit({type: "DONE", id, source});
  } catch (error) {
    emit({type: "ERROR", id,
          message: `${error instanceof Error ? error.message : String(error)} (content ${CONTENT_SCRIPT_VERSION})`});
  }
}
```

Content script принимает несколько сообщений без per-tab mutex или active-request registry: каждый `runPrompt` запускается через `void` конкурентно. Background сериализует только момент dispatch, но не ждёт завершения UI request, поэтому следующие requests могут пересечься в одной вкладке.

## Content script: вставка и отправка

```javascript
function findPromptInput(source) {
  const selectorsBySource = {
    claude: ["div.ProseMirror[contenteditable='true']", "[contenteditable='true']", "textarea"],
    chatgpt: ["#prompt-textarea", "textarea[data-testid='prompt-textarea']",
              "div[contenteditable='true'][role='textbox']", "[contenteditable='true']"],
    gemini: ["[contenteditable='true']", "rich-textarea [contenteditable='true']",
             ".ql-editor", "textarea"],
  };
  return firstVisible(selectorsBySource[source]);
}

function insertPrompt(input, promptText) {
  input.click();
  input.focus();
  if ("value" in input) {
    const prototype = input.tagName === "TEXTAREA"
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    setter ? setter.call(input, promptText) : input.value = promptText;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    input.dispatchEvent(new InputEvent("input", {
      bubbles: true, inputType: "insertText", data: promptText,
    }));
    return String(input.value || "").length;
  }

  try {
    const dataTransfer = new DataTransfer();
    dataTransfer.setData("text/plain", promptText);
    input.dispatchEvent(new ClipboardEvent("paste", {
      bubbles: true, cancelable: true, clipboardData: dataTransfer,
    }));
    input.dispatchEvent(new Event("input", {bubbles: true}));
  } catch {}

  if (!normalizeText(input.innerText || input.textContent || "")
      .includes(normalizeText(promptText))) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(input);
    range.deleteContents();
    range.collapse(false);
    selection?.removeAllRanges();
    selection?.addRange(range);
    document.execCommand("insertText", false, promptText);
  }
  if (!normalizeText(input.innerText || input.textContent || "")
      .includes(normalizeText(promptText))) {
    input.textContent = promptText;
  }
  input.dispatchEvent(new InputEvent("beforeinput", {
    bubbles: true, inputType: "insertText", data: promptText,
  }));
  input.dispatchEvent(new InputEvent("input", {
    bubbles: true, inputType: "insertText", data: promptText,
  }));
  input.dispatchEvent(new Event("change", {bubbles: true}));
  return normalizeText(input.innerText || input.textContent || "").length;
}

async function submitPrompt(source, input) {
  const button = await waitForSendButton(source, 5_000);
  if (button) {
    button.element.click();
    return `button ${button.selector}`;
  }
  dispatchEnter(input);
  return "keyboard Enter fallback";
}
```

Gemini send selectors включают особенно широкие fallback:

```javascript
gemini: [
  "button[aria-label='Send message']",
  "button[aria-label*='send' i]",
  "button[data-testid='send-button']",
  "button.send-button",
  ".send-button",
  "button:has(mat-icon)",
  "button[type='submit']",
  "div:has(> rich-textarea) ~ div button:last-of-type",
  "div:has(> [contenteditable='true']) button:last-of-type",
]
```

Затем кандидат принимается, если enabled и для Gemini имеет `send|arrow|...` где-либо в label/title/text/`innerHTML` либо является последней кнопкой parent. Финальный fallback берёт последнюю enabled button в ближайшем container.

## Content script: извлечение и определение завершения

Общий ChatGPT/Gemini loop:

```javascript
async function streamResponse({id, source, baselineText, baselineInfo, promptText}) {
  if (source === "claude") return streamClaudeResponse({id, baselineInfo, promptText});
  let lastText = "";
  let hasNewResponse = false;
  let lastTextChangedAt = Date.now();
  let latestText = "";
  const startedAt = Date.now();
  let lastActivityAt = Date.now();
  const observer = new MutationObserver(() => { lastActivityAt = Date.now(); });
  observer.observe(document.body, {
    childList: true, subtree: true, characterData: true, attributes: true,
    attributeFilter: ["class", "style", "disabled", "data-is-streaming",
                      "data-message-author-role"],
  });
  try {
    while (true) {
      const currentText = normalizeText(getLatestAssistantText(source));
      const elapsed = Date.now() - startedAt;
      if (!hasNewResponse) {
        if (!currentText || currentText === normalizeText(baselineText)) {
          await delay(250);
          continue; // no overall deadline
        }
        hasNewResponse = true;
      }
      if (currentText && currentText !== lastText) {
        if (currentText.startsWith(lastText)) {
          emit({type: "TOKEN", id, source, token: currentText.slice(lastText.length)});
        } else {
          emit({type: "REPLACE", id, source, content: currentText});
        }
        lastText = currentText;
        lastTextChangedAt = Date.now();
        await delay(250);
        continue;
      }
      // Gemini-specific Start Research click/retry logic omitted here but it uses
      // visible control labels, broad state checks and quiet-time heuristics.
      const generating = isGenerating(source);
      if (currentText && !generating) {
        const quietForMs = Date.now() - Math.max(lastActivityAt, lastTextChangedAt);
        const requiredQuietMs = source === "gemini" /* research */ ? 10_000 : 1_250;
        if (quietForMs >= requiredQuietMs) {
          emitFinalIfNeeded(id, source, lastText || latestText);
          return;
        }
      }
      await delay(250);
    }
  } finally {
    observer.disconnect();
  }
}
```

`getLatestAssistantText` выбирает последний видимый кандидат после сортировки по `getBoundingClientRect().top`:

```javascript
const selectorsBySource = {
  chatgpt: ["[data-message-author-role='assistant']", "div.agent-turn", ".markdown"],
  gemini: ["model-response", "message-content", "[data-test-id='response']",
           "[data-testid='response']", ".model-response-text",
           "div[class*='response']", ".markdown"],
};
```

Он фильтрует user nodes несколькими селекторами и читает `innerText`/`textContent`. У Gemini immersive research parser ищет `deep-research-report`, `research-report`, class/testid fragments, а затем generic `article`, `[role=document]`, `main`; принимает документ длиной >=600 с heading>=1 и paragraph/list/pre/blockquote>=3, превращает DOM blocks в Markdown и фильтрует несколько UI labels.

Claude имеет отдельный polling loop. Он пытается связать новый user node с assistant root по DOM order, отправляет полный `REPLACE` snapshot при каждом изменении и завершает, когда `data-is-streaming=false` или появились кнопки Copy/Retry/Read aloud и DOM тихий 1.5s; fallback — 18s тишины. Общего deadline также нет. Root selectors:

```javascript
[
  "[data-is-streaming]",
  "[data-message-author-role='assistant']",
  "[data-testid='assistant-message']",
  ".font-claude-message",
  "[class*='font-claude-message']",
  ".prose",
]
```

## Gemini Deep Research UI flow как пример роста сложности

При открытом immersive report текущая незакоммиченная логика:

1. Определяет отсутствие composer и наличие длинного research document либо close/back control.
2. Ищет Close/Back по label/icon; fallback выбирает любую usable кнопку/ссылку в левом верхнем углу по геометрии.
3. Синтетически dispatch-ит pointerdown/mousedown/pointerup/mouseup и `.click()`.
4. Если не сработало — `window.history.back()`.
5. Ищет New chat по локализованному label.
6. Ищет `+`/tools около composer по labels/icon/geometry.
7. Ищет Deep Research в menu по локализованному тексту и пытается подтвердить selection через aria/class/нахождение вне menu.
8. После submit ищет Start Research, до трёх раз кликает и ждёт activity/text/quiet-time.

Это не доказательство, что extension обязательно надо удалить; это конкретный сценарий для оценки стоимости сопровождения и тестируемости.

## Lifecycle, health и фактические логи

`aibridge/lifecycle.py`:

- проверяет `GET /health` с timeout 1s;
- сериализует одновременный startup file lock;
- сначала `schtasks.exe /Run /TN "AI Bridge"`, затем fallback detached `pythonw -m aibridge.broker`;
- ждёт readiness суммарно 10s;
- scheduled task зарегистрирован without triggers, execution limit disabled, restart count 999;
- broker health показывает только процесс/in-memory состояние, не готовность конкретного tab adapter выполнить запрос.

Фактический health 17 июля 2026:

```json
{
  "ok": true,
  "service": "aibridge",
  "version": "0.1.0",
  "sources": ["chatgpt", "claude", "gemini"],
  "pendingRequests": 0,
  "extensionClients": 1
}
```

Фактический `bridge-task.err.log` три раза содержит одинаковый необработанный disconnect stack trace, в то время как health остаётся ok:

```text
connection handler failed
OSError: [WinError 64] Указанное сетевое имя более недоступно
...
websockets.exceptions.ConnectionClosedError: no close frame received or sent
  File "C:\workspace\aibridge\aibridge\broker.py", line 80, in handle_client
    async for raw_message in websocket:
```

Это не обязательно причина всех пользовательских зависаний, но подтверждённый дефект обработки ожидаемых disconnect и качества журналирования.

## Тесты и проверенные наблюдения

В проекте 7 Python unit tests; все проходят. `node --check` для обоих JS файлов тоже проходит.

Покрыто:

- один request route broker extension→core по id;
- health service name;
- lifecycle fast-path;
- Scheduled Task preference;
- startup lock между 8 процессами;
- task runner не запускает второй healthy broker.

Не покрыто:

- schema/protocol state machine;
- duplicate ids, reconnect/redelivery, lost ACK, late terminal events;
- multiple extension clients/tabs и правильный routing;
- одновременно три providers;
- content script concurrency;
- ни один selector, click, insert, parser или completion detector;
- service worker suspension/restart;
- browser navigation/reload mid-request;
- long prompts, large outputs, code blocks, tables, citations, canvas/virtualized DOM;
- login/consent/rate-limit/error/interstitial states;
- Deep Research;
- cancellation/deadlines/recovery;
- privacy/redaction диагностических артефактов.

## Проверенные актуальные ограничения/возможности платформ (официальная документация)

Эти факты предназначены для критики вариантов, а не для навязывания решения:

- Manifest V3 extension service worker обычно может завершаться после простоя; Chrome рекомендует не полагаться на global variables и сохранять состояние. Начиная с Chrome 116 активный WebSocket продлевает lifetime, но worker всё равно надо проектировать устойчивым к неожиданному завершению.
- `chrome.runtime.connectNative()` доступен service worker/extension pages, но не content scripts; он запускает native host и держит его до disconnect. Native host→Chrome message ограничен 1 MB, на Windows требуется registry manifest и строгий length-prefixed stdio protocol.
- Playwright locators дают auto-wait/actionability и retryable assertions; tracing может сохранять screenshots, DOM snapshots и network activity. Это улучшает действия и диагностику, но не создаёт стабильного контракта с чужим DOM.
- Playwright extension setup требует persistent Chromium context; актуальная документация предупреждает, что Chrome/Edge убрали flags для side-load extensions и рекомендует bundled Chromium. То есть managed Playwright browser может не быть тем же пользовательским Chrome с уже открытыми вкладками.
- Chrome DevTools Protocol позволяет DOM/Runtime/Input/Network automation, но tip-of-tree protocol меняется без гарантии backward compatibility. Для подключения к обычному Chrome нужен явно запущенный/доступный debugging endpoint; это отдельная operational/security модель.
- У OpenAI, Google Gemini и Anthropic есть официальные API со структурированными response objects и streaming. Google в актуальной документации рекомендует Interactions API для новых проектов и предоставляет background execution/observable steps, включая specialized agents; эти возможности надо проверить по требуемым моделям/Deep Research и стоимости, а не считать точным эквивалентом consumer UI.

## Предварительные гипотезы локального аудитора — не доказанные факты

Проверьте и при необходимости опровергните каждую:

1. Основная проблема не в Python как языке, а в отсутствии чётких контрактов и в попытке трактовать DOM чужих SPA как стабильный API; простой rewrite 1:1 на Rust/Go/TypeScript не исправит это.
2. Если API-ключи допустимы, production-grade default должен быть official API adapters; browser UI bridge можно оставить только как явно best-effort compatibility backend с более слабым SLA.
3. Если consumer UI обязателен, managed Playwright Chromium с отдельным persistent profile, provider-specific TypeScript adapters, locator/actionability, tracing и явными state machines может быть диагностируемее content scripts. Но отдельный browser/profile и антибот/ToS могут сделать его продуктово неприемлемым; надо измерить PoC, не объявлять победителем заранее.
4. Native Messaging может упростить процессный lifecycle и убрать loopback broker/WebSocket/reconnect, но само по себе не исправляет DOM click/parsing. Оно имеет смысл лишь как transport/lifecycle upgrade при сохранении extension.
5. Если надо работать именно с уже открытыми вкладками обычного Chrome, extension может оказаться единственным приемлемым browser integration point. Тогда её следует оставить тонким transport/tab agent, вынести provider adapters в отдельные модули, использовать per-tab actor/queue и устойчивый journal на native side.
6. «Exactly once» submit в чужом UI недостижим без provider-generated stable turn id или проверяемого fingerprint после submit. Реальная семантика должна быть at-most-once by default с состоянием `unknown_outcome` и ручным/политикой-controlled retry, чтобы не дублировать дорогой запрос.
7. Определение полного ответа только по DOM quiet-time принципиально ненадёжно. Надо исследовать доступные стабильные positive signals и, возможно, network observation; перехват внутренних приватных endpoint может быть ещё более хрупким/опасным и не должен предлагаться без явной оценки.

## Что требуется в ответе

1. **Краткий вердикт:** сохранить extension, заменить или перейти на гибрид; отдельно для ветки с API и без API.
2. **Root-cause audit:** системные причины проблем на каждом слое: CLI/lifecycle, broker/protocol, MV3 worker, tab routing, UI actions, response correlation/extraction, completion detection, storage/get-last, tests/observability/security.
3. **Сравнительная таблица минимум пяти вариантов:**
   - harden current extension + loopback WebSocket;
   - extension + Native Messaging;
   - managed Playwright persistent Chromium;
   - CDP attach/managed Chrome;
   - official APIs;
   - при необходимости гибрид.
   Оцените reliability ceiling, совместимость с уже открытыми вкладками, стоимость, ToS/anti-bot risk, Deep Research parity, сложность установки, тестируемость, наблюдаемость и сопровождение при изменении UI.
4. **Целевая архитектура:** компоненты и границы ответственности, provider adapter interface, transport, persistence, request state machine, queue/concurrency, deadlines/cancel/retry semantics, capability discovery, versioning и error taxonomy. Покажите текстовую схему потока одного запроса.
5. **Выбор языка/стека:** что оставить на Python, что писать на TypeScript/Node, имеет ли смысл Go/Rust/.NET. Обоснуйте через browser tooling, Windows packaging, concurrency, typing, diagnostics и team complexity. Не рекомендуйте rewrite только ради языка.
6. **Request state machine:** предложите конкретные состояния и переходы минимум от accepted до sent/streaming/completed/failed/cancelled/unknown_outcome. Укажите, где нужны ACK и durable records и что делать при reconnect на каждой стадии.
7. **Provider adapter design:** как изолировать ChatGPT/Gemini/Claude, локаторы, input, submit proof, response anchoring, structured extraction, finalization. Что нельзя абстрагировать общим кодом.
8. **Testing/observability plan:** protocol tests, crash/reconnect fault injection, DOM fixture contract tests, Playwright E2E, canary, traces/screenshots/DOM snapshot redaction, SLO/metrics. Дайте измеримые acceptance criteria, например success rate и false-success rate, а не «стало стабильнее».
9. **План миграции:** последовательные фазы с минимальным риском, feature flags/rollback, и gate после каждой фазы. Опишите, что можно переиспользовать, а что удалить.
10. **Короткий decision PoC (1–2 недели):** какие эксперименты и метрики объективно выберут между extension, Native Messaging, Playwright/CDP и API, не переписывая всё заранее.
11. **Риски и edge cases:** navigation/reload, multiple tabs/profiles, stale DOM, virtualized messages, network loss, service worker death, partial/huge outputs, research jobs на десятки минут, login/consent/captcha/rate-limit, localization, Markdown/code/tables/citations, privacy, user edits composer, Ctrl+C, broker crash и late events.
12. Если считаете какое-либо предлагаемое направление опасным или архитектурно непригодным, скажите это прямо и объясните почему.

## Общие требования к качеству кода

Пишите production-ready код: безопасный, быстрый, читаемый и поддерживаемый. Выбирайте простое решение без оверинжиниринга. Учитывайте edge cases, валидацию, ошибки и производительность. Соблюдайте SOLID, DRY и KISS там, где это уместно. Добавляйте тесты или примеры использования только тогда, когда это действительно нужно для задачи.

Не продвигайте гипотезы локального аудитора как заведомо правильные. Самостоятельно проанализируйте предоставленный код, логи, платформенные ограничения и продуктовые развилки. Если есть более безопасный, производительный, современный или поддерживаемый вариант, предложите его. Дайте конкретный план изменений, риски и edge cases. Если контекста недостаточно, явно укажите, какого именно контекста не хватает и как его собрать.
