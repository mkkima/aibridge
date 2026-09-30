# AI Bridge: native C++20 Windows EXE с лёгким Win32 UI

## Product task

AI Bridge — локальный Windows-инструмент, который связывает консольную команду `ai` с уже открытыми авторизованными вкладками ChatGPT, Gemini и Claude через Chrome Manifest V3 extension. Официальные API провайдеров полностью исключены. Пользователь хочет заменить Python control plane на очень маленький native C++ `.exe` и получить интерфейс, в котором видны ход заданий, состояние моста и моделей, результаты и логи, а также можно управлять тем, что выводится в консоль.

Нужна не абстрактная оценка, а архитектура и конкретный план немедленной реализации безопасного вертикального среза. Конечная цель — после миграции не требовать Python runtime. Существующее расширение на первом шаге желательно оставить совместимым, иначе одновременно меняется слишком много слоёв.

## Жёсткие ограничения и цели

- Windows 10/11 x64.
- C++20, MSVC, CMake/Ninja уже доступны.
- Один небольшой self-contained `.exe` либо `.exe` плюс объективно необходимая минимальная data/config directory.
- Без Qt, Electron, Chromium, .NET runtime, Java и тяжёлых UI-frameworks.
- Предпочтителен чистый Win32 UI/Common Controls; system tray допустим.
- Официальные AI API запрещены.
- Нельзя терять уже реализованные durable semantics: SQLite, first-terminal-wins, `FINAL` как единственный успех, `UNKNOWN_OUTCOME`, отсутствие автоматического повторного submit после неоднозначного dispatch.
- Сохранить команды либо совместимый launcher:

```text
ai --file prompt.md --chatgpt --gemini --claude
ai --get-last --chatgpt
ai --status
```

- Обычный запуск EXE показывает GUI; запуск с CLI-аргументами должен отправлять команду единственному broker instance и возвращать корректный stdout/stderr/exit code.
- UI не должен блокировать broker transport или SQLite.
- Нужно максимально малое количество зависимостей и маленький Release binary, но не ценой самописного небезопасного JSON/SQLite/протокола.
- Текущие пользовательские изменения в extension (Gemini Deep Research) нельзя потерять.
- Миграция должна быть поэтапной с возможностью отката на Python v0.2 до подтверждённого parity.

## Желаемое поведение GUI

Минимальный интерфейс:

1. Общий статус: broker running/stopped, порт, версия, DB, uptime.
2. Статус ChatGPT/Gemini/Claude: вкладка доступна/нет, active job, последний terminal outcome.
3. Таблица заданий: id/source/state/stage/duration/result bytes/error.
4. Панель выбранного задания: status timeline, финальный текст, error details.
5. Лог-панель с фильтрами, Copy, Clear view, Save; очистка UI не должна удалять durable journal.
6. Start/Stop/Restart bridge и безопасная отмена только там, где она не создаёт повторный submit.
7. Настройка console mode: summary only / errors / verbose progress; эти настройки должны влиять на будущие CLI-вызовы.
8. По возможности tray icon; закрытие окна может скрывать UI, не останавливая broker.

UI не должен притворяться, что DOM-ответ полный: он показывает фактически committed `FINAL` и точное состояние job.

## Доступный toolchain

```text
MSVC cl.exe 14.51.36231 (x64)
CMake installed
Ninja installed
MSBuild installed
Windows SDK 10.0.26100.0
Python 3.12 currently used only by legacy implementation
```

## Текущая архитектура v0.2

```text
ai.cmd -> Python CLI -> loopback WebSocket -> Python broker
                                             |
                                             +-> SQLite durable job journal
                                             |
Chrome MV3 service worker <- loopback WS ----+
       |
       +-> exact provider page content.js (currently one monolith)
```

Loopback endpoints:

```text
ws://127.0.0.1:8765
http://127.0.0.1:8766/health
http://127.0.0.1:8766/sources
http://127.0.0.1:8766/results/last?source=...
```

Current process is started on demand through Windows Scheduled Task `AI Bridge`. Current broker is v0.2.0 and sees all three sources.

Repository:

```text
aibridge/
  broker.py       ~947 lines
  job_store.py    ~700 lines
  client.py       ~345 lines
  command.py      ~398 lines
  lifecycle.py    ~309 lines
extension/
  manifest.json
  src/background.js ~436 lines
  src/content.js    ~1710 lines
tests/              32 passing Python unit/integration tests
ai.cmd
scripts/install.ps1
scripts/uninstall.ps1
```

## Current extension contract

`extension/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "AI Browser Bridge",
  "version": "0.1.11",
  "permissions": ["alarms", "tabs", "scripting"],
  "host_permissions": [
    "http://127.0.0.1/*",
    "ws://127.0.0.1/*",
    "https://chatgpt.com/*",
    "https://chat.openai.com/*",
    "https://gemini.google.com/*",
    "https://claude.ai/*"
  ],
  "background": {"service_worker": "src/background.js", "type": "module"},
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

The service worker maintains a WebSocket connection to `ws://127.0.0.1:8765` and sends:

```json
{"type":"BRIDGE_READY","source":"chatgpt","sources":["chatgpt","gemini","claude"],"timestamp":123}
{"type":"BRIDGE_HEARTBEAT","source":"chatgpt","sources":[...],"timestamp":123}
```

It receives:

```json
{
  "id":"uuid",
  "type":"COMPLETION_REQUEST",
  "context":"prompt text",
  "stream":true,
  "source":"chatgpt|gemini|claude"
}
```

It returns `STATUS`, `TOKEN`, `REPLACE`, `FINAL`, `DONE`, `ERROR` (and legacy `COMPLETION_*` aliases). `FINAL.content` is the full authoritative text as extracted by the current content script. `DONE` follows `FINAL` on success over the same FIFO WebSocket.

Relevant current `background.js` behavior:

```javascript
const BROKER_URL = "ws://127.0.0.1:8765";
const BROKER_HEALTH_URL = "http://127.0.0.1:8766/health";
let socket;
const outboundQueue = [];

async function connect() {
  if (!(await isBrokerReady())) { scheduleReconnect(); return; }
  socket = new WebSocket(BROKER_URL);
  socket.addEventListener("open", async () => {
    await sendBridgeReady();
    flushOutboundQueue();
    await sendHeartbeat();
  });
  socket.addEventListener("message", event => void handleBrokerMessage(event.data));
}

async function handleBrokerMessage(rawMessage) {
  const payload = JSON.parse(rawMessage);
  if (payload.type !== "COMPLETION_REQUEST") return;
  const tab = await resolveTargetTab(payload.source);
  await sendPromptToTab(tab.id, {
    type: "AIBRIDGE_RUN_PROMPT",
    id: payload.id,
    context: payload.context,
  });
}

chrome.runtime.onMessage.addListener(message => {
  if (message?.type === "AIBRIDGE_COMPLETION_EVENT") {
    sendToBroker(message.payload);
  }
});
```

## Durable state that must survive the rewrite

Current SQLite default path:

```text
%LOCALAPPDATA%\AIBridge\data\jobs-v1.sqlite3
```

Core states:

```text
ACCEPTED -> QUEUED -> DISPATCHED -> STREAMING -> COMPLETED
                                              -> FAILED
                                              -> UNKNOWN_OUTCOME
```

Terminal states are immutable. `FINAL` with valid non-empty content is the only way to `COMPLETED`. `ERROR` becomes `FAILED`. `DONE` before `FINAL` becomes `FAILED/DONE_WITHOUT_FINAL`. A send failure after durable `DISPATCH_ATTEMPTED`, broker restart, or absolute deadline becomes `UNKNOWN_OUTCOME`. No automatic redispatch is allowed after `DISPATCHED`.

Schema shape (exact important constraints abbreviated only where repetitive):

```sql
PRAGMA application_id = 0x41494252; -- AIBR
PRAGMA user_version = 1;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL CHECK (source IN ('chatgpt','gemini','claude')),
  context_sha256 TEXT NOT NULL,
  context_bytes INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'ACCEPTED','QUEUED','DISPATCHED','STREAMING',
    'COMPLETED','FAILED','UNKNOWN_OUTCOME'
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
  terminal_event_id INTEGER
);

CREATE TABLE job_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT,
  applied INTEGER NOT NULL,
  created_at_ms INTEGER NOT NULL,
  details_json TEXT
);
```

The Python `JobStore` owns one SQLite connection on the broker event-loop thread. Each mutation is `BEGIN IMMEDIATE`, event insert, guarded state update, commit. `terminal_event_id` gives strict last-result ordering. On startup every non-terminal job becomes `UNKNOWN_OUTCOME/BROKER_RESTARTED`.

## Current broker behavior that C++ must preserve

Simplified exact request flow:

```python
async def handle_request(payload, raw):
    validate id/source/nonempty context/max bytes
    create = store.create_job(id, source, context_hash, deadline)
    if duplicate:
        replay stored FINAL only for identical completed job
        otherwise return deterministic ERROR; never redispatch
    register live CLI route
    extension = exact source-matching extension client
    if no extension:
        durable ACCEPTED -> QUEUED, retain pending raw request until TTL
        return
    durable ACCEPTED -> DISPATCHED before websocket.send(raw)
    if send fails:
        durable -> UNKNOWN_OUTCOME; notify CLI
```

Simplified response flow:

```python
async def handle_response(payload, raw):
    load job; ignore unknown/terminal jobs
    reject response.source mismatch
    STATUS: forward to CLI
    TOKEN/REPLACE: first event marks STREAMING, forward
    FINAL:
        validate nonempty UTF-8/full size
        commit COMPLETED + content/hash/bytes first
        only after successful commit forward FINAL and report success
    ERROR: commit FAILED, then notify CLI
    DONE before terminal: commit FAILED/DONE_WITHOUT_FINAL, synthesize ERROR
```

Maintenance sweep runs every second:

- queued pending deadline -> `FAILED/PENDING_TIMEOUT` and live CLI error;
- accepted/dispatched/streaming absolute deadline -> `UNKNOWN_OUTCOME/JOB_DEADLINE_EXCEEDED` and live CLI error.

HTTP `/results/last` reads through the broker-owned SQLite connection and returns content plus bytes/SHA-256/timestamp. No successful `FINAL` may be forwarded if DB commit fails.

## Current CLI behavior

`ai.cmd` currently calls Python:

```bat
@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
call "%~dp0python_runtime.cmd" -m aibridge.cli %*
```

Normal request fan-out sends one request per selected source concurrently and prints only a completion summary. `--get-last` uses HTTP and prints exactly the stored full content. `--status` reads health/sources. The new EXE CLI mode must preserve UTF-8 stdout/stderr and meaningful exit codes for scripts.

## Existing startup/install behavior

`scripts/install.ps1` finds Python, registers an on-demand Scheduled Task with no triggers, and adds the repo to user PATH. `ai` health-checks and starts the task when required. The C++ target should simplify this: ideally a single executable manages single-instance startup and GUI/tray lifetime, while CLI invocations connect over user-scoped IPC. It must not require elevation.

## Preliminary hypotheses (not proven; critique them)

1. A single `aibridge.exe` can have two modes:
   - no command arguments / `--ui`: primary GUI + broker process;
   - CLI arguments: connect to the primary instance through a per-user named pipe, submit/follow job, mirror selected events to stdout/stderr, exit.
2. Native Win32 UI + Common Controls will yield the smallest dependency-free UI. WinUI 3, Qt, WebView2 and Electron are inappropriate for the size goal.
3. SQLite amalgamation compiled into the EXE is preferable to replacing the proven journal or shipping a separate runtime DLL.
4. A small proven WebSocket/HTTP implementation may be safer than hand-writing RFC6455. But Boost.Beast/Asio may materially increase source/build complexity and binary size; Windows WebSocket Protocol Component API may require substantial handshake/socket plumbing. The best transport library/API is unresolved.
5. A header-only JSON parser such as RapidJSON or simdjson/nlohmann may be acceptable, but binary size and validation behavior must be compared. Hand-written ad-hoc JSON parsing is presumed unsafe.
6. The GUI thread should only render immutable snapshots/events posted from a broker worker; SQLite and socket I/O must never run on the UI thread.
7. For fast safe migration, first native slice could run side-by-side on alternate ports and prove protocol/storage/UI parity before `ai.cmd` and extension switch. This is a hypothesis, not permission to leave Python permanently.

Check these hypotheses independently. Do not merely ratify them.

## Questions

1. Is a full native C++ rewrite justified under the explicit size/no-Python constraint, and what exact architecture should one small EXE use?
2. What is the safest smallest choice for loopback WebSocket server + tiny HTTP server on Windows: Windows WebSocket API with Winsock/HTTP parsing, Boost.Beast, standalone Asio + a small WS library, libhv, another library, or temporarily Native Messaging? Compare maintenance, binary size, correctness and licensing.
3. Which JSON solution and SQLite packaging strategy minimize binary/dependency size without sacrificing validation and durability?
4. How should the single-instance process and CLI-to-GUI/broker IPC work? Define named-pipe identity/security, framing, request/stream/terminal events, reconnect and exit-code behavior.
5. Define the threading model: UI message loop, network I/O, SQLite ownership, maintenance timers, event fan-out and shutdown ordering.
6. Give a concrete Win32 UI layout and state model that stays responsive and useful but does not become another monolith.
7. How can console output be controlled from GUI settings while preserving deterministic behavior for scripts and redirected stdout?
8. How should the existing SQLite v1 schema be reused/migrated by C++ without corrupting a DB last opened by Python? What tests prove cross-language parity?
9. What exact first vertical slice can be implemented now and compiled into a real EXE, while avoiding a fake UI shell that merely wraps Python?
10. What modules/files/CMake targets should be created? Give concrete class/interface boundaries and build flags for a small Release binary.
11. What binary size is realistically achievable (order of magnitude, not fabricated precision), and which features/dependencies dominate it?
12. What are the critical risks and edge cases: multiple instances, stale named pipe, browser reconnects, fragmented/masked WebSocket frames, large UTF-8 messages, disk full, UI close during active jobs, system suspend/clock changes, upgrade/rollback and privacy?
13. Define a staged migration/rollback plan and exact acceptance tests before the C++ EXE replaces v0.2.

If recommended solutions differ materially, explain trade-offs. Do not propose official AI APIs. Do not propose Qt/Electron/.NET. Do not guess when context is insufficient; state what measurement or missing file is needed.

## Общие требования к качеству кода

Пишите production-ready код: безопасный, быстрый, читаемый и поддерживаемый. Выбирайте простое решение без оверинжиниринга. Учитывайте edge cases, валидацию, ошибки и производительность. Соблюдайте SOLID, DRY и KISS там, где это уместно. Добавляйте тесты или примеры использования только тогда, когда это действительно нужно для задачи.

Не продвигайте собственные идеи как заведомо правильные: любые гипотезы, планы и архитектурные предложения предварительны и должны проверяться. Самостоятельно предложите лучший подход и конкретный план изменений. Отдельно укажите риски и edge cases.
