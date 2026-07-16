const BROKER_URL = "ws://127.0.0.1:8765";
const BROKER_HEALTH_URL = "http://127.0.0.1:8766/health";
const RECONNECT_ALARM = "aibridge-reconnect";
const HEARTBEAT_INTERVAL_MS = 15_000;
const BASE_RECONNECT_DELAY_MS = 500;
const MAX_RECONNECT_DELAY_MS = 10_000;
const MAX_OUTBOUND_QUEUE_SIZE = 200;
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

void connect();
chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 });

chrome.runtime.onInstalled.addListener(() => {
  void connect();
});

chrome.runtime.onStartup.addListener(() => {
  void connect();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== RECONNECT_ALARM) {
    return;
  }

  void connect();
  void sendHeartbeat();
});

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "AIBRIDGE_COMPLETION_EVENT") {
    return false;
  }

  sendToBroker(message.payload);
  return false;
});

chrome.tabs.onActivated.addListener(() => {
  void sendBridgeReady();
});

chrome.tabs.onUpdated.addListener((_tabId, changeInfo) => {
  if (changeInfo.status === "complete") {
    void sendBridgeReady();
  }
});

chrome.tabs.onRemoved.addListener(() => {
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

      await chrome.tabs.sendMessage(tab.id, {
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
  }, 800);

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
  const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeAiTab = activeTabs.find((tab) => {
    const source = sourceFromUrl(tab.url);
    return source && (!targetSource || source === targetSource);
  });
  if (activeAiTab?.id) {
    console.info("[AI Bridge] using active AI tab", activeAiTab.id, activeAiTab.url);
    return activeAiTab;
  }

  const allTabs = await chrome.tabs.query({});
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
  const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeSource = sourceFromUrl(activeTabs[0]?.url);
  if (activeSource) {
    return activeSource;
  }

  const allTabs = await chrome.tabs.query({});
  return sourceFromUrl(allTabs.find((tab) => sourceFromUrl(tab.url))?.url) || "none";
}

async function currentBridgeStatus() {
  const [source, sources] = await Promise.all([currentSource(), openSources()]);
  return { source, sources };
}

async function openSources() {
  const tabs = await chrome.tabs.query({});
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
