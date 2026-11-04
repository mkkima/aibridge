const BROKER_URL = "ws://127.0.0.1:8765";
const BROKER_HEALTH_URL = "http://127.0.0.1:8766/health";
const RECONNECT_ALARM = "aibridge-reconnect";
const HEARTBEAT_INTERVAL_MS = 15_000;
const BASE_RECONNECT_DELAY_MS = 500;
const MAX_RECONNECT_DELAY_MS = 10_000;
const MAX_OUTBOUND_QUEUE_SIZE = 200;
const MAX_PROXIED_TEXT_FILE_BYTES = 4 * 1024 * 1024;
const TARGET_HOSTS = {
  chatgpt: ["chatgpt.com", "chat.openai.com"],
  gemini: ["gemini.google.com"],
  claude: ["claude.ai"],
};

let socket = undefined;
let isConnecting = false;
let reconnectTimer = undefined;
let reconnectAttempt = 0;
let heartbeatTimer = undefined;
const outboundQueue = [];
const browserAPI = globalThis.browser || globalThis.chrome;

void connect();
try {
  browserAPI.alarms.create(RECONNECT_ALARM, { periodInMinutes: 1 });
} catch (error) {
  console.warn("[AI Bridge] failed to create alarm", error);
}

browserAPI.runtime.onInstalled.addListener(() => {
  void connect();
});

browserAPI.runtime.onStartup.addListener(() => {
  void connect();
});

browserAPI.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== RECONNECT_ALARM) {
    return;
  }

  void connect();
  void sendHeartbeat();
});

browserAPI.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "AIBRIDGE_INSPECT_CHATGPT_FILE") {
    void inspectChatGptFileInMainWorld(message, sender)
      .then((result) => sendResponse({ ok: true, ...result }))
      .catch((error) => sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }));
    return true;
  }

  if (message?.type === "AIBRIDGE_FETCH_CHATGPT_FILE") {
    void proxyChatGptFileRequest(message, sender)
      .then((result) => sendResponse({ ok: true, ...result }))
      .catch((error) => sendResponse({
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }));
    return true;
  }

  if (!message || message.type !== "AIBRIDGE_COMPLETION_EVENT") {
    return false;
  }

  sendToBroker(message.payload);
  return false;
});

async function inspectChatGptFileInMainWorld(message, sender) {
  if (sourceFromUrl(sender?.tab?.url) !== "chatgpt" || !sender?.tab?.id) {
    throw new Error("ChatGPT file inspection is only available to a ChatGPT tab");
  }
  const fileName = String(message.fileName || "").trim();
  if (!fileName || fileName.length > 500) {
    throw new Error("ChatGPT file inspection requires a valid filename");
  }

  try {
    const results = await browserAPI.scripting.executeScript({
      target: { tabId: sender.tab.id },
      world: "MAIN",
      func: inspectChatGptFileState,
      args: [fileName],
    });
    return results?.[0]?.result || {};
  } catch (error) {
    try {
      const results = await browserAPI.scripting.executeScript({
        target: { tabId: sender.tab.id },
        func: inspectChatGptFileState,
        args: [fileName],
      });
      return results?.[0]?.result || {};
    } catch {
      throw error;
    }
  }
}

function inspectChatGptFileState(fileName) {
  const canonical = (value) => String(value || "")
    .toLowerCase()
    .replace(/^.*[\\/]/, "")
    .replace(/[^a-z0-9а-яё]+/gi, "");
  const expectedName = canonical(fileName);
  const controls = [...document.querySelectorAll("button, [role='button'], a, [role='link']")]
    .filter((node) => {
      const text = String(node.innerText || node.textContent || "").trim();
      return text && canonical(text).includes(expectedName);
    });
  const target = controls[controls.length - 1];
  if (!target) {
    return {};
  }

  const roots = [];
  let element = target;
  for (let depth = 0; element && depth < 7; depth += 1) {
    for (const key of Object.getOwnPropertyNames(element)) {
      if (/^__(?:react|next)/i.test(key)) {
        try {
          roots.push(element[key]);
        } catch {
          // Ignore inaccessible framework internals.
        }
      }
    }
    element = element.parentElement;
  }

  const fileIds = new Set();
  const urls = new Set();
  let content = "";
  const seen = new WeakSet();
  const queue = roots.map((value) => ({ value, depth: 0 }));
  let inspected = 0;

  while (queue.length > 0 && inspected < 12_000) {
    const entry = queue.shift();
    const value = entry.value;
    if (!value || typeof value !== "object" || seen.has(value) || entry.depth > 10) {
      continue;
    }
    seen.add(value);
    inspected += 1;

    let pairs = [];
    try {
      pairs = Object.entries(value);
    } catch {
      continue;
    }
    const stringPairs = pairs.filter(([_key, child]) => typeof child === "string");
    const objectMentionsFile = stringPairs.some(([_key, child]) => (
      canonical(child).includes(expectedName)
    ));

    for (const [key, child] of stringPairs) {
      const text = String(child);
      const idMatch = text.match(/(?:file-service|sediment):\/\/([a-z0-9_-]{12,})/i) ||
        text.match(/\b(file[-_][a-z0-9_-]{10,})\b/i);
      if (idMatch) {
        fileIds.add(idMatch[1]);
      }
      if (
        /^(?:https?:|sandbox:|blob:)/i.test(text) &&
        (
          canonical(text).includes(expectedName) ||
          /\/backend-api\/files\/|oaiusercontent\.com/i.test(text)
        )
      ) {
        urls.add(text);
      }
      if (
        objectMentionsFile &&
        /^(?:content|text|file_text|fileContent|contents)$/i.test(key) &&
        text.length > 0 &&
        text.length <= 4 * 1024 * 1024
      ) {
        content ||= text;
      }
    }

    for (const [key, child] of pairs) {
      if (/^(?:return|child|sibling|stateNode|alternate)$/i.test(key)) {
        continue;
      }
      if (child && typeof child === "object" && !(child instanceof Element)) {
        queue.push({ value: child, depth: entry.depth + 1 });
      }
    }
  }

  return {
    fileIds: [...fileIds].slice(0, 20),
    urls: [...urls].slice(0, 20),
    content,
  };
}

async function proxyChatGptFileRequest(message, sender) {
  if (sourceFromUrl(sender?.tab?.url) !== "chatgpt") {
    throw new Error("ChatGPT file proxy is only available to a ChatGPT tab");
  }

  const url = new URL(String(message.url || ""));
  if (
    url.protocol !== "https:" ||
    !(
      url.hostname === "files.oaiusercontent.com" ||
      url.hostname.endsWith(".oaiusercontent.com")
    )
  ) {
    throw new Error("ChatGPT file proxy rejected this host");
  }

  const headers = { Accept: "*/*" };
  if (typeof message.accessToken === "string" && message.accessToken) {
    headers.Authorization = `Bearer ${message.accessToken}`;
  }
  if (typeof message.accountId === "string" && message.accountId) {
    headers["ChatGPT-Account-Id"] = message.accountId;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await fetch(url.href, {
      method: "GET",
      cache: "no-store",
      credentials: "omit",
      headers,
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`file request returned HTTP ${response.status}`);
    }

    const declaredLength = Number(response.headers.get("content-length") || "0");
    if (declaredLength > MAX_PROXIED_TEXT_FILE_BYTES) {
      throw new Error("file is larger than the bridge limit");
    }
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > MAX_PROXIED_TEXT_FILE_BYTES) {
      throw new Error("file is larger than the bridge limit");
    }

    return {
      contentType: String(response.headers.get("content-type") || ""),
      bytesBase64: bytesToBase64(new Uint8Array(buffer)),
    };
  } finally {
    clearTimeout(timeout);
  }
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunkSize = 32_768;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}

browserAPI.tabs.onActivated.addListener(() => {
  void connect();
  void sendBridgeReady();
});

browserAPI.tabs.onUpdated.addListener((_tabId, changeInfo) => {
  if (changeInfo.status === "complete" || changeInfo.url) {
    void connect();
    void sendBridgeReady();
  }
});

browserAPI.tabs.onRemoved.addListener(() => {
  void sendBridgeReady();
});

async function connect() {
  if (
    isConnecting ||
    socket?.readyState === WebSocket.OPEN ||
    socket?.readyState === WebSocket.CONNECTING
  ) {
    return;
  }

  clearReconnectTimer();
  isConnecting = true;

  if (!(await isBrokerReady())) {
    isConnecting = false;
    scheduleReconnect();
    return;
  }

  try {
    socket = new WebSocket(BROKER_URL);
  } catch (error) {
    isConnecting = false;
    socket = undefined;
    console.warn("[AI Bridge] failed to create socket", error);
    scheduleReconnect();
    return;
  }

  const activeSocket = socket;

  activeSocket.addEventListener("open", async () => {
    if (socket !== activeSocket) {
      return;
    }

    isConnecting = false;
    reconnectAttempt = 0;
    startHeartbeat();
    await sendBridgeReady();
    flushOutboundQueue();
    await sendHeartbeat();
    flushOutboundQueue();

    const source = await currentSource();
    console.info("[AI Bridge] connected", BROKER_URL, source);
  });

  activeSocket.addEventListener("message", (event) => {
    if (socket !== activeSocket) {
      return;
    }

    void handleBrokerMessage(event.data);
  });

  activeSocket.addEventListener("close", () => {
    if (socket !== activeSocket) {
      return;
    }

    isConnecting = false;
    socket = undefined;
    stopHeartbeat();
    scheduleReconnect();
  });

  activeSocket.addEventListener("error", () => {
    if (socket !== activeSocket) {
      return;
    }

    isConnecting = false;
    try {
      activeSocket.close();
    } catch {
      socket = undefined;
    }
    scheduleReconnect();
  });
}

function scheduleReconnect() {
  if (reconnectTimer !== undefined) {
    return;
  }

  const delay = Math.min(
    MAX_RECONNECT_DELAY_MS,
    BASE_RECONNECT_DELAY_MS * 2 ** reconnectAttempt,
  );
  reconnectAttempt += 1;

  reconnectTimer = setTimeout(() => {
    reconnectTimer = undefined;
    void connect();
  }, delay);
}

function clearReconnectTimer() {
  if (reconnectTimer === undefined) {
    return;
  }

  clearTimeout(reconnectTimer);
  reconnectTimer = undefined;
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    void sendHeartbeat();
  }, HEARTBEAT_INTERVAL_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer === undefined) {
    return;
  }

  clearInterval(heartbeatTimer);
  heartbeatTimer = undefined;
}

async function sendBridgeReady() {
  const { source, sources } = await currentBridgeStatus();
  sendToBroker(
    {
      type: "BRIDGE_READY",
      source,
      sources,
      timestamp: Date.now(),
    },
    { queue: false },
  );
}

async function sendHeartbeat() {
  const { source, sources } = await currentBridgeStatus();
  sendToBroker(
    {
      type: "BRIDGE_HEARTBEAT",
      source,
      sources,
      timestamp: Date.now(),
    },
    { queue: false },
  );
}

let handleBrokerMessagePromise = Promise.resolve();

async function handleBrokerMessage(rawMessage) {
  let payload;
  try {
    payload = JSON.parse(rawMessage);
  } catch {
    return;
  }

  if (payload.type !== "COMPLETION_REQUEST") {
    return;
  }

  handleBrokerMessagePromise = handleBrokerMessagePromise.then(async () => {
    try {
      sendToBroker({
        type: "STATUS",
        id: payload.id,
        stage: "background.received_request",
        message: "Background received cloud request",
      });

      const tab = await resolveTargetTab(payload.source);
      const source = sourceFromUrl(tab.url);
      
      sendToBroker({
        type: "STATUS",
        id: payload.id,
        stage: "background.target_tab",
        source,
        message: `Dispatching prompt to tab ${tab.id} (${source})`,
      });

      await sendPromptToTab(tab.id, {
        type: "AIBRIDGE_RUN_PROMPT",
        id: payload.id,
        context: payload.context,
      });

      sendToBroker({
        type: "STATUS",
        id: payload.id,
        stage: "background.prompt_sent",
        source,
        message: "Background dispatched prompt",
      });
    } catch (error) {
      sendToBroker({
        type: "ERROR",
        id: payload.id,
        message: error instanceof Error ? error.message : String(error),
      });
    }
  });
  
  await handleBrokerMessagePromise;
}

async function sendPromptToTab(tabId, message) {
  try {
    await browserAPI.tabs.sendMessage(tabId, message);
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : String(error);
    if (!/receiving end does not exist|could not establish connection/i.test(errorMessage)) {
      throw error;
    }

    console.info("[AI Bridge] reinjecting content script into stale tab", tabId);
    await browserAPI.scripting.executeScript({
      target: { tabId },
      files: ["src/content.js"],
    });
    await browserAPI.tabs.sendMessage(tabId, message);
  }
}

function sendToBroker(payload, options = { queue: true }) {
  if (socket?.readyState !== WebSocket.OPEN) {
    if (options.queue) {
      enqueueOutbound(payload);
    }
    void connect();
    return;
  }

  try {
    socket.send(JSON.stringify(payload));
  } catch (error) {
    if (options.queue) {
      enqueueOutbound(payload);
    }
    console.warn("[AI Bridge] failed to send payload", error);
    try {
      socket.close();
    } catch {
      socket = undefined;
    }
    scheduleReconnect();
  }
}

async function isBrokerReady() {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, 2000);

  try {
    const response = await fetch(BROKER_HEALTH_URL, {
      cache: "no-store",
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}

function enqueueOutbound(payload) {
  if (outboundQueue.length >= MAX_OUTBOUND_QUEUE_SIZE) {
    outboundQueue.shift();
  }
  outboundQueue.push(payload);
}

function flushOutboundQueue() {
  while (socket?.readyState === WebSocket.OPEN && outboundQueue.length > 0) {
    const payload = outboundQueue.shift();
    if (!payload) {
      continue;
    }

    try {
      socket.send(JSON.stringify(payload));
    } catch (error) {
      outboundQueue.unshift(payload);
      console.warn("[AI Bridge] failed to flush payload", error);
      try {
        socket.close();
      } catch {
        socket = undefined;
      }
      scheduleReconnect();
      return;
    }
  }
}

async function resolveTargetTab(targetSource) {
  const activeTabs = await browserAPI.tabs.query({ active: true, currentWindow: true });
  const activeAiTab = activeTabs.find((tab) => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (activeAiTab?.id) {
    console.info("[AI Bridge] using active AI tab", activeAiTab.id, activeAiTab.url);
    return activeAiTab;
  }

  const allTabs = await browserAPI.tabs.query({});
  const aiTab = allTabs.find((tab) => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (aiTab?.id) {
    console.info("[AI Bridge] using background AI tab", aiTab.id, aiTab.url);
    return aiTab;
  }

  throw new Error(`Open ${targetSource || "Gemini, ChatGPT, or Claude"} tab first`);
}

async function currentSource() {
  const activeTabs = await browserAPI.tabs.query({ active: true, currentWindow: true });
  const activeSource = sourceFromUrl(activeTabs[0]?.url);
  if (activeSource) {
    return activeSource;
  }

  const allTabs = await browserAPI.tabs.query({});
  return sourceFromUrl(allTabs.find((tab) => sourceFromUrl(tab.url))?.url) || "none";
}

async function currentBridgeStatus() {
  const [source, sources] = await Promise.all([currentSource(), openSources()]);
  return { source, sources };
}

async function openSources() {
  const tabs = await browserAPI.tabs.query({});
  const seenSources = new Set();
  for (const tab of tabs) {
    const source = sourceFromUrl(tab.url);
    if (source) {
      seenSources.add(source);
    }
  }

  return Object.keys(TARGET_HOSTS).filter((source) => seenSources.has(source));
}

function sourceFromUrl(url) {
  if (typeof url !== "string") {
    return "";
  }

  let hostname;
  try {
    hostname = new URL(url).hostname.toLowerCase();
  } catch {
    return "";
  }

  for (const [source, hosts] of Object.entries(TARGET_HOSTS)) {
    if (hosts.includes(hostname)) {
      return source;
    }
  }

  return "";
}
