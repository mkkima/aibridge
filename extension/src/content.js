const CONTENT_SCRIPT_VERSION = "2026.07.16-gemini-auto-research";
const HOSTNAME = window.location.hostname;
const GEMINI_RESEARCH_FINAL_QUIET_MS = 10_000;
let lastGeminiResearchReportText = "";

console.info("[AI Bridge] content script loaded", HOSTNAME, CONTENT_SCRIPT_VERSION);

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
    if (!source) {
      throw new Error("Open Gemini, ChatGPT, or Claude tab");
    }

    console.info("[AI Bridge] running prompt", id, source);
    emitStatus(
      id,
      source,
      "content.received",
      `Content script received prompt command (${CONTENT_SCRIPT_VERSION})`,
    );
    
    if (!context || context.trim() === "") {
      const latestText = getLatestAssistantText(source);
      emitStatus(id, source, "content.done", "Fetched latest text");
      emit({ type: "FINAL", id, source, content: latestText });
      emit({ type: "DONE", id, source });
      return;
    }

    const input = await waitForElement(() => findPromptInput(source), 15_000);
    emitStatus(id, source, "content.input_found", "Prompt input found");
    const baselineInfo = source === "claude" ? captureClaudeBaseline() : undefined;
    const baselineText = getLatestAssistantText(source);

    const insertedLength = insertPrompt(input, context);
    emitStatus(
      id,
      source,
      "content.prompt_inserted",
      `Prompt inserted, input now has ${insertedLength} chars`,
    );
    await delay(350);

    const submitMethod = await submitPrompt(source, input);
    console.info("[AI Bridge] prompt submitted", source, submitMethod);
    emitStatus(id, source, "content.submitted", `Prompt submitted via ${submitMethod}`);
    await streamResponse({ id, source, baselineText, baselineInfo, promptText: context });
    emitStatus(id, source, "content.done", "Response stream finished");
    emit({ type: "DONE", id, source });
  } catch (error) {
    console.error("[AI Bridge] prompt failed", error);
    const message = error instanceof Error ? error.message : String(error);
    emit({
      type: "ERROR",
      id,
      message: `${message} (content ${CONTENT_SCRIPT_VERSION})`,
    });
  }
}

async function streamResponse({ id, source, baselineText, baselineInfo, promptText }) {
  if (source === "claude") {
    await streamClaudeResponse({ id, baselineInfo, promptText });
    return;
  }

  let lastText = "";
  let emittedLength = 0;
  let hasNewResponse = false;
  let lastTextChangedAt = Date.now();
  let latestText = "";
  const startedAt = Date.now();
  let lastWaitLogAt = 0;
  let geminiResearchStarted = false;
  let geminiResearchPlanText = "";
  let geminiResearchClickedAt = 0;
  let geminiResearchClickAttempts = 0;
  let geminiResearchSawActivity = false;
  let geminiResearchSawPostClickText = false;
  
  let lastActivityAt = Date.now();
  const observer = new MutationObserver(() => {
    lastActivityAt = Date.now();
  });
  
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['class', 'style', 'disabled', 'data-is-streaming', 'data-message-author-role']
  });

  try {
    while (true) {
      const currentText = normalizeText(getLatestAssistantText(source));
      if (currentText) {
        latestText = currentText;
      }
      const elapsed = Date.now() - startedAt;

      if (!hasNewResponse) {
        if (!currentText || currentText === normalizeText(baselineText)) {
          if (elapsed - lastWaitLogAt > 5_000) {
            lastWaitLogAt = elapsed;
            const debug = getResponseDebug(source);
            emitStatus(
              id,
              source,
              "content.waiting_response",
              `Waiting for first response text (${Math.round(elapsed / 1000)}s). ${debug}`,
            );
          }
          await delay(250);
          continue;
        }
        hasNewResponse = true;
        lastText = "";
        emittedLength = 0;
        emitStatus(
          id,
          source,
          "content.response_started",
          `Response text appeared: ${currentText.length} chars`,
        );
      }

      if (currentText && currentText !== lastText) {
        let token = "";
        let isReplace = false;

        if (currentText.startsWith(lastText)) {
          token = currentText.slice(lastText.length);
        } else {
          token = currentText;
          isReplace = true;
        }

        lastText = currentText;
        emittedLength = currentText.length;
        lastTextChangedAt = Date.now();

        if (token) {
          if (isReplace) {
            emit({ type: "REPLACE", id, source, content: token });
            emitStatus(
              id,
              source,
              "content.replace",
              `Replaced full text: ${currentText.length} chars`,
            );
          } else {
            emit({ type: "TOKEN", id, source, token });
            emitStatus(
              id,
              source,
              "content.token",
              `Sent token chunk: ${token.length} chars, total ${currentText.length} chars`,
            );
          }
        }

        await delay(250);
        continue;
      }

      if (source === "gemini" && !geminiResearchStarted) {
        const startResearchButton = findGeminiStartResearchButton();
        if (startResearchButton) {
          geminiResearchStarted = true;
          geminiResearchPlanText = currentText;
          geminiResearchClickedAt = Date.now();
          geminiResearchClickAttempts = 1;
          lastActivityAt = geminiResearchClickedAt;
          lastTextChangedAt = geminiResearchClickedAt;

          startResearchButton.scrollIntoView({ block: "center", inline: "nearest" });
          startResearchButton.click();
          emitStatus(
            id,
            source,
            "content.research_started",
            "Gemini research plan detected; clicked Start Research",
          );
          await delay(750);
          continue;
        }
      }

      const generating = isGenerating(source);
      if (source === "gemini" && geminiResearchStarted) {
        const researchRunning = isGeminiResearchRunning();
        const startResearchButton = findGeminiStartResearchButton();
        if (
          startResearchButton &&
          !generating &&
          !researchRunning &&
          Date.now() - geminiResearchClickedAt >= 1_500
        ) {
          if (geminiResearchClickAttempts >= 3) {
            throw new Error("Gemini Start Research button did not activate");
          }
          geminiResearchClickAttempts += 1;
          geminiResearchClickedAt = Date.now();
          lastActivityAt = geminiResearchClickedAt;
          startResearchButton.click();
          emitStatus(
            id,
            source,
            "content.research_start_retry",
            `Retrying Start Research click (${geminiResearchClickAttempts}/3)`,
          );
          await delay(750);
          continue;
        }
        if (
          generating ||
          researchRunning ||
          (Date.now() - geminiResearchClickedAt >= 1_000 && lastActivityAt > geminiResearchClickedAt)
        ) {
          geminiResearchSawActivity = true;
        }
        if (
          currentText &&
          normalizeText(currentText) !== normalizeText(geminiResearchPlanText)
        ) {
          geminiResearchSawPostClickText = true;
        }

        if (!geminiResearchSawActivity || !geminiResearchSawPostClickText || researchRunning) {
          await delay(500);
          continue;
        }
      }

      if (currentText && !generating) {
        const quietForMs = source === "gemini" && geminiResearchStarted
          ? Date.now() - lastTextChangedAt
          : Date.now() - Math.max(lastActivityAt, lastTextChangedAt);
        const requiredQuietMs = source === "claude"
          ? getClaudeRequiredQuietMs()
          : source === "gemini" && geminiResearchStarted
            ? GEMINI_RESEARCH_FINAL_QUIET_MS
            : 1_250;
        if (quietForMs >= requiredQuietMs) {
          emitStatus(id, source, "content.stable", "Response considered stable");
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

function emitFinalIfNeeded(id, source, lastText) {
  const finalText = normalizeText(getLatestAssistantText(source));
  const content = finalText || normalizeText(lastText || "");
  if (!content) {
    return;
  }

  emit({ type: "FINAL", id, source, content });
  emitStatus(
    id,
    source,
    "content.final",
    `Final full text sent: ${content.length} chars`,
  );
}

function currentSource() {
  if (HOSTNAME.includes("gemini.google.com")) {
    return "gemini";
  }
  if (HOSTNAME.includes("chatgpt.com") || HOSTNAME.includes("chat.openai.com")) {
    return "chatgpt";
  }
  if (HOSTNAME.includes("claude.ai")) {
    return "claude";
  }
  return "";
}

function findPromptInput(source) {
  const selectorsBySource = {
    claude: [
      "div.ProseMirror[contenteditable='true']",
      "[contenteditable='true']",
      "textarea",
    ],
    chatgpt: [
      "#prompt-textarea",
      "textarea[data-testid='prompt-textarea']",
      "div[contenteditable='true'][role='textbox']",
      "[contenteditable='true']",
    ],
    gemini: [
      "[contenteditable='true']",
      "rich-textarea [contenteditable='true']",
      ".ql-editor",
      "textarea",
    ],
  };

  return firstVisible(selectorsBySource[source] || selectorsBySource.chatgpt);
}

function insertPrompt(input, promptText) {
  input.click();
  input.focus();

  if ("value" in input) {
    const prototype =
      input.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) {
      setter.call(input, promptText);
    } else {
      input.value = promptText;
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: promptText,
      }),
    );
    return String(input.value || "").length;
  }

  // Try dispatching a paste event first, which works well in background tabs for ProseMirror
  try {
    const dataTransfer = new DataTransfer();
    dataTransfer.setData("text/plain", promptText);
    const pasteEvent = new ClipboardEvent("paste", {
      bubbles: true,
      cancelable: true,
      clipboardData: dataTransfer
    });
    input.dispatchEvent(pasteEvent);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  } catch (e) {
    console.warn("Paste event failed", e);
  }

  if (!normalizeText(input.innerText || input.textContent || "").includes(normalizeText(promptText))) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(input);
    range.deleteContents();
    range.collapse(false);
    selection?.removeAllRanges();
    selection?.addRange(range);
    document.execCommand("insertText", false, promptText);
  }

  if (!normalizeText(input.innerText || input.textContent || "").includes(normalizeText(promptText))) {
    input.textContent = promptText;
  }
  
  input.dispatchEvent(
    new InputEvent("beforeinput", {
      bubbles: true,
      inputType: "insertText",
      data: promptText,
    }),
  );
  input.dispatchEvent(
    new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: promptText,
    }),
  );
  input.dispatchEvent(new Event("change", { bubbles: true }));
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

async function waitForSendButton(source, timeoutMs) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const button = findSendButton(source);
    if (button) {
      return button;
    }
    await delay(100);
  }
  return undefined;
}

function findSendButton(source) {
  const selectorsBySource = {
    claude: [
      "button[aria-label*='send' i]",
      "button[aria-label*='отправ' i]",
      "button[data-testid='send-button']",
      "button[type='submit']",
    ],
    chatgpt: [
      "button[data-testid='send-button']",
      "button[aria-label='Send prompt']",
      "button[aria-label='Отправить запрос']",
      "button[aria-label*='send' i]",
      "button[aria-label*='отправ' i]",
      "button[type='submit']",
    ],
    gemini: [
      "button[aria-label='Send message']",
      "button[aria-label*='Send message']",
      "button[aria-label*='send' i]",
      "button[aria-label*='отправ' i]",
      "button[data-testid='send-button']",
      "button.send-button",
      ".send-button",
      "button:has(mat-icon)",
      "button[type='submit']",
      "button[mattooltip*='send' i]",
      "button[mattooltip*='отправ' i]",
      "div:has(> rich-textarea) ~ div button:last-of-type",
      "div:has(> [contenteditable='true']) button:last-of-type",
    ],
  };

  for (const selector of selectorsBySource[source] || selectorsBySource.chatgpt) {
    let candidates = [];
    try {
      candidates = [...document.querySelectorAll(selector)];
    } catch {
      continue;
    }

    const element = candidates.find((candidate) => {
      if (!(candidate instanceof Element) || !isVisible(candidate)) {
        return false;
      }

      const ariaDisabled = candidate.getAttribute("aria-disabled") === "true";
      const disabledClass = candidate.className && candidate.className.toString().includes("disabled");
      const hasSendIcon = /send|arrow|отправ|submit|задать|создать|run/i.test(
        `${candidate.ariaLabel || ""} ${candidate.title || ""} ${candidate.textContent || ""} ${candidate.innerHTML || ""}`,
      );
      
      const isLastButton = candidate === candidate.parentElement.lastElementChild && candidate.tagName === 'BUTTON';

      return !candidate.disabled && !ariaDisabled && !disabledClass && (source !== "gemini" || hasSendIcon || isLastButton);
    });

    if (element) {
      return { element, selector };
    }
  }

  // Fallback for Gemini and Claude: look for any button near the input that is enabled or is the last button
  if (source === "gemini" || source === "claude") {
    const input = findPromptInput(source);
    if (input) {
      const container = input.closest('fieldset') || input.closest('div:has(button)') || input.parentElement?.parentElement;
      if (container) {
        const buttons = [...container.querySelectorAll('button')].filter(b => isVisible(b) && !b.disabled && b.getAttribute("aria-disabled") !== "true");
        if (buttons.length > 0) {
          // The send button is usually the last button
          return { element: buttons[buttons.length - 1], selector: `${source}-fallback-last-button` };
        }
      }
    }
  }

  return undefined;
}

function findGeminiStartResearchButton() {
  const labelPattern = /^(?:start research|начать исследование)$/i;
  const controls = [...document.querySelectorAll("button, [role='button']")];
  return controls.find((control) => {
    if (!isVisible(control)) {
      return false;
    }
    if (control.disabled || control.getAttribute("aria-disabled") === "true") {
      return false;
    }
    return controlLabels(control).some((label) => labelPattern.test(label));
  });
}

function isGeminiResearchRunning() {
  const stopResearchPattern = /(?:stop|cancel).{0,24}research|(?:останов|прекрат|отмен).{0,24}исслед/i;
  const controls = [...document.querySelectorAll("button, [role='button']")];
  if (
    controls.some((control) => (
      isVisible(control) &&
      controlLabels(control).some((label) => stopResearchPattern.test(label))
    ))
  ) {
    return true;
  }

  return Boolean(firstVisible([
    "model-response [aria-busy='true']",
    "model-response [role='progressbar']",
    "model-response mat-progress-spinner",
    "model-response mat-spinner",
    "model-response [class*='progress-spinner']",
  ]));
}

function controlLabels(control) {
  return [
    control.getAttribute("aria-label"),
    control.getAttribute("title"),
    control.innerText,
    control.textContent,
  ].map((value) => normalizeText(value || "")).filter(Boolean);
}

function dispatchEnter(input) {
  const eventConfig = {
    key: "Enter",
    code: "Enter",
    keyCode: 13,
    which: 13,
    charCode: 13,
    bubbles: true,
    cancelable: true,
    composed: true,
  };
  input.dispatchEvent(new KeyboardEvent("keydown", eventConfig));
  input.dispatchEvent(new KeyboardEvent("keypress", eventConfig));
  input.dispatchEvent(new KeyboardEvent("keyup", eventConfig));
  
  // Also try dispatching a submit event to the closest form if available
  const form = input.closest('form');
  if (form) {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }
}

function getLatestAssistantText(source) {
  if (source === "claude") {
    return getLatestClaudeAssistantText();
  }
  if (source === "gemini") {
    const researchReport = getGeminiResearchReportText();
    if (researchReport) {
      lastGeminiResearchReportText = researchReport;
      return researchReport;
    }
    if (!findPromptInput("gemini") && lastGeminiResearchReportText) {
      return lastGeminiResearchReportText;
    }
  }

  const selectorsBySource = {
    chatgpt: [
      "[data-message-author-role='assistant']",
      "div.agent-turn",
      ".markdown",
    ],
    gemini: [
      "model-response",
      "message-content",
      "[data-test-id='response']",
      "[data-testid='response']",
      ".model-response-text",
      "div[class*='response']",
      ".markdown",
    ],
  };

  const selectors = selectorsBySource[source] || selectorsBySource.chatgpt;
  const candidates = [];

  for (const selector of selectors) {
    let nodes = [];
    try {
      nodes = [...document.querySelectorAll(selector)].filter(isVisible);
    } catch {
      continue;
    }

    for (const node of nodes) {
      // Very strict user message filter
      if (
        node.closest(".font-user-message") || 
        node.closest("[data-message-author-role='user']") ||
        node.hasAttribute("data-is-user") ||
        node.querySelector("[data-message-author-role='user']")
      ) {
        continue;
      }
      
      let textNode = node;
      if (source === "chatgpt") {
        textNode = node.querySelector(".markdown") || node;
      } else if (source === "claude") {
        // Try to dig into the content to avoid wrappers that might include user prompts or "responded" text
        textNode = node.querySelector(".prose") || node.querySelector("div.grid-cols-1") || node;
      }
      
      let text = normalizeText(textNode.innerText || textNode.textContent || "");
      
      // Cleanup for Claude: strip any "user responded: " prefix that leaks from screen readers
      if (source === "claude") {
         const promptMatch = text.match(/^(.{1,100}?)\s+responded:\s*(.*)/i);
         if (promptMatch) {
            text = normalizeText(promptMatch[2]);
         }
      }
      
      if (text) {
        candidates.push({ text, top: node.getBoundingClientRect().top, selector });
      }
    }

    
    if (candidates.length > 0) {
      break;
    }
  }

  if (candidates.length === 0) {
    return "";
  }

  candidates.sort((left, right) => left.top - right.top);
  return candidates[candidates.length - 1].text;
}

function getGeminiResearchReportText() {
  const explicitResearchSelectors = [
    "deep-research-report",
    "research-report",
    "[data-test-id*='research-report' i]",
    "[data-testid*='research-report' i]",
    "[class*='research-report' i]",
    "[class*='deep-research' i]",
  ];
  const genericDocumentSelectors = [
    "article",
    "[role='document']",
    "main",
  ];
  const visiblePromptInput = findPromptInput("gemini");
  const roots = [];

  for (const selector of [...explicitResearchSelectors, ...genericDocumentSelectors]) {
    let nodes = [];
    try {
      nodes = [...document.querySelectorAll(selector)];
    } catch {
      continue;
    }
    for (const node of nodes) {
      if (!roots.includes(node) && isVisible(node)) {
        roots.push(node);
      }
    }
  }

  const candidates = [];
  for (const root of roots) {
    const isExplicitResearchRoot = explicitResearchSelectors.some((selector) => {
      try {
        return root.matches(selector);
      } catch {
        return false;
      }
    });

    // Normal Gemini chat keeps the composer visible. Generic article/main roots
    // are accepted only in the immersive report view where the composer is gone.
    if (visiblePromptInput && !isExplicitResearchRoot) {
      continue;
    }

    const headingCount = root.querySelectorAll("h1, h2, h3, h4").length;
    const paragraphCount = root.querySelectorAll("p, li, pre, blockquote").length;
    const text = extractGeminiResearchReportText(root);
    if (text.length < 600 || headingCount < 1 || paragraphCount < 3) {
      continue;
    }

    const semanticBonus = isExplicitResearchRoot
      ? 5_000
      : root.matches("article, [role='document']")
        ? 2_000
        : 0;
    candidates.push({
      text,
      score: semanticBonus + text.length + headingCount * 300 + paragraphCount * 25,
    });
  }

  candidates.sort((left, right) => right.score - left.score);
  return candidates[0]?.text || "";
}

function extractGeminiResearchReportText(root) {
  const blockSelectors = "h1, h2, h3, h4, h5, h6, p, li, pre, blockquote, td, th";
  const blocks = getTopLevelElements([...root.querySelectorAll(blockSelectors)].filter((node) => (
    isVisible(node) &&
    !isGeminiResearchNoiseNode(node) &&
    !node.querySelector(blockSelectors)
  ))).sort(compareDocumentOrder);

  const texts = blocks.map((node) => {
    const text = cleanupGeminiResearchText(node.innerText || node.textContent || "");
    if (!text) {
      return "";
    }
    if (/^H[1-6]$/.test(node.tagName)) {
      const level = Number(node.tagName.slice(1));
      return `${"#".repeat(level)} ${text}`;
    }
    if (node.tagName === "LI") {
      return `- ${text}`;
    }
    if (node.tagName === "PRE") {
      return `\`\`\`\n${text}\n\`\`\``;
    }
    return text;
  }).filter(Boolean);

  if (texts.length > 0) {
    return dedupeClaudeTextBlocks(texts).join("\n\n");
  }

  return cleanupGeminiResearchText(root.innerText || root.textContent || "");
}

function isGeminiResearchNoiseNode(node) {
  return Boolean(
    node.closest("nav, button, [role='button'], [role='toolbar'], [role='menu']") ||
    node.closest("form, fieldset, textarea, input, [contenteditable='true']") ||
    node.closest("[aria-hidden='true'], [hidden], .sr-only"),
  );
}

function cleanupGeminiResearchText(text) {
  return normalizeText(
    text
      .replace(/\r\n?/g, "\n")
      .split(/\n+/)
      .map((line) => line.trim())
      .filter((line) => (
        line &&
        !/^(?:Создать|Create|Поделиться|Share|Экспортировать|Export|Ещё|More|Содержание|Contents)$/i.test(line)
      ))
      .join("\n"),
  );
}

async function streamClaudeResponse({ id, baselineInfo, promptText }) {
  const baseline = baselineInfo || captureClaudeBaseline();
  const startedAt = Date.now();
  let userNode = undefined;
  let turnRoot = undefined;
  let observedRoot = undefined;
  let observer = undefined;
  let lastDomChangedAt = Date.now();
  let lastTextChangedAt = Date.now();
  let lastText = "";
  let lastEmittedText = "";
  let lastStatusAt = 0;

  const attachObserver = (root) => {
    if (!root || root === observedRoot) {
      return;
    }
    observer?.disconnect();
    observedRoot = root;
    lastDomChangedAt = Date.now();
    observer = new MutationObserver(() => {
      lastDomChangedAt = Date.now();
    });
    observer.observe(root.parentElement || root, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["class", "style", "data-is-streaming", "aria-hidden", "hidden"],
    });
  };

  try {
    while (true) {
      if (!userNode) {
        userNode = findClaudeUserMessageAfter(baseline.lastUserNode, promptText);
      }
      const anchorNode = userNode || baseline.lastTurnRoot || baseline.lastUserNode;
      turnRoot = anchorNode ? findClaudeTurnRootAfter(anchorNode) : undefined;
      attachObserver(turnRoot || document.body);

      const currentText = turnRoot ? extractClaudeTurnText(turnRoot) : "";
      const now = Date.now();
      if (currentText && currentText !== lastText) {
        lastText = currentText;
        lastTextChangedAt = now;
        if (currentText !== lastEmittedText) {
          emit({ type: "REPLACE", id, source: "claude", content: currentText });
          emitStatus(id, "claude", "content.snapshot", `Claude snapshot: ${currentText.length} chars`);
          lastEmittedText = currentText;
        }
      }

      const streamingState = getClaudeStreamingState(anchorNode);
      const generating = isClaudeGenerating(anchorNode);
      const hasFinalActions = turnRoot ? hasClaudeFinalActions(turnRoot) : false;
      const quietForMs = now - Math.max(lastTextChangedAt, lastDomChangedAt);
      const hasPositiveDoneSignal = streamingState === "false" || hasFinalActions;
      const shouldFinish = Boolean(lastText) && !generating && (
        (hasPositiveDoneSignal && quietForMs >= 1_500) ||
        quietForMs >= 18_000
      );

      if (now - lastStatusAt > 5_000) {
        lastStatusAt = now;
        emitStatus(
          id,
          "claude",
          "content.claude_poll",
          `text=${lastText.length}, streaming=${streamingState}, generating=${generating}, actions=${hasFinalActions}, quiet=${quietForMs}, version=${CONTENT_SCRIPT_VERSION}`,
        );
      }

      if (shouldFinish) {
        await delay(350);
        const finalRoot = findClaudeTurnRootAfter(anchorNode) || turnRoot;
        const finalText = finalRoot ? extractClaudeTurnText(finalRoot) : lastText;
        const content = finalText && finalText.length >= lastText.length ? finalText : lastText;
        emit({ type: "FINAL", id, source: "claude", content });
        emitStatus(id, "claude", "content.final", `Claude final snapshot: ${content.length} chars`);
        return;
      }

      await delay(500);
    }
  } finally {
    observer?.disconnect();
  }
}

function captureClaudeBaseline() {
  const lastUserNode = getLastClaudeUserMessageNode();
  const lastTurnRoot = findClaudeTurnRootAfter(lastUserNode);
  return {
    lastUserNode,
    lastTurnRoot,
    lastText: lastTurnRoot ? extractClaudeTurnText(lastTurnRoot) : "",
  };
}

function getLatestClaudeAssistantText() {
  const afterLastUserRoot = findClaudeTurnRootAfter(getLastClaudeUserMessageNode());
  const fallbackRoots = getTopLevelElements(getClaudeAssistantRoots().filter((node) => (
    !isUserMessageNode(node) &&
    !isPromptComposerNode(node)
  ))).sort(compareDocumentOrder);
  const root = afterLastUserRoot || fallbackRoots[fallbackRoots.length - 1];
  return root ? extractClaudeTurnText(root) : "";
}

function findClaudeUserMessageAfter(previousUserNode, promptText) {
  const normalizedPrompt = normalizeText(promptText || "");
  const userNodes = getClaudeUserMessageNodes().filter((node) => (
    !previousUserNode || isAfterNode(node, previousUserNode)
  ));
  if (userNodes.length === 0) {
    return undefined;
  }
  if (!normalizedPrompt) {
    return userNodes[userNodes.length - 1];
  }

  return userNodes.find((node) => {
    const text = normalizeText(node.innerText || node.textContent || "");
    return text.includes(normalizedPrompt) || normalizedPrompt.includes(text);
  }) || userNodes[userNodes.length - 1];
}

function findClaudeTurnRootAfter(userNode) {
  const roots = getTopLevelElements(getClaudeAssistantRoots().filter((node) => (
    (!userNode || isAfterNode(node, userNode)) &&
    !isUserMessageNode(node) &&
    !isPromptComposerNode(node)
  ))).sort(compareDocumentOrder);

  if (roots.length === 0) {
    return undefined;
  }

  const streamingRoots = roots.filter((node) => (
    node.hasAttribute("data-is-streaming") || node.querySelector("[data-is-streaming]")
  ));
  if (streamingRoots.length > 0) {
    return streamingRoots[streamingRoots.length - 1];
  }

  return roots[roots.length - 1];
}

function getClaudeAssistantRoots() {
  const selectors = [
    "[data-is-streaming]",
    "[data-message-author-role='assistant']",
    "[data-testid='assistant-message']",
    ".font-claude-message",
    "[class*='font-claude-message']",
    ".prose",
  ];
  const roots = new Set();

  for (const selector of selectors) {
    let nodes = [];
    try {
      nodes = [...document.querySelectorAll(selector)];
    } catch {
      continue;
    }
    for (const node of nodes) {
      if (!isVisible(node) || isUserMessageNode(node) || isPromptComposerNode(node)) {
        continue;
      }
      const root =
        node.closest("[data-is-streaming]") ||
        node.closest("[data-message-author-role='assistant']") ||
        node.closest("[data-testid='assistant-message']") ||
        node.closest(".font-claude-message") ||
        node.closest("[class*='font-claude-message']") ||
        node;
      if (root && isVisible(root) && !isUserMessageNode(root) && !isPromptComposerNode(root)) {
        roots.add(root);
      }
    }
  }

  return [...roots];
}

function extractClaudeTurnText(root) {
  if (!root) {
    return "";
  }

  const blockSelectors = "p, li, pre, blockquote, h1, h2, h3, h4, h5, h6, td, th";
  const blocks = getTopLevelElements([...root.querySelectorAll(blockSelectors)].filter((node) => (
    isVisible(node) &&
    !isClaudeNoiseNode(node) &&
    !node.querySelector(blockSelectors)
  ))).sort(compareDocumentOrder);

  const texts = blocks
    .map((node) => cleanupClaudeText(node.innerText || node.textContent || ""))
    .filter(Boolean);

  if (texts.length > 0) {
    return dedupeClaudeTextBlocks(texts).join("\n");
  }

  const fallback = cleanupClaudeText(root.innerText || root.textContent || "");
  return stripClaudeUiNoise(fallback);
}

function dedupeClaudeTextBlocks(blocks) {
  const result = [];
  for (const block of blocks) {
    const text = normalizeText(block);
    if (!text) {
      continue;
    }
    const previous = result[result.length - 1] || "";
    if (previous && (previous === text || text.startsWith(`${previous} `) || text.startsWith(previous + "\n"))) {
      result[result.length - 1] = text;
      continue;
    }
    if (previous && previous.startsWith(`${text} `)) {
      continue;
    }
    result.push(text);
  }
  return result;
}

function cleanupClaudeText(text) {
  let cleaned = normalizeText(text);
  const respondedMatch = cleaned.match(/^.{1,160}?\s+responded:\s*([\s\S]*)$/i);
  if (respondedMatch) {
    cleaned = normalizeText(respondedMatch[1]);
  }
  return stripClaudeUiNoise(cleaned);
}

function stripClaudeUiNoise(text) {
  return normalizeText(
    text
      .split(/\n+/)
      .map((line) => line.trim())
      .filter((line) => (
        line &&
        !/^Claude .* currently unavailable\.?$/i.test(line) &&
        !/^Write a message/i.test(line) &&
        !/^Incognito chats/i.test(line) &&
        !/^(Copy|Retry|Regenerate|Копировать|Повторить)$/i.test(line)
      ))
      .join("\n"),
  );
}

function getClaudeUserMessageNodes() {
  return [
    ...document.querySelectorAll("[data-message-author-role='user'], [data-testid='user-message'], .font-user-message"),
  ].filter((node) => isVisible(node) && !isPromptComposerNode(node)).sort(compareDocumentOrder);
}

function getLastClaudeUserMessageNode() {
  const userNodes = getClaudeUserMessageNodes();
  return userNodes[userNodes.length - 1];
}

function getTopLevelElements(nodes) {
  return nodes.filter((node, index) => (
    isVisible(node) &&
    nodes.indexOf(node) === index &&
    !nodes.some((other) => other !== node && other.contains(node))
  ));
}

function isAfterNode(node, previousNode) {
  return Boolean(previousNode.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
}

function compareDocumentOrder(left, right) {
  if (left === right) {
    return 0;
  }
  return left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
}

function getClaudeStreamingState(userNode) {
  const roots = getClaudeAssistantRoots().filter((node) => (
    (!userNode || isAfterNode(node, userNode)) &&
    !isPromptComposerNode(node) &&
    !isUserMessageNode(node)
  ));
  const streamingNodes = roots.flatMap((root) => (
    root.hasAttribute("data-is-streaming")
      ? [root]
      : [...root.querySelectorAll("[data-is-streaming]")]
  ));

  if (streamingNodes.some((node) => node.getAttribute("data-is-streaming") === "true")) {
    return "true";
  }
  if (streamingNodes.some((node) => node.getAttribute("data-is-streaming") === "false")) {
    return "false";
  }

  return "unknown";
}

function getClaudeRequiredQuietMs() {
  const streamingState = getClaudeStreamingState(getLastClaudeUserMessageNode());
  return streamingState === "false" ? 1_500 : 30_000;
}

function isClaudeGenerating(userNode) {
  return getClaudeStreamingState(userNode) === "true" || Boolean(firstVisible([
    "button[aria-label*='stop' i]",
    "button[aria-label*='останов' i]",
    "button[data-testid='stop-button']",
    "button[data-testid*='stop' i]",
  ], (node) => (
    (!userNode || isAfterNode(node, userNode)) &&
    !isPromptComposerNode(node) &&
    !isUserMessageNode(node)
  )));
}

function hasClaudeFinalActions(root) {
  if (!root) {
    return false;
  }
  try {
    root.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
    root.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true }));
  } catch {
    // Hover-gated controls are optional; streaming state remains the primary signal.
  }

  const selectors = [
    "button[aria-label*='copy' i]",
    "button[aria-label*='копир' i]",
    "button[aria-label*='retry' i]",
    "button[aria-label*='regenerate' i]",
    "button[aria-label*='повтор' i]",
    "button[aria-label*='read aloud' i]",
  ];

  return selectors.some((selector) => {
    try {
      return [...root.querySelectorAll(selector)].some((node) => (
        isVisible(node) &&
        !isPromptComposerNode(node) &&
        !isUserMessageNode(node)
      ));
    } catch {
      return false;
    }
  });
}

function isClaudeNoiseNode(node) {
  return Boolean(
    node.closest("button, svg, [role='button'], [role='menu'], [role='toolbar']") ||
    node.closest("[aria-hidden='true'], [hidden], .sr-only") ||
    node.closest("textarea, input, form, fieldset, [contenteditable='true'], .ProseMirror") ||
    node.closest("[data-testid*='composer' i], [data-testid*='action' i], [data-testid*='model' i]"),
  );
}

function getResponseDebug(source) {
  if (source === "claude") {
    const selectors = [
      "[data-message-author-role='assistant']",
      "[data-testid='assistant-message']",
      "[data-is-streaming]",
      ".font-claude-message",
      ".prose",
      "button[aria-label*='stop' i]",
    ];
    const parts = selectors.map((selector) => {
      try {
        return `${selector}=${document.querySelectorAll(selector).length}`;
      } catch {
        return `${selector}=ERR`;
      }
    });
    return `Claude response selectors: ${parts.join(", ")}; streaming=${getClaudeStreamingState()}; version=${CONTENT_SCRIPT_VERSION}`;
  }

  if (source !== "gemini") {
    return "";
  }

  const selectors = [
    "message-content",
    "model-response",
    "[data-test-id='response']",
    "[data-testid='response']",
    ".model-response-text",
    ".markdown",
    "div[class*='response']",
  ];
  const parts = selectors.map((selector) => {
    try {
      return `${selector}=${document.querySelectorAll(selector).length}`;
    } catch {
      return `${selector}=ERR`;
    }
  });
  return `Gemini response selectors: ${parts.join(", ")}`;
}

function isGenerating(source) {
  if (source === "claude") {
    return getClaudeStreamingState() === "true" || Boolean(firstVisible([
      "button[aria-label*='stop' i]",
      "button[aria-label*='останов' i]",
      "button[data-testid='stop-button']",
      "button[data-testid*='stop' i]",
    ], (node) => !isPromptComposerNode(node) && !isUserMessageNode(node)));
  }

  const selectorsBySource = {
    chatgpt: [
      "button[data-testid='stop-button']",
      "button[aria-label='Stop generating']",
      "button[aria-label='Остановить генерацию']",
      "button[aria-label*='Stop']",
      "button[aria-label*='Останов']",
    ],
    gemini: [
      "button[aria-label*='Stop']",
      "button[aria-label*='Останов']",
      ".stop-button",
    ],
  };

  return Boolean(firstVisible(selectorsBySource[source] || selectorsBySource.chatgpt));
}

async function waitForElement(resolveElement, timeoutMs) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const element = resolveElement();
    if (element) {
      return element;
    }
    await delay(100);
  }
  throw new Error("Cannot find prompt input on the current AI page");
}

function firstVisible(selectors, predicate = () => true) {
  for (const selector of selectors) {
    const element = [...document.querySelectorAll(selector)].find(
      (candidate) => isVisible(candidate) && predicate(candidate),
    );
    if (element) {
      return element;
    }
  }
  return undefined;
}

function isVisible(element) {
  if (!(element instanceof Element)) {
    return false;
  }

  if (element.hidden || element.getAttribute("aria-hidden") === "true" || !element.isConnected) {
    return false;
  }

  const style = window.getComputedStyle(element);
  if (style.visibility === "hidden" || style.display === "none" || style.opacity === "0") {
    return false;
  }

  const rects = element.getClientRects();
  return rects.length > 0 || Boolean((element.innerText || element.textContent || "").trim());
}

function isPromptComposerNode(element) {
  return Boolean(
    element.closest("form") ||
    element.closest("fieldset") ||
    element.closest("[contenteditable='true']") ||
    element.matches("textarea, input, [contenteditable='true'], .ProseMirror"),
  );
}

function isUserMessageNode(element) {
  return Boolean(
    element.closest(".font-user-message") ||
    element.closest("[data-message-author-role='user']") ||
    element.closest("[data-testid='user-message']") ||
    element.hasAttribute("data-is-user") ||
    element.querySelector("[data-message-author-role='user']"),
  );
}

function normalizeText(text) {
  return (text || "").replace(/\u00a0/g, " ").trim();
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function emit(payload) {
  chrome.runtime.sendMessage({
    type: "AIBRIDGE_COMPLETION_EVENT",
    payload,
  });
}

function emitStatus(id, source, stage, message) {
  emit({
    type: "STATUS",
    id,
    source,
    stage,
    message,
  });
}
