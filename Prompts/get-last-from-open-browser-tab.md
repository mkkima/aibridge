# Вернуть `ai --get-last` к чтению открытой браузерной вкладки

## Product task

AI Bridge — локальный мост между CLI и браузерным расширением для открытых вкладок ChatGPT, Gemini и Claude. Официальные API не используются.

Нужно изменить только семантику:

```text
ai --get-last --chatgpt
ai --get-last --gemini
ai --get-last --claude
```

Команда должна прочитать последний ответ непосредственно из открытой вкладки выбранной модели через существующий DOM-парсер расширения. Она не должна возвращать сохранённый ответ из SQLite и не должна создавать/отправлять новый пользовательский prompt.

Текущий DOM-парсинг и обычная отправка промтов должны остаться без изменений.

## Проблема

После недавней durable-jobs переделки `--get-last` читает глобально последний `COMPLETED` результат модели из `%LOCALAPPDATA%\AIBridge\data\jobs-v1.sqlite3`. Из-за этого после создания нового чата или удаления старого браузерного чата команда возвращает старый локально сохранённый текст. База не знает понятия браузерного чата.

Ожидается, что новый чат/текущая открытая вкладка полностью определяют результат `--get-last`.

## Релевантная структура

```text
aibridge/command.py       CLI и ветка --get-last
aibridge/client.py        WebSocket client и HTTP get_last_result
aibridge/broker.py        durable broker, routing, SQLite jobs, HTTP /results/last
aibridge/job_store.py     SQLite journal
extension/src/background.js  выбор вкладки и forwarding в content script
extension/src/content.js     DOM extraction; уже умеет читать latest assistant text
tests/test_command.py
tests/test_broker.py
tests/test_integration.py
```

## Текущий CLI (`aibridge/command.py`)

```python
if args.get_last:
    source = target_sources_from_args(args)[0]
    bridge = CloudBridge(
        CloudBridgeConfig(url=args.url, health_url=args.health_url)
    )
    try:
        result = await bridge.get_last_result(source)
    except NoStoredResultError:
        print(
            f"{source_label(source)}: нет сохранённого завершённого ответа.",
            file=sys.stderr,
        )
        sys.exit(1)
    except CloudBridgeError as exc:
        print(f"{source_label(source)}: ошибка: {exc}", file=sys.stderr)
        sys.exit(1)
    print(result.content)
    return
```

До durable-переделки CLI делал `prompt = ""`, вызывал обычный `stream_completion`, собирал полученный текст и печатал его.

## Текущий клиент (`aibridge/client.py`)

```python
async def get_last_result(self, source: str) -> StoredResult:
    normalized_source = source.strip().casefold() if isinstance(source, str) else ""
    if normalized_source not in VALID_SOURCES:
        raise CloudBridgeError(f"Unsupported AI source: {source}")

    def fetch() -> StoredResult:
        query = urllib.parse.urlencode({"source": normalized_source})
        url = f"{self._config.health_url.rstrip('/')}/results/last?{query}"
        # HTTP GET, validation of stored SQLite result...

    return await asyncio.to_thread(fetch)

async def stream_completion(
    self,
    context: str,
    source: str | None = None,
) -> AsyncIterator[str]:
    request_id = uuid.uuid4().hex
    request = {
        "id": request_id,
        "type": "COMPLETION_REQUEST",
        "context": context,
        "stream": True,
    }
    if source:
        request["source"] = source
    # websocket connect, send, then require non-empty FINAL
```

## Текущий broker request validation (`aibridge/broker.py`)

```python
if event_type == "COMPLETION_REQUEST":
    await self._handle_completion_request(client_id, websocket, payload, raw_message)
elif event_type in READY_TYPES:
    await self._handle_extension_ready(client_id, websocket, payload)
elif event_type in RESPONSE_TYPES:
    await self._handle_completion_response(payload, raw_message)
```

```python
request_id = payload.get("id")
# validate id and source
context = payload.get("context")
if not isinstance(context, str) or not context.strip():
    await self._send_error(
        websocket,
        request_id,
        "EMPTY_CONTEXT_NOT_SUPPORTED",
        "Prompt context must be non-empty; use ai --get-last to read stored output",
    )
    return

created = self._store.create_job(
    job_id=request_id,
    source=source,
    context=context,
    deadline_at_ms=...,
)
# registers core websocket, selects extension, queues/persists if absent,
# dispatches raw COMPLETION_REQUEST to extension.
```

Response handling currently starts by loading the durable job:

```python
request_id = payload.get("id")
job = self._store.get_job(request_id)
if job is None or job.state in TERMINAL_STATES:
    return

# validates source; STATUS/TOKEN/REPLACE forwarded;
# FINAL is validated, stored through self._store.complete(...), then forwarded;
# DONE before FINAL fails the durable job.
```

Routing maps:

```python
self._extension_clients: dict[int, ExtensionClient] = {}
self._core_clients_by_request: dict[str, WebSocketServerProtocol] = {}
self._pending_requests: dict[str, PendingRequest] = {}
self._request_ids_by_client: dict[int, set[str]] = {}
```

Extension selection chooses a connected extension client whose advertised sources contain the requested source.

## Background tab selection (`extension/src/background.js`)

```javascript
async function handleBrokerMessage(rawMessage) {
  const payload = JSON.parse(rawMessage);
  if (payload.type !== "COMPLETION_REQUEST") {
    return;
  }

  handleBrokerMessagePromise = handleBrokerMessagePromise.then(async () => {
    try {
      const tab = await resolveTargetTab(payload.source);
      const source = sourceFromUrl(tab.url);
      await sendPromptToTab(tab.id, {
        type: "AIBRIDGE_RUN_PROMPT",
        id: payload.id,
        context: payload.context,
      });
    } catch (error) {
      sendToBroker({ type: "ERROR", id: payload.id, message: String(error) });
    }
  });
}
```

```javascript
async function resolveTargetTab(targetSource) {
  const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeAiTab = activeTabs.find((tab) => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (activeAiTab?.id) return activeAiTab;

  const allTabs = await chrome.tabs.query({});
  const aiTab = allTabs.find((tab) => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (aiTab?.id) return aiTab;
  throw new Error(`Open ${targetSource || "Gemini, ChatGPT, or Claude"} tab first`);
}
```

## Content script already supports read-only extraction (`extension/src/content.js`)

```javascript
chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "AIBRIDGE_RUN_PROMPT") {
    return false;
  }
  void runPrompt(message.id, String(message.context || ""));
  return false;
});

async function runPrompt(id, context) {
  try {
    const source = currentSource();
    if (!source) throw new Error("Open Gemini, ChatGPT, or Claude tab");

    if (!context || context.trim() === "") {
      const latestText = getLatestAssistantText(source);
      emitStatus(id, source, "content.done", "Fetched latest text");
      emit({ type: "FINAL", id, source, content: latestText });
      emit({ type: "DONE", id, source });
      return;
    }

    // Existing prompt insertion/submission/stream parsing path.
  } catch (error) {
    emit({ type: "ERROR", id, message: `${String(error)} (...)` });
  }
}
```

`getLatestAssistantText(source)` is the existing provider-specific DOM parser and must not be rewritten in this task.

## Tests

Current `tests/test_command.py` explicitly asserts the unwanted behavior:

```python
async def test_get_last_reads_store_without_browser_request(self):
    # fake get_last_result returns StoredResult("stored output")
    await command.main()
    self.assertEqual(stdout.getvalue(), "stored output\n")
    self.assertEqual(fake_bridge.get_last_calls, ["chatgpt"])
    self.assertFalse(fake_bridge.stream_called)
```

Current integration test asserts that a normal prompt FINAL is committed and then served by HTTP `get_last_result`; normal durable request behavior may remain, but CLI `--get-last` must stop using it.

## Предварительная гипотеза (не доказанный факт)

Более явный и безопасный протокол может использовать отдельные типы, например:

```text
core -> broker -> extension: TAB_SNAPSHOT_REQUEST {id, source}
background -> content: AIBRIDGE_READ_CURRENT {id}
content -> broker -> core: STATUS, FINAL, DONE/ERROR
```

Такой запрос должен быть ephemeral/read-only: не создавать SQLite job, не сохранять извлечённый ответ, не ставиться в durable prompt queue и быстро ошибаться, если вкладки/extension нет. Broker может держать отдельную map `request_id -> (core websocket, source)` до FINAL/ERROR и очищать её при disconnect/timeout.

Это предположение, не готовое решение. Проверьте, действительно ли отдельный тип лучше минимального варианта `COMPLETION_REQUEST` с явным `readCurrentTab: true`, и предложите собственный вариант.

## Требуемый результат от анализа

1. Определите самый простой production-ready способ вернуть чтение открытой вкладки без SQLite.
2. Дайте конкретный план изменений по `command.py`, `client.py`, `broker.py`, `background.js`, `content.js` и тестам.
3. Укажите, как безопасно коррелировать read-only response, валидировать source/id/content и очищать routing при FINAL, ERROR, core/extension disconnect и timeout.
4. Решите, должен ли запрос ждать появления вкладки или сразу возвращать понятную ошибку.
5. Оцените выбор вкладки при нескольких вкладках одной модели: активная в текущем окне прежде фоновых.
6. Укажите риски, edge cases и минимальный diff.
7. Не предлагайте официальные API.
8. Не меняйте существующие provider-specific DOM selectors/parser без доказанной необходимости.
9. Не гадайте при нехватке данных — явно укажите недостающий контекст.
10. Независимо предложите лучший подход, а не только оцените гипотезу локального агента.

## Общие требования к качеству кода

Пишите production-ready код: безопасный, быстрый, читаемый и поддерживаемый. Выбирайте простое решение без оверинжиниринга. Учитывайте edge cases, валидацию, ошибки и производительность. Соблюдайте SOLID, DRY и KISS там, где это уместно. Добавляйте тесты или примеры использования только тогда, когда это действительно нужно для задачи.

Не продвигайте предложенную гипотезу как заведомо правильную. Проверьте её по предоставленному коду и предложите более безопасный, производительный или современный вариант, если он отличается. Дайте конкретный план изменений и явно укажите риски.
