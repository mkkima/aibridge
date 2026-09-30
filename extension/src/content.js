const CONTENT_SCRIPT_VERSION = "2026.08.08-strict-cli-chatgpt-single-insert";
const HOSTNAME = window.location.hostname;
const GEMINI_RESEARCH_FINAL_QUIET_MS = 10_000;
const CLAUDE_DOCUMENT_FETCH_TIMEOUT_MS = 10_000;
const CLAUDE_DOCUMENT_MAX_BYTES = 4 * 1024 * 1024;
const CLAUDE_DOCUMENT_TOTAL_MAX_BYTES = 6 * 1024 * 1024;
const CHATGPT_FILE_FETCH_TIMEOUT_MS = 10_000;
const CHATGPT_FILE_MAX_BYTES = 4 * 1024 * 1024;
const CHATGPT_FILE_TOTAL_MAX_BYTES = 6 * 1024 * 1024;
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
      const latestText = source === "claude"
        ? await getLatestClaudeAssistantTextWithDocuments()
        : source === "chatgpt"
          ? await getLatestChatGptAssistantTextWithFiles()
          : getLatestAssistantText(source);
      emitStatus(id, source, "content.done", "Fetched latest text");
      emit({ type: "FINAL", id, source, content: latestText });
      emit({ type: "DONE", id, source });
      return;
    }

    if (source === "gemini") {
      await prepareGeminiAfterOpenResearch(id, source);
    }

    const input = await waitForElement(() => findPromptInput(source), 15_000);
    emitStatus(id, source, "content.input_found", "Prompt input found");
    const baselineInfo = source === "claude" ? captureClaudeBaseline() : undefined;
    const baselineText = getLatestAssistantText(source);

    const insertedLength = insertPrompt(input, context, source);
    emitStatus(
      id,
      source,
      "content.prompt_inserted",
      `Prompt inserted, input now has ${insertedLength} chars`,
    );
    await delay(350);

    if (source === "chatgpt") {
      const expandedPastedText = await expandChatGptPastedTextIfNeeded(input, context);
      if (expandedPastedText) {
        emitStatus(
          id,
          source,
          "content.pasted_text_expanded",
          "Clicked Show in text field for the pasted ChatGPT prompt",
        );
      }
    }

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

function insertPrompt(input, promptText, source) {
  input.click();
  input.focus();

  if (source === "chatgpt") {
    return insertChatGptPrompt(input, promptText);
  }

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

function insertChatGptPrompt(input, promptText) {
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
    input.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        composed: true,
        inputType: "insertText",
        data: promptText,
      }),
    );
    return String(input.value || "").length;
  }

  const selection = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(input);
  range.deleteContents();
  range.collapse(false);
  selection?.removeAllRanges();
  selection?.addRange(range);

  // ChatGPT handles execCommand itself and emits the corresponding input
  // notification. Sending a synthetic paste/beforeinput as well causes the
  // same prompt to be accepted twice by some ProseMirror builds.
  document.execCommand("insertText", false, promptText);

  const expectedText = normalizeText(promptText);
  let actualText = normalizeText(input.innerText || input.textContent || "");
  if (actualText !== expectedText) {
    range.selectNodeContents(input);
    range.deleteContents();
    input.textContent = promptText;
    input.dispatchEvent(
      new InputEvent("input", {
        bubbles: true,
        composed: true,
        inputType: "insertText",
        data: promptText,
      }),
    );
    actualText = normalizeText(input.innerText || input.textContent || "");
  }

  input.dispatchEvent(new Event("change", { bubbles: true }));
  return actualText.length;
}

async function expandChatGptPastedTextIfNeeded(input, promptText) {
  // ChatGPT turns large pasted prompts into a compact attachment card. The card
  // must be expanded back into the composer before Send is pressed, otherwise
  // the prompt can remain staged as a pasted-text attachment.
  if (promptText.length < 2_000) {
    return false;
  }

  const deadline = Date.now() + 3_000;
  while (Date.now() < deadline) {
    const control = findChatGptShowInTextFieldControl(input);
    if (control) {
      control.scrollIntoView({ block: "nearest", inline: "nearest" });
      control.click();
      await delay(300);
      return true;
    }
    await delay(100);
  }
  return false;
}

function findChatGptShowInTextFieldControl(input) {
  const labelPattern = /^(?:show in (?:the )?text field|show in composer|показать в текстовом поле|показать в поле ввода)$/i;
  const composer = input.closest("form") || input.parentElement?.parentElement || document;
  const roots = composer === document ? [document] : [composer, document];

  for (const root of roots) {
    const controls = root.querySelectorAll(
      "button, [role='button'], a, [tabindex='0']",
    );
    for (const control of controls) {
      if (!isVisible(control) || control.getAttribute("aria-disabled") === "true") {
        continue;
      }
      if (controlLabels(control).some((label) => labelPattern.test(label))) {
        return control;
      }
    }

    // In some ChatGPT builds the visible label is a nested span without button
    // semantics. Click its nearest interactive ancestor.
    for (const label of root.querySelectorAll("span, div, p")) {
      if (!isVisible(label) || !labelPattern.test(normalizeText(label.innerText || label.textContent || ""))) {
        continue;
      }
      return label.closest("button, [role='button'], a, [tabindex]") || label;
    }
  }
  return undefined;
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

async function prepareGeminiAfterOpenResearch(id, source) {
  if (!isGeminiImmersiveResearchOpen()) {
    return;
  }

  emitStatus(
    id,
    source,
    "content.research_report_open",
    "Open Gemini research report detected",
  );

  const closeButton = await waitForGeminiControl(
    findGeminiResearchCloseButton,
    5_000,
    "Cannot find Close on the open Gemini research report",
  );
  clickControl(closeButton);

  const returnedToChat = () => Boolean(
    findPromptInput("gemini") || findGeminiNewChatButton(),
  );
  if (!(await waitForGeminiStateOptional(returnedToChat, 2_500))) {
    // The unlabeled report icon sometimes ignores synthetic clicks. Opening a
    // report adds a Gemini history entry, so Back is equivalent to Close.
    window.history.back();
    await waitForGeminiState(
      returnedToChat,
      10_000,
      "Gemini research report did not close",
    );
  }
  emitStatus(id, source, "content.research_report_closed", "Closed Gemini research report");

  const newChatButton = await waitForGeminiControl(
    findGeminiNewChatButton,
    10_000,
    "Cannot find New chat in Gemini",
  );
  clickControl(newChatButton);
  await delay(500);

  const input = await waitForElement(() => findPromptInput("gemini"), 15_000);
  lastGeminiResearchReportText = "";
  emitStatus(id, source, "content.gemini_new_chat", "Opened a new Gemini chat");

  const toolsButton = await waitForGeminiControl(
    () => findGeminiToolsButton(input),
    10_000,
    "Cannot find the + tools button in the Gemini prompt field",
  );
  clickControl(toolsButton);

  const deepResearchButton = await waitForGeminiControl(
    findGeminiDeepResearchButton,
    5_000,
    "Cannot find Deep Research in the Gemini tools menu",
  );
  clickControl(deepResearchButton);
  await waitForGeminiState(
    () => !isVisible(deepResearchButton) || isGeminiDeepResearchSelected(),
    5_000,
    "Gemini did not enable Deep Research",
  );
  emitStatus(id, source, "content.deep_research_selected", "Enabled Gemini Deep Research");
}

function isGeminiImmersiveResearchOpen() {
  if (findPromptInput("gemini")) {
    return false;
  }

  return Boolean(getGeminiResearchReportText() || findGeminiResearchCloseButton());
}

function findGeminiResearchCloseButton() {
  const closePattern = /^(?:close|back|close (?:report|research)|закрыть|назад|закрыть (?:отчёт|отчет|исследование))$/i;
  const semanticMatch = bestGeminiControl(
    [...document.querySelectorAll("button, [role='button'], a")].filter((control) => {
      if (!isUsableControl(control)) {
        return false;
      }
      return controlLabels(control).some((label) => closePattern.test(label)) ||
        controlIconLabels(control).some((label) => /^(?:arrow_back|close)$/i.test(label));
    }),
    (control) => {
      const rect = control.getBoundingClientRect();
      return (rect.top < 140 ? 4 : 0) + (rect.left < 180 ? 4 : 0);
    },
  );

  if (semanticMatch) {
    return semanticMatch;
  }

  // Some Gemini report builds expose the top-left back button as an unlabeled
  // icon. In the immersive report there is no composer, so its screen position
  // is the most stable fallback available.
  const topLeftIconControls = [...document.querySelectorAll("button, [role='button'], a")]
    .filter((control) => {
      if (!isUsableControl(control)) {
        return false;
      }
      const rect = control.getBoundingClientRect();
      return rect.top >= 0 && rect.top <= 110 &&
        rect.left >= 0 && rect.left <= 130 &&
        rect.width >= 24 && rect.width <= 96 &&
        rect.height >= 24 && rect.height <= 96;
    });

  return bestGeminiControl(topLeftIconControls, (control) => {
    const rect = control.getBoundingClientRect();
    const hasIcon = Boolean(control.querySelector("svg, mat-icon, [data-icon-name], [fonticon]"));
    return (hasIcon ? 8 : 0) - ((rect.left + rect.top) / 100);
  });
}

function findGeminiNewChatButton() {
  const newChatPattern = /^(?:new chat|start new chat|новый чат|начать новый чат|создать (?:новый )?чат)(?:\s|$)/i;
  return bestGeminiControl(
    [...document.querySelectorAll("button, [role='button'], a")].filter((control) => (
      isUsableControl(control) &&
      controlLabels(control).some((label) => newChatPattern.test(label))
    )),
    (control) => {
      const rect = control.getBoundingClientRect();
      return (rect.top < 180 ? 3 : 0) + (control.tagName === "BUTTON" ? 1 : 0);
    },
  );
}

function findGeminiToolsButton(input) {
  const composer = findGeminiComposerContainer(input);
  if (!composer) {
    return undefined;
  }

  const inputRect = input.getBoundingClientRect();
  const labelPattern = /(?:add|plus|tools?|upload|attach|file|добав|плюс|инструмент|загруз|прикреп|файл)/i;
  const candidates = [...composer.querySelectorAll("button, [role='button']")].filter((control) => {
    if (!isUsableControl(control)) {
      return false;
    }
    const labels = [...controlLabels(control), ...controlIconLabels(control)];
    const hasToolsLabel = labels.some((label) => labelPattern.test(label));
    const hasPlusIcon = labels.some((label) => /^(?:add|add_circle|plus)$/i.test(label));
    return hasToolsLabel || hasPlusIcon;
  });

  return bestGeminiControl(candidates, (control) => {
    const rect = control.getBoundingClientRect();
    const labels = controlIconLabels(control);
    return (rect.left <= inputRect.left + 80 ? 5 : 0) +
      (labels.some((label) => /^(?:add|add_circle|plus)$/i.test(label)) ? 4 : 0);
  });
}

function findGeminiComposerContainer(input) {
  const knownContainer = input.closest(
    "form, fieldset, .input-area-container, .text-input-field, .input-area, [class*='input-area']",
  );
  if (knownContainer) {
    return knownContainer;
  }

  let current = input.parentElement;
  for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
    if (current.querySelectorAll("button, [role='button']").length >= 2) {
      return current;
    }
  }
  return input.parentElement;
}

function findGeminiDeepResearchButton() {
  const controls = [...document.querySelectorAll(
    "[role='menuitem'], [role='option'], button, [role='button'], mat-list-item, [class*='menu-item'], [class*='toolbox-item']",
  )].filter((control) => {
    if (!isUsableControl(control)) {
      return false;
    }
    return controlLabels(control).some((label) => (
      /^(?:deep research|глубокое исследование|углубл[её]нное исследование)(?:\s|$)/i.test(label)
    ));
  });

  return bestGeminiControl(controls, (control) => {
    const role = control.getAttribute("role") || "";
    const text = normalizeText(control.innerText || control.textContent || "");
    const isNativeButton = control.tagName === "BUTTON" || control.tagName === "A";
    return (isNativeButton ? 10 : 0) +
      (/menuitem|option|button/i.test(role) ? 6 : 0) +
      (/^(?:deep research|глубокое исследование|углубл[её]нное исследование)(?:\s|$)/i.test(text) ? 3 : 0);
  });
}

function isGeminiDeepResearchSelected() {
  const input = findPromptInput("gemini");
  if (!input) {
    return false;
  }
  const composer = findGeminiComposerContainer(input);
  if (!composer) {
    return false;
  }

  return [...composer.querySelectorAll("button, [role='button'], [role='option'], [class*='chip']")]
    .some((control) => (
      isVisible(control) &&
      controlLabels(control).some((label) => (
        /deep research|глубокое исследование|углубл[её]нное исследование/i.test(label)
      )) &&
      (
        control.getAttribute("aria-pressed") === "true" ||
        control.getAttribute("aria-selected") === "true" ||
        control.getAttribute("aria-checked") === "true" ||
        /selected|active/i.test(String(control.className || "")) ||
        !control.closest("[role='menu'], [role='listbox'], .mat-mdc-menu-panel")
      )
    ));
}

function controlIconLabels(control) {
  const iconNodes = control.querySelectorAll(
    "mat-icon, [data-icon-name], [data-mat-icon-name], [fonticon], svg, use",
  );
  return [...iconNodes].flatMap((icon) => [
    icon.getAttribute("data-icon-name"),
    icon.getAttribute("data-mat-icon-name"),
    icon.getAttribute("fonticon"),
    icon.getAttribute("href"),
    icon.getAttribute("xlink:href"),
    icon.getAttribute("aria-label"),
    icon.textContent,
  ]).map((value) => normalizeText(value || "")).filter(Boolean);
}

function isUsableControl(control) {
  if (!isVisible(control)) {
    return false;
  }
  const rect = control.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 &&
    rect.bottom > 0 && rect.right > 0 &&
    rect.top < window.innerHeight && rect.left < window.innerWidth &&
    !control.disabled &&
    control.getAttribute("aria-disabled") !== "true";
}

function bestGeminiControl(controls, scoreExtra = () => 0) {
  return controls
    .map((control) => ({
      control,
      score: scoreExtra(control) - control.querySelectorAll("button, [role='button']").length,
    }))
    .sort((left, right) => right.score - left.score)[0]?.control;
}

function clickControl(control) {
  control.scrollIntoView({ block: "center", inline: "nearest" });
  const rect = control.getBoundingClientRect();
  const centerX = Math.max(0, Math.min(window.innerWidth - 1, rect.left + (rect.width / 2)));
  const centerY = Math.max(0, Math.min(window.innerHeight - 1, rect.top + (rect.height / 2)));
  const hit = document.elementFromPoint(centerX, centerY);
  const hitBelongsToControl = hit && (control.contains(hit) || hit.contains(control));
  const eventTarget = hitBelongsToControl ? hit : control;

  eventTarget.dispatchEvent(new PointerEvent("pointerdown", {
    bubbles: true,
    cancelable: true,
    composed: true,
    pointerId: 1,
    pointerType: "mouse",
    isPrimary: true,
    clientX: centerX,
    clientY: centerY,
  }));
  eventTarget.dispatchEvent(new MouseEvent("mousedown", {
    bubbles: true,
    cancelable: true,
    composed: true,
    clientX: centerX,
    clientY: centerY,
  }));
  eventTarget.dispatchEvent(new PointerEvent("pointerup", {
    bubbles: true,
    cancelable: true,
    composed: true,
    pointerId: 1,
    pointerType: "mouse",
    isPrimary: true,
    clientX: centerX,
    clientY: centerY,
  }));
  eventTarget.dispatchEvent(new MouseEvent("mouseup", {
    bubbles: true,
    cancelable: true,
    composed: true,
    clientX: centerX,
    clientY: centerY,
  }));

  const interactiveTarget = eventTarget.closest(
    "button, [role='menuitem'], [role='option'], [role='button'], a, mat-list-item",
  );
  if (interactiveTarget && (control.contains(interactiveTarget) || interactiveTarget.contains(control))) {
    interactiveTarget.click();
  } else {
    control.click();
  }
}

async function waitForGeminiControl(resolveControl, timeoutMs, errorMessage) {
  let result;
  await waitForGeminiState(() => {
    result = resolveControl();
    return Boolean(result);
  }, timeoutMs, errorMessage);
  return result;
}

async function waitForGeminiState(predicate, timeoutMs, errorMessage) {
  if (await waitForGeminiStateOptional(predicate, timeoutMs)) {
    return;
  }
  throw new Error(errorMessage);
}

async function waitForGeminiStateOptional(predicate, timeoutMs) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (predicate()) {
      return true;
    }
    await delay(100);
  }
  return false;
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
      
      let text = extractRichResponseMarkdown(textNode, source);
      if (!text) {
        text = normalizeText(textNode.innerText || textNode.textContent || "");
      }
      
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

async function getLatestChatGptAssistantTextWithFiles() {
  const root = getLatestChatGptAssistantRoot();
  if (!root) {
    return "";
  }

  const textRoot = root.querySelector(".markdown") || root;
  const responseText =
    extractRichResponseMarkdown(textRoot, "chatgpt") ||
    normalizeText(textRoot.innerText || textRoot.textContent || "");
  const cards = collectChatGptTextFileCards(root);
  if (cards.length === 0) {
    return responseText;
  }

  let auth = undefined;
  const resolvedFiles = [];
  const unresolvedCards = [];
  let totalBytes = 0;

  for (const card of cards) {
    if (!card.url || card.url.startsWith("sandbox:")) {
      unresolvedCards.push(card);
      continue;
    }

    try {
      auth ||= await getChatGptWebAuth();
      const content = await fetchChatGptTextFile(card, auth);
      const byteLength = utf8ByteLength(content);
      if (totalBytes + byteLength > CHATGPT_FILE_TOTAL_MAX_BYTES) {
        throw new Error("combined file size exceeds the bridge limit");
      }
      resolvedFiles.push({ ...card, content });
      totalBytes += byteLength;
    } catch (error) {
      console.info(
        "[AI Bridge] ChatGPT file link was not readable; trying conversation data",
        card.fileName,
        error instanceof Error ? error.message : String(error),
      );
      unresolvedCards.push(card);
    }
  }

  if (unresolvedCards.length === 0) {
    return appendChatGptFiles(responseText, resolvedFiles, []);
  }

  try {
    auth ||= await getChatGptWebAuth();
    const references = await fetchCurrentChatGptTurnFileReferences(auth);
    const usedReferenceIndexes = new Set();
    const stillUnresolved = [];

    for (const card of unresolvedCards) {
      const matchIndex = findChatGptFileReferenceMatch(
        card,
        references,
        usedReferenceIndexes,
        unresolvedCards.length,
      );
      if (matchIndex < 0) {
        try {
          const pageStateFile = await resolveChatGptFileFromPageState(card, auth);
          const byteLength = utf8ByteLength(pageStateFile.content);
          if (
            byteLength > CHATGPT_FILE_MAX_BYTES ||
            totalBytes + byteLength > CHATGPT_FILE_TOTAL_MAX_BYTES
          ) {
            throw new Error("file is larger than the bridge limit");
          }
          resolvedFiles.push({ ...card, ...pageStateFile });
          totalBytes += byteLength;
        } catch (error) {
          stillUnresolved.push({
            ...card,
            error: error instanceof Error ? error.message : String(error),
          });
        }
        continue;
      }

      const reference = references[matchIndex];
      try {
        const content = await fetchChatGptTextFileById(
          reference.fileId,
          {
            ...card,
            fileName: card.fileName || reference.fileName,
          },
          auth,
        );
        const byteLength = utf8ByteLength(content);
        if (
          byteLength > CHATGPT_FILE_MAX_BYTES ||
          totalBytes + byteLength > CHATGPT_FILE_TOTAL_MAX_BYTES
        ) {
          throw new Error("file is larger than the bridge limit");
        }
        usedReferenceIndexes.add(matchIndex);
        resolvedFiles.push({
          ...card,
          fileName: card.fileName || reference.fileName,
          fileId: reference.fileId,
          content,
        });
        totalBytes += byteLength;
      } catch (error) {
        stillUnresolved.push({
          ...card,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    return appendChatGptFiles(responseText, resolvedFiles, stillUnresolved);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.warn("[AI Bridge] failed to read ChatGPT files from conversation", message);
    return appendChatGptFiles(
      responseText,
      resolvedFiles,
      unresolvedCards.map((card) => ({ ...card, error: message })),
    );
  }
}

async function resolveChatGptFileFromPageState(card, auth) {
  const result = await chrome.runtime.sendMessage({
    type: "AIBRIDGE_INSPECT_CHATGPT_FILE",
    fileName: card.fileName,
  });
  if (!result?.ok) {
    throw new Error(result?.error || "ChatGPT page-state inspection failed");
  }

  if (typeof result.content === "string" && result.content.trim()) {
    return {
      content: result.content.replace(/\r\n?/g, "\n"),
      source: "page-state",
    };
  }

  const fileIds = Array.isArray(result.fileIds) ? result.fileIds.slice(0, 5) : [];
  for (const fileId of fileIds) {
    try {
      return {
        content: await fetchChatGptTextFileById(fileId, card, auth),
        fileId,
        source: "page-state-id",
      };
    } catch (error) {
      console.info(
        "[AI Bridge] ChatGPT page-state file ID was not readable",
        fileId,
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  const urls = Array.isArray(result.urls) ? result.urls.slice(0, 5) : [];
  for (const url of urls) {
    if (!url || /^sandbox:/i.test(url)) {
      continue;
    }
    try {
      return {
        content: await fetchChatGptTextFile({ ...card, url }, auth),
        url,
        source: "page-state-url",
      };
    } catch (error) {
      console.info(
        "[AI Bridge] ChatGPT page-state file URL was not readable",
        error instanceof Error ? error.message : String(error),
      );
    }
  }

  throw new Error("no matching attachment data was found in the current ChatGPT turn");
}

function getLatestChatGptAssistantRoot() {
  const selectors = [
    "[data-message-author-role='assistant']",
    "[data-testid='conversation-turn-assistant']",
    "div.agent-turn",
  ];
  const roots = [];
  for (const selector of selectors) {
    let nodes = [];
    try {
      nodes = [...document.querySelectorAll(selector)];
    } catch {
      continue;
    }
    for (const node of nodes) {
      if (
        isVisible(node) &&
        !isUserMessageNode(node) &&
        !isPromptComposerNode(node) &&
        !roots.includes(node)
      ) {
        roots.push(node);
      }
    }
    if (roots.length > 0) {
      break;
    }
  }
  roots.sort(compareDocumentOrder);
  return roots[roots.length - 1];
}

function collectChatGptTextFileCards(root) {
  const candidates = [];
  const selector = [
    "a[href]",
    "button",
    "[role='link']",
    "[data-file-id]",
    "[data-attachment-id]",
    "[data-testid*='file' i]",
  ].join(", ");

  let nodes = [];
  try {
    nodes = [...root.querySelectorAll(selector)];
  } catch {
    return [];
  }

  for (const node of nodes) {
    if (!isVisible(node)) {
      continue;
    }
    const label = [
      node.getAttribute?.("download"),
      node.getAttribute?.("aria-label"),
      node.getAttribute?.("title"),
      node.innerText,
      node.textContent,
    ].map((value) => String(value || "").trim()).filter(Boolean).join("\n");
    const controlMetadata = getChatGptFileControlMetadata(node, root);
    const rawUrl = controlMetadata.url;
    const fileName =
      extractChatGptTextFileName(label) ||
      extractChatGptTextFileName(rawUrl);
    if (!fileName) {
      continue;
    }

    const fileId = controlMetadata.fileId || extractChatGptFileId(rawUrl);
    if (!isChatGptGeneratedFileControl(node, rawUrl, fileId)) {
      continue;
    }
    candidates.push({
      node,
      fileName,
      format: normalizeClaudeDocumentFormat(
        fileName.match(/\.([a-z0-9.+_-]{1,12})$/i)?.[1] || "",
      ),
      fileId,
      url: resolveChatGptFileUrl(rawUrl),
    });
  }

  const deduped = [];
  const seen = new Set();
  for (const candidate of candidates) {
    const key = canonicalClaudeDocumentName(candidate.fileName);
    if (!key || seen.has(key)) {
      continue;
    }
    seen.add(key);
    deduped.push(candidate);
  }
  return deduped.slice(0, 12);
}

function getChatGptFileControlMetadata(node, root) {
  let current = node;
  let url = "";
  let fileId = "";

  for (let depth = 0; current && depth < 7; depth += 1) {
    const currentText = normalizeText(current.innerText || current.textContent || "");
    if (current !== node && currentText.length > 1_500) {
      break;
    }

    url ||= firstClaudeDocumentString(
      current.getAttribute?.("href"),
      current.getAttribute?.("data-download-url"),
      current.getAttribute?.("data-file-url"),
      current.getAttribute?.("data-attachment-url"),
    );
    fileId ||= firstClaudeDocumentString(
      current.getAttribute?.("data-file-id"),
      current.getAttribute?.("data-attachment-id"),
      current.getAttribute?.("data-asset-id"),
    );

    if (!url || !fileId) {
      const nested = current.querySelector?.(
        "a[href^='sandbox:'], a[href*='/backend-api/files/'], " +
        "[data-file-id], [data-attachment-id], [data-download-url], [data-file-url]",
      );
      if (nested) {
        url ||= firstClaudeDocumentString(
          nested.getAttribute?.("href"),
          nested.getAttribute?.("data-download-url"),
          nested.getAttribute?.("data-file-url"),
        );
        fileId ||= firstClaudeDocumentString(
          nested.getAttribute?.("data-file-id"),
          nested.getAttribute?.("data-attachment-id"),
        );
      }
    }

    if ((url && fileId) || current === root) {
      break;
    }
    current = current.parentElement;
  }

  return { url, fileId };
}

function isChatGptGeneratedFileControl(node, rawUrl, fileId) {
  if (fileId) {
    return true;
  }
  if (!rawUrl) {
    return node.matches?.("button, [role='button'], [role='link']") || false;
  }
  if (/^sandbox:/i.test(rawUrl)) {
    return true;
  }
  try {
    const url = new URL(rawUrl, window.location.href);
    return (
      /\/backend-api\/files\//i.test(url.pathname) ||
      url.hostname.toLowerCase().endsWith(".oaiusercontent.com")
    );
  } catch {
    return false;
  }
}

function extractChatGptTextFileName(value) {
  const text = String(value || "").replace(/\\\)/g, ")");
  const supportedExtension =
    "(?:md|markdown|txt|text|json|jsonl|csv|tsv|yaml|yml|xml|html?|css|js|jsx|ts|tsx|py|rs|c|cc|cpp|h|hpp|java|kt|go|rb|php|sh|bash|zsh|ps1|sql|toml|ini|cfg|conf|log)";
  const pattern = new RegExp(
    `(?:^|[\\\\/\\s(\"'])((?:[^\\\\/\\n\\r\"'()]+?)\\.${supportedExtension})(?=$|[\\s)\"'?#])`,
    "i",
  );
  const match = text.match(pattern);
  if (!match) {
    return "";
  }
  try {
    return decodeURIComponent(match[1]).trim();
  } catch {
    return match[1].trim();
  }
}

function resolveChatGptFileUrl(rawUrl) {
  if (!rawUrl) {
    return "";
  }
  if (/^sandbox:/i.test(rawUrl)) {
    return rawUrl;
  }
  try {
    return new URL(rawUrl, window.location.href).href;
  } catch {
    return "";
  }
}

function extractChatGptFileId(value) {
  const text = String(value || "");
  const pointerMatch = text.match(/(?:file-service|sediment):\/\/([^/?#\s]+)/i);
  if (pointerMatch) {
    return pointerMatch[1];
  }
  const explicitMatch = text.match(/(?:^|[/?])((?:file[-_])?[a-z0-9][a-z0-9_-]{12,})(?:$|[/?#])/i);
  return explicitMatch?.[1] || "";
}

async function getChatGptWebAuth() {
  let session = {};
  try {
    const response = await fetch(new URL("/api/auth/session", window.location.origin).href, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (response.ok) {
      session = await response.json();
    }
  } catch (error) {
    console.info(
      "[AI Bridge] ChatGPT session metadata was not available",
      error instanceof Error ? error.message : String(error),
    );
  }

  return {
    accessToken: firstClaudeDocumentString(
      session?.accessToken,
      session?.access_token,
      session?.token,
    ),
    accountId: firstClaudeDocumentString(
      session?.account?.id,
      session?.account_id,
      session?.accountId,
      session?.user?.account_id,
      session?.user?.accountId,
    ),
  };
}

function chatGptRequestHeaders(auth, accept = "*/*") {
  const headers = { Accept: accept };
  if (auth?.accessToken) {
    headers.Authorization = `Bearer ${auth.accessToken}`;
  }
  if (auth?.accountId) {
    headers["ChatGPT-Account-Id"] = auth.accountId;
  }
  return headers;
}

async function fetchChatGptTextFile(card, auth) {
  if (card.fileId && (!card.url || card.url.startsWith("sandbox:"))) {
    return fetchChatGptTextFileById(card.fileId, card, auth);
  }
  const url = new URL(card.url, window.location.href);
  if (!isAllowedChatGptFileUrl(url)) {
    throw new Error("unsupported ChatGPT file URL");
  }
  return fetchChatGptTextFileUrl(url.href, card, auth, 0);
}

async function fetchChatGptTextFileById(fileId, card, auth) {
  const normalizedId = String(fileId || "").replace(/^(?:file-service|sediment):\/\//i, "");
  if (!normalizedId) {
    throw new Error("ChatGPT attachment has no file ID");
  }
  const metadataUrl = new URL(
    `/backend-api/files/download/${encodeURIComponent(normalizedId)}`,
    window.location.origin,
  ).href;
  return fetchChatGptTextFileUrl(metadataUrl, card, auth, 0);
}

async function fetchChatGptTextFileUrl(url, card, auth, depth) {
  if (depth > 2) {
    throw new Error("too many ChatGPT file redirects");
  }
  const parsedUrl = new URL(url, window.location.href);
  if (!isAllowedChatGptFileUrl(parsedUrl)) {
    throw new Error("unsupported ChatGPT file host");
  }

  const resource = await fetchChatGptFileResource(parsedUrl, auth);
  const { bytes, contentType } = resource;
  const decoded = decodeClaudeDocumentBytes(bytes);

  if (
    /application\/json/i.test(contentType) &&
    /\/backend-api\/files\//i.test(parsedUrl.pathname)
  ) {
    try {
      const metadata = JSON.parse(decoded);
      const downloadUrl =
        metadata?.download_url ||
        metadata?.downloadUrl ||
        metadata?.url ||
        "";
      if (downloadUrl) {
        return await fetchChatGptTextFileUrl(downloadUrl, card, auth, depth + 1);
      }
    } catch {
      // A generated .json file is valid text and should be returned as-is.
    }
  }

  if (!isClaudeTextDocument(contentType, card.fileName, card.format, bytes)) {
    throw new Error(`unsupported binary file type: ${contentType || "unknown"}`);
  }
  return decoded.replace(/\r\n?/g, "\n");
}

async function fetchChatGptFileResource(url, auth) {
  if (
    url.origin !== window.location.origin &&
    url.hostname.toLowerCase().endsWith(".oaiusercontent.com")
  ) {
    const result = await chrome.runtime.sendMessage({
      type: "AIBRIDGE_FETCH_CHATGPT_FILE",
      url: url.href,
      accessToken: auth?.accessToken || "",
      accountId: auth?.accountId || "",
    });
    if (!result?.ok) {
      throw new Error(result?.error || "ChatGPT file proxy failed");
    }
    const bytes = base64ToBytes(result.bytesBase64 || "");
    if (bytes.byteLength > CHATGPT_FILE_MAX_BYTES) {
      throw new Error("file is larger than the bridge limit");
    }
    return {
      bytes,
      contentType: String(result.contentType || "")
        .split(";", 1)[0]
        .trim()
        .toLowerCase(),
    };
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), CHATGPT_FILE_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url.href, {
      method: "GET",
      credentials: url.origin === window.location.origin ? "include" : "omit",
      cache: "no-store",
      headers: chatGptRequestHeaders(auth),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`file request returned HTTP ${response.status}`);
    }
    const declaredLength = Number(response.headers.get("content-length") || "0");
    if (declaredLength > CHATGPT_FILE_MAX_BYTES) {
      throw new Error("file is larger than the bridge limit");
    }
    return {
      bytes: await readClaudeResponseBytes(response, CHATGPT_FILE_MAX_BYTES),
      contentType: String(response.headers.get("content-type") || "")
        .split(";", 1)[0]
        .trim()
        .toLowerCase(),
    };
  } finally {
    clearTimeout(timeout);
  }
}

function base64ToBytes(value) {
  const binary = atob(String(value || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function isAllowedChatGptFileUrl(url) {
  if (url.protocol === "blob:") {
    return url.href.startsWith(`blob:${window.location.origin}/`);
  }
  if (url.protocol === "data:") {
    return /^data:(?:text\/|application\/(?:json|xml|javascript))/i.test(url.href);
  }
  if (url.protocol !== "https:") {
    return false;
  }
  const hostname = url.hostname.toLowerCase();
  return (
    hostname === window.location.hostname.toLowerCase() ||
    hostname === "chatgpt.com" ||
    hostname === "chat.openai.com" ||
    hostname.endsWith(".oaiusercontent.com") ||
    hostname.endsWith(".openai.com")
  );
}

async function fetchCurrentChatGptTurnFileReferences(auth) {
  const conversationId = getCurrentChatGptConversationId();
  if (!conversationId) {
    throw new Error("current ChatGPT conversation ID was not found");
  }

  const endpoint = new URL(
    `/backend-api/conversation/${encodeURIComponent(conversationId)}`,
    window.location.origin,
  ).href;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), CHATGPT_FILE_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(endpoint, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      headers: chatGptRequestHeaders(auth, "application/json"),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`ChatGPT conversation returned HTTP ${response.status}`);
    }
    const payload = await response.json();
    return getCurrentChatGptTurnFileReferences(payload);
  } finally {
    clearTimeout(timeout);
  }
}

function getCurrentChatGptConversationId() {
  const segments = window.location.pathname.split("/").filter(Boolean);
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f-]{20,}$/i;
  const cIndex = segments.lastIndexOf("c");
  if (cIndex >= 0 && uuidPattern.test(segments[cIndex + 1] || "")) {
    return segments[cIndex + 1];
  }
  return [...segments].reverse().find((segment) => uuidPattern.test(segment)) || "";
}

function getCurrentChatGptTurnFileReferences(payload) {
  const mapping = payload?.mapping && typeof payload.mapping === "object"
    ? payload.mapping
    : {};
  let nodeId = payload?.current_node || payload?.currentNode || "";
  if (!nodeId || !mapping[nodeId]) {
    nodeId = Object.keys(mapping).find((candidateId) => (
      Array.isArray(mapping[candidateId]?.children) &&
      mapping[candidateId].children.length === 0
    )) || "";
  }

  const references = [];
  const visitedNodes = new Set();
  let currentTurnUserNodeId = "";
  let sawResponseMessage = false;
  while (nodeId && mapping[nodeId] && !visitedNodes.has(nodeId)) {
    visitedNodes.add(nodeId);
    const node = mapping[nodeId];
    const message = node.message;
    const role = String(message?.author?.role || message?.role || "").toLowerCase();

    if (role === "user" && sawResponseMessage) {
      currentTurnUserNodeId = nodeId;
      break;
    }
    if (message && role !== "user") {
      sawResponseMessage = true;
      collectChatGptMessageFileReferences(message, references);
    }
    nodeId = node.parent || "";
  }

  if (currentTurnUserNodeId) {
    const queue = [...(mapping[currentTurnUserNodeId]?.children || [])];
    let inspectedNodeCount = 0;
    while (queue.length > 0 && inspectedNodeCount < 500) {
      const descendantId = queue.shift();
      if (!descendantId || visitedNodes.has(descendantId) || !mapping[descendantId]) {
        continue;
      }
      visitedNodes.add(descendantId);
      inspectedNodeCount += 1;
      const descendant = mapping[descendantId];
      const message = descendant.message;
      const role = String(message?.author?.role || message?.role || "").toLowerCase();
      if (role === "user") {
        continue;
      }
      if (message) {
        collectChatGptMessageFileReferences(message, references);
      }
      queue.push(...(descendant.children || []));
    }
  }

  const deduped = [];
  const seen = new Set();
  for (const reference of references) {
    const key = `${reference.fileId}\u0000${canonicalClaudeDocumentName(reference.fileName)}`;
    if (!reference.fileId || seen.has(key)) {
      continue;
    }
    seen.add(key);
    deduped.push(reference);
  }
  return deduped;
}

function collectChatGptMessageFileReferences(message, references) {
  const startingLength = references.length;
  const attachments = Array.isArray(message?.metadata?.attachments)
    ? message.metadata.attachments
    : [];
  for (const attachment of attachments) {
    addChatGptFileReference(attachment, references);
  }

  const parts = Array.isArray(message?.content?.parts) ? message.content.parts : [];
  for (const part of parts) {
    if (part && typeof part === "object") {
      addChatGptFileReference(part, references);
    }
  }

  collectNestedChatGptFileReferences(message?.metadata, references, 0);

  const serialized = safeJsonStringify(message);
  if (serialized) {
    const fileIds = extractChatGptFileIdsFromText(serialized);
    const fileNames = extractChatGptTextFileNames(serialized);
    if (fileIds.length === 1 && fileNames.length === 1) {
      references.push({
        fileId: fileIds[0],
        fileName: fileNames[0],
        mimeType: "",
      });
    } else if (fileIds.length === 1 && references.length === startingLength) {
      references.push({
        fileId: fileIds[0],
        fileName: fileNames[0] || "",
        mimeType: "",
      });
    }
  }
}

function collectNestedChatGptFileReferences(value, references, depth) {
  if (!value || depth > 6) {
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      collectNestedChatGptFileReferences(item, references, depth + 1);
    }
    return;
  }
  if (typeof value !== "object") {
    return;
  }

  addChatGptFileReference(value, references);
  for (const [key, child] of Object.entries(value)) {
    if (
      child &&
      typeof child === "object" &&
      /(?:attachment|file|asset|result|output|content|message)/i.test(key)
    ) {
      collectNestedChatGptFileReferences(child, references, depth + 1);
    }
  }
}

function addChatGptFileReference(value, references) {
  if (!value || typeof value !== "object") {
    return;
  }
  const pointer = firstClaudeDocumentString(
    value.asset_pointer,
    value.assetPointer,
    value.file_id,
    value.fileId,
    value.id,
  );
  const fileId = extractChatGptFileId(pointer) || (
    /^(?:file[-_])?[a-z0-9][a-z0-9_-]{12,}$/i.test(pointer) ? pointer : ""
  );
  if (!fileId) {
    return;
  }

  const fileName = firstClaudeDocumentString(
    value.name,
    value.file_name,
    value.filename,
    value.fileName,
    value.title,
    value.path,
  );
  references.push({
    fileId,
    fileName: extractChatGptTextFileName(fileName) || fileName,
    mimeType: firstClaudeDocumentString(value.mime_type, value.mimeType, value.content_type),
  });
}

function safeJsonStringify(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return "";
  }
}

function extractChatGptFileIdsFromText(value) {
  const text = String(value || "")
    .replace(/\\u002f/gi, "/")
    .replace(/\\\//g, "/")
    .replace(/\\"/g, "\"");
  const ids = [];
  const patterns = [
    /(?:file-service|sediment):\\?\/\\?\/([a-z0-9_-]{12,})/gi,
    /["'](?:file_id|fileId|attachment_id|attachmentId)["']\s*:\s*["']([a-z0-9_-]{12,})["']/gi,
    /\/backend-api\/files\/(?:download\/)?([a-z0-9_-]{12,})/gi,
  ];
  for (const pattern of patterns) {
    for (const match of text.matchAll(pattern)) {
      if (match[1] && !ids.includes(match[1])) {
        ids.push(match[1]);
      }
    }
  }
  return ids;
}

function extractChatGptTextFileNames(value) {
  const text = String(value || "")
    .replace(/\\u002f/gi, "/")
    .replace(/\\\//g, "/")
    .replace(/\\"/g, "\"");
  const names = [];
  const supportedExtension =
    "(?:md|markdown|txt|text|json|jsonl|csv|tsv|yaml|yml|xml|html?|css|js|jsx|ts|tsx|py|rs|c|cc|cpp|h|hpp|java|kt|go|rb|php|sh|bash|zsh|ps1|sql|toml|ini|cfg|conf|log)";
  const pattern = new RegExp(
    `(?:^|[\\\\/\\s(\"'])((?:[^\\\\/\\n\\r\"'()]+?)\\.${supportedExtension})(?=$|[\\s)\"'?#])`,
    "gi",
  );
  for (const match of text.matchAll(pattern)) {
    let name = match[1];
    try {
      name = decodeURIComponent(name);
    } catch {
      // Keep the original filename when it is not URL-encoded.
    }
    name = name.trim();
    if (name && !names.includes(name)) {
      names.push(name);
    }
  }
  return names;
}

function findChatGptFileReferenceMatch(card, references, usedIndexes, unresolvedCount) {
  const cardName = canonicalClaudeDocumentName(card.fileName);
  const exactIndex = references.findIndex((reference, index) => (
    !usedIndexes.has(index) &&
    cardName &&
    cardName === canonicalClaudeDocumentName(reference.fileName)
  ));
  if (exactIndex >= 0) {
    return exactIndex;
  }

  const partialIndex = references.findIndex((reference, index) => {
    if (usedIndexes.has(index)) {
      return false;
    }
    const referenceName = canonicalClaudeDocumentName(reference.fileName);
    return (
      cardName.length >= 8 &&
      referenceName.length >= 8 &&
      (cardName.includes(referenceName) || referenceName.includes(cardName))
    );
  });
  if (partialIndex >= 0) {
    return partialIndex;
  }

  const unusedIndexes = references
    .map((_reference, index) => index)
    .filter((index) => !usedIndexes.has(index));
  if (unresolvedCount === 1 && unusedIndexes.length === 1) {
    return unusedIndexes[0];
  }
  return -1;
}

function appendChatGptFiles(responseText, files, unresolvedCards) {
  const sections = [];
  const seen = new Set();
  for (const file of files) {
    const content = String(file.content || "").replace(/\r\n?/g, "\n");
    const key = `${canonicalClaudeDocumentName(file.fileName)}\u0000${content}`;
    if (!content.trim() || seen.has(key)) {
      continue;
    }
    seen.add(key);
    const suffix = content.endsWith("\n") ? "" : "\n";
    sections.push(
      `## ChatGPT file: ${escapeMarkdownHeading(file.fileName || "text file")}\n\n${content}${suffix}`,
    );
  }

  for (const card of unresolvedCards) {
    const reason = card.error ? `: ${card.error}` : "";
    sections.push(
      `> AI Bridge could not read the ChatGPT file “${card.fileName || "text file"}”${reason}.`,
    );
  }

  const base = String(responseText || "").trimEnd();
  if (sections.length === 0) {
    return base;
  }
  return [base, ...sections].filter(Boolean).join("\n\n---\n\n");
}

function extractRichResponseMarkdown(root, source) {
  if (!root) {
    return "";
  }

  const blockSelector = "h1, h2, h3, h4, h5, h6, p, ul, ol, pre, blockquote, table, hr";
  const candidates = [];
  try {
    if (root.matches?.(blockSelector)) {
      candidates.push(root);
    }
    candidates.push(...root.querySelectorAll(blockSelector));
  } catch {
    return "";
  }

  const blocks = getTopLevelElements(candidates.filter((node) => (
    isVisible(node) && !isResponseMarkdownNoiseNode(node, source)
  ))).sort(compareDocumentOrder);
  const markdown = blocks
    .map((node) => serializeMarkdownBlock(node, source))
    .filter((text) => text.trim())
    .join("\n\n")
    .replace(/\r\n?/g, "\n")
    .trim();

  return markdown;
}

function isResponseMarkdownNoiseNode(node, source) {
  if (
    node.closest("button, nav, form, fieldset, textarea, input, [contenteditable='true']") ||
    node.closest("[role='button'], [role='menu'], [role='toolbar'], [aria-hidden='true'], [hidden], .sr-only")
  ) {
    return true;
  }
  if (source === "claude" && isClaudeNoiseNode(node)) {
    return true;
  }
  if (source === "gemini" && isGeminiResearchNoiseNode(node)) {
    return true;
  }
  return isUserMessageNode(node) || isPromptComposerNode(node);
}

function serializeMarkdownBlock(node, source) {
  const tag = node.tagName;
  if (/^H[1-6]$/.test(tag)) {
    const level = Number(tag.slice(1));
    return `${"#".repeat(level)} ${serializeMarkdownInlineChildren(node).trim()}`;
  }
  if (tag === "P") {
    return serializeMarkdownInlineChildren(node).trim();
  }
  if (tag === "UL" || tag === "OL") {
    return serializeMarkdownList(node, source, 0);
  }
  if (tag === "PRE") {
    return serializeMarkdownCodeBlock(node);
  }
  if (tag === "BLOCKQUOTE") {
    const content = [...node.children]
      .map((child) => serializeMarkdownBlock(child, source))
      .filter((text) => text.trim())
      .join("\n\n") ||
      serializeMarkdownInlineChildren(node).trim() ||
      normalizeText(node.innerText || node.textContent || "");
    return content.split("\n").map((line) => `> ${line}`).join("\n");
  }
  if (tag === "TABLE") {
    return serializeMarkdownTable(node);
  }
  if (tag === "HR") {
    return "---";
  }
  return serializeMarkdownInlineChildren(node).trim();
}

function serializeMarkdownInlineChildren(node) {
  return [...node.childNodes].map(serializeMarkdownInlineNode).join("");
}

function serializeMarkdownInlineNode(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    return String(node.nodeValue || "")
      .replace(/\u00a0/g, " ")
      .replace(/[\t\r\n ]+/g, " ");
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return "";
  }

  const element = node;
  const tag = element.tagName;
  if (
    tag === "BUTTON" || tag === "SVG" || tag === "STYLE" || tag === "SCRIPT" ||
    element.matches("[aria-hidden='true'], [hidden], .sr-only")
  ) {
    return "";
  }
  if (tag === "BR") {
    return "\n";
  }
  if (tag === "STRONG" || tag === "B") {
    const content = serializeMarkdownInlineChildren(element).trim();
    return content ? `**${content}**` : "";
  }
  if (tag === "EM" || tag === "I") {
    const content = serializeMarkdownInlineChildren(element).trim();
    return content ? `*${content}*` : "";
  }
  if (tag === "S" || tag === "DEL") {
    const content = serializeMarkdownInlineChildren(element).trim();
    return content ? `~~${content}~~` : "";
  }
  if (tag === "CODE" && element.parentElement?.tagName !== "PRE") {
    const content = String(element.textContent || "").replace(/\u00a0/g, " ");
    if (!content) {
      return "";
    }
    const longestRun = Math.max(0, ...[...content.matchAll(/`+/g)].map((match) => match[0].length));
    const fence = "`".repeat(Math.max(1, longestRun + 1));
    const padding = content.startsWith("`") || content.endsWith("`") ? " " : "";
    return `${fence}${padding}${content}${padding}${fence}`;
  }
  if (tag === "A") {
    const label = serializeMarkdownInlineChildren(element).trim();
    const href = element.getAttribute("href") || "";
    if (!href) {
      return label;
    }
    return `[${label || href}](${href.replace(/\)/g, "\\)")})`;
  }
  if (tag === "IMG") {
    const alt = element.getAttribute("alt") || "";
    const src = element.getAttribute("src") || "";
    return src ? `![${alt}](${src.replace(/\)/g, "\\)")})` : alt;
  }
  if (tag === "PRE") {
    return `\n${serializeMarkdownCodeBlock(element)}\n`;
  }
  return serializeMarkdownInlineChildren(element);
}

function serializeMarkdownCodeBlock(pre) {
  const code = pre.querySelector("code") || pre;
  let content = String(code.textContent || "")
    .replace(/\u00a0/g, " ")
    .replace(/\r\n?/g, "\n");
  if (content.startsWith("\n")) {
    content = content.slice(1);
  }
  if (content.endsWith("\n")) {
    content = content.slice(0, -1);
  }

  const className = typeof code.className === "string" ? code.className : "";
  const language = className.match(/(?:^|\s)language-([\w+-]+)/i)?.[1] || "";
  const longestRun = Math.max(0, ...[...content.matchAll(/`+/g)].map((match) => match[0].length));
  const fence = "`".repeat(Math.max(3, longestRun + 1));
  return `${fence}${language}\n${content}\n${fence}`;
}

function serializeMarkdownList(list, source, depth) {
  const ordered = list.tagName === "OL";
  const start = ordered ? Number(list.getAttribute("start") || "1") : 1;
  const items = [...list.children].filter((child) => child.tagName === "LI");
  const indent = "  ".repeat(depth);

  return items.map((item, index) => {
    const nestedLists = [...item.children].filter((child) => child.tagName === "UL" || child.tagName === "OL");
    const body = [...item.childNodes]
      .filter((child) => !nestedLists.includes(child))
      .map((child) => {
        if (child.nodeType === Node.ELEMENT_NODE && child.tagName === "PRE") {
          return serializeMarkdownCodeBlock(child);
        }
        return serializeMarkdownInlineNode(child);
      })
      .join("")
      .trim();
    const marker = ordered ? `${start + index}. ` : "- ";
    const continuation = `${indent}${" ".repeat(marker.length)}`;
    const bodyLines = (body || " ").split("\n");
    let result = `${indent}${marker}${bodyLines[0]}`;
    if (bodyLines.length > 1) {
      result += `\n${bodyLines.slice(1).map((line) => continuation + line).join("\n")}`;
    }
    for (const nested of nestedLists) {
      result += `\n${serializeMarkdownList(nested, source, depth + 1)}`;
    }
    return result;
  }).join("\n");
}

function serializeMarkdownTable(table) {
  const rows = [...table.rows].map((row) => (
    [...row.cells].map((cell) => (
      serializeMarkdownInlineChildren(cell)
        .trim()
        .replace(/\n/g, "<br>")
        .replace(/\|/g, "\\|")
    ))
  )).filter((cells) => cells.length > 0);
  if (rows.length === 0) {
    return "";
  }

  const width = Math.max(...rows.map((cells) => cells.length));
  const normalizeRow = (cells) => [...cells, ...Array(Math.max(0, width - cells.length)).fill("")];
  const header = normalizeRow(rows[0]);
  const lines = [
    `| ${header.join(" | ")} |`,
    `| ${header.map(() => "---").join(" | ")} |`,
  ];
  for (const row of rows.slice(1)) {
    lines.push(`| ${normalizeRow(row).join(" | ")} |`);
  }
  return lines.join("\n");
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
  const markdown = extractRichResponseMarkdown(root, "gemini");
  if (markdown) {
    return markdown;
  }

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
  const root = getLatestClaudeAssistantRoot();
  return root ? extractClaudeTurnText(root) : "";
}

function getLatestClaudeAssistantRoot() {
  const afterLastUserRoot = findClaudeTurnRootAfter(getLastClaudeUserMessageNode());
  const fallbackRoots = getTopLevelElements(getClaudeAssistantRoots().filter((node) => (
    !isUserMessageNode(node) &&
    !isPromptComposerNode(node)
  ))).sort(compareDocumentOrder);
  return afterLastUserRoot || fallbackRoots[fallbackRoots.length - 1];
}

async function getLatestClaudeAssistantTextWithDocuments() {
  const root = getLatestClaudeAssistantRoot();
  if (!root) {
    return "";
  }

  const responseText = extractClaudeTurnText(root);
  const cards = collectClaudeDocumentCards(root);
  if (cards.length === 0) {
    return responseText;
  }

  const resolvedDocuments = [];
  const unresolvedCards = [];
  let totalBytes = 0;

  for (const card of cards) {
    const inlineContent = getClaudeInlineDocumentContent(card.node);
    if (inlineContent) {
      const documentInfo = createResolvedClaudeDocument(card, inlineContent, "dom");
      const byteLength = utf8ByteLength(documentInfo.content);
      if (
        byteLength <= CLAUDE_DOCUMENT_MAX_BYTES &&
        totalBytes + byteLength <= CLAUDE_DOCUMENT_TOTAL_MAX_BYTES
      ) {
        resolvedDocuments.push(documentInfo);
        totalBytes += byteLength;
        continue;
      }
    }

    if (card.url) {
      try {
        const content = await fetchClaudeDocumentText(card);
        const byteLength = utf8ByteLength(content);
        if (totalBytes + byteLength > CLAUDE_DOCUMENT_TOTAL_MAX_BYTES) {
          throw new Error("combined document size exceeds the bridge limit");
        }
        resolvedDocuments.push(createResolvedClaudeDocument(card, content, "download"));
        totalBytes += byteLength;
        continue;
      } catch (error) {
        console.info(
          "[AI Bridge] Claude document link was not readable; trying conversation data",
          card.title,
          error instanceof Error ? error.message : String(error),
        );
      }
    }

    unresolvedCards.push(card);
  }

  if (unresolvedCards.length > 0) {
    try {
      const conversationDocuments = await fetchCurrentClaudeConversationDocuments(
        root,
        unresolvedCards,
      );
      const usedDocumentIndexes = new Set();
      const stillUnresolved = [];

      for (const card of unresolvedCards) {
        const matchIndex = findClaudeConversationDocumentMatch(
          card,
          conversationDocuments,
          usedDocumentIndexes,
          unresolvedCards.length,
        );
        if (matchIndex < 0) {
          stillUnresolved.push({
            ...card,
            error: "no matching file payload was found in the current Claude conversation",
          });
          continue;
        }

        const match = conversationDocuments[matchIndex];
        const byteLength = utf8ByteLength(match.content);
        if (
          byteLength > CLAUDE_DOCUMENT_MAX_BYTES ||
          totalBytes + byteLength > CLAUDE_DOCUMENT_TOTAL_MAX_BYTES
        ) {
          stillUnresolved.push({
            ...card,
            error: "document is larger than the bridge limit",
          });
          continue;
        }

        usedDocumentIndexes.add(matchIndex);
        resolvedDocuments.push({
          title: card.title || match.title,
          fileName: card.fileName || match.fileName,
          format: card.format || match.format,
          content: match.content,
          source: "conversation",
        });
        totalBytes += byteLength;
      }

      return appendClaudeDocuments(responseText, resolvedDocuments, stillUnresolved);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.warn("[AI Bridge] failed to read Claude documents from conversation", message);
      return appendClaudeDocuments(
        responseText,
        resolvedDocuments,
        unresolvedCards.map((card) => ({ ...card, error: message })),
      );
    }
  }

  return appendClaudeDocuments(responseText, resolvedDocuments, []);
}

function collectClaudeDocumentCards(root) {
  const seeds = [];
  const seedSelector = [
    "a[download]",
    "a[href^='blob:']",
    "button",
    "[role='button']",
    "[data-testid*='download' i]",
    "[data-testid*='file' i]",
  ].join(", ");

  try {
    for (const node of root.querySelectorAll(seedSelector)) {
      if (!isVisible(node)) {
        continue;
      }
      const label = normalizeText(
        node.getAttribute?.("aria-label") ||
        node.getAttribute?.("title") ||
        node.innerText ||
        node.textContent ||
        "",
      );
      const href = node.getAttribute?.("href") || "";
      if (
        node.hasAttribute?.("download") ||
        href.startsWith("blob:") ||
        /^(?:download|скачать)(?:\s|$)/i.test(label) ||
        /(?:download|file|attachment)/i.test(node.getAttribute?.("data-testid") || "")
      ) {
        seeds.push(node);
      }
    }
  } catch {
    return [];
  }

  const cards = [];
  const seenNodes = new Set();
  for (const seed of seeds) {
    const cardNode = findClaudeDocumentCardNode(seed, root);
    if (!cardNode || seenNodes.has(cardNode)) {
      continue;
    }
    seenNodes.add(cardNode);
    cards.push(describeClaudeDocumentCard(cardNode));
    if (cards.length >= 12) {
      break;
    }
  }
  return cards;
}

function findClaudeDocumentCardNode(seed, root) {
  let current = seed;
  let fallback = undefined;

  for (let depth = 0; current && depth < 9; depth += 1) {
    const text = normalizeText(current.innerText || current.textContent || "");
    if (current !== root && text && text.length <= 2_000) {
      if (!fallback && current !== seed) {
        fallback = current;
      }
      if (
        /(?:document|документ)\s*[·•]\s*[a-z0-9.+_-]+/i.test(text) &&
        /(?:download|скачать)/i.test(text)
      ) {
        return current;
      }
    }
    if (current === root) {
      break;
    }
    current = current.parentElement;
  }

  if (seed.matches?.("a[download], a[href^='blob:']")) {
    return fallback;
  }
  return undefined;
}

function describeClaudeDocumentCard(cardNode) {
  const text = String(cardNode.innerText || cardNode.textContent || "")
    .replace(/\r\n?/g, "\n");
  const lines = text
    .split(/\n+/)
    .map((line) => normalizeText(line))
    .filter(Boolean);
  const metadataMatch = text.match(
    /(?:document|документ)\s*[·•]\s*([a-z0-9.+_-]+)/i,
  );
  const format = normalizeClaudeDocumentFormat(metadataMatch?.[1] || "");

  const titleSelectors = [
    "[data-testid*='file-name' i]",
    "[data-testid*='filename' i]",
    "[class*='line-clamp-1']",
    "[class*='break-all'][class*='font-medium']",
    "[class*='font-medium']",
  ];
  let title = "";
  for (const selector of titleSelectors) {
    const candidate = cardNode.querySelector?.(selector);
    const candidateText = normalizeText(candidate?.innerText || candidate?.textContent || "");
    if (isClaudeDocumentTitleLine(candidateText)) {
      title = candidateText;
      break;
    }
  }
  if (!title) {
    title = lines.find(isClaudeDocumentTitleLine) || "Claude document";
  }

  const links = [...(cardNode.querySelectorAll?.("a[href]") || [])];
  let bestLink = undefined;
  let bestScore = -1;
  for (const link of links) {
    const href = link.getAttribute("href") || "";
    const label = normalizeText(
      link.getAttribute("aria-label") ||
      link.getAttribute("title") ||
      link.innerText ||
      link.textContent ||
      "",
    );
    let score = 0;
    if (link.hasAttribute("download")) score += 100;
    if (href.startsWith("blob:")) score += 80;
    if (/^(?:download|скачать)(?:\s|$)/i.test(label)) score += 60;
    if (/(?:download|attachment|file)/i.test(href)) score += 30;
    if (score > bestScore) {
      bestLink = link;
      bestScore = score;
    }
  }

  const dataUrlNode = cardNode.querySelector?.(
    "[data-download-url], [data-file-url], [data-attachment-url]",
  );
  const rawUrl =
    bestLink?.getAttribute("href") ||
    dataUrlNode?.getAttribute("data-download-url") ||
    dataUrlNode?.getAttribute("data-file-url") ||
    dataUrlNode?.getAttribute("data-attachment-url") ||
    "";
  const downloadName = normalizeText(bestLink?.getAttribute("download") || "");
  const fileName = downloadName || inferClaudeDocumentFileName(title, format);

  return {
    node: cardNode,
    title,
    fileName,
    format,
    url: resolveClaudeDocumentUrl(rawUrl),
  };
}

function isClaudeDocumentTitleLine(line) {
  return Boolean(
    line &&
    line.length <= 300 &&
    !/^(?:download|скачать)$/i.test(line) &&
    !/^(?:document|документ)\s*[·•]\s*[a-z0-9.+_-]+$/i.test(line),
  );
}

function normalizeClaudeDocumentFormat(format) {
  return String(format || "").trim().replace(/^\./, "").toUpperCase();
}

function inferClaudeDocumentFileName(title, format) {
  const cleanedTitle = normalizeText(title || "") || "claude-document";
  if (/\.[a-z0-9]{1,12}$/i.test(cleanedTitle) || !format) {
    return cleanedTitle;
  }
  return `${cleanedTitle}.${format.toLowerCase()}`;
}

function resolveClaudeDocumentUrl(rawUrl) {
  if (!rawUrl) {
    return "";
  }
  try {
    return new URL(rawUrl, window.location.href).href;
  } catch {
    return "";
  }
}

function getClaudeInlineDocumentContent(cardNode) {
  const contentNode = cardNode.querySelector?.(
    "[data-file-content], [data-document-content], pre, textarea",
  );
  if (!contentNode) {
    return "";
  }
  const attributeContent =
    contentNode.getAttribute?.("data-file-content") ||
    contentNode.getAttribute?.("data-document-content");
  const content = attributeContent ?? contentNode.value ?? contentNode.textContent ?? "";
  return String(content).trim() ? String(content).replace(/\r\n?/g, "\n") : "";
}

function createResolvedClaudeDocument(card, content, source) {
  return {
    title: card.title,
    fileName: card.fileName,
    format: card.format,
    content: String(content).replace(/\r\n?/g, "\n"),
    source,
  };
}

async function fetchClaudeDocumentText(card) {
  const parsedUrl = new URL(card.url, window.location.href);
  if (!isAllowedClaudeDocumentUrl(parsedUrl)) {
    throw new Error("unsupported document URL");
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), CLAUDE_DOCUMENT_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(parsedUrl.href, {
      method: "GET",
      credentials: parsedUrl.origin === window.location.origin ? "include" : "omit",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`download returned HTTP ${response.status}`);
    }

    const declaredLength = Number(response.headers.get("content-length") || "0");
    if (declaredLength > CLAUDE_DOCUMENT_MAX_BYTES) {
      throw new Error("document is larger than the bridge limit");
    }

    const bytes = await readClaudeResponseBytes(response, CLAUDE_DOCUMENT_MAX_BYTES);
    const contentType = String(response.headers.get("content-type") || "")
      .split(";", 1)[0]
      .trim()
      .toLowerCase();
    if (!isClaudeTextDocument(contentType, card.fileName, card.format, bytes)) {
      throw new Error(`unsupported binary document type: ${contentType || "unknown"}`);
    }

    return decodeClaudeDocumentBytes(bytes);
  } finally {
    clearTimeout(timeout);
  }
}

function isAllowedClaudeDocumentUrl(url) {
  if (url.protocol === "blob:") {
    return url.href.startsWith(`blob:${window.location.origin}/`);
  }
  if (url.protocol === "data:") {
    return /^data:(?:text\/|application\/(?:json|xml|javascript))/i.test(url.href);
  }
  return url.protocol === "https:";
}

async function readClaudeResponseBytes(response, maxBytes) {
  if (!response.body?.getReader) {
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > maxBytes) {
      throw new Error("document is larger than the bridge limit");
    }
    return new Uint8Array(buffer);
  }

  const reader = response.body.getReader();
  const chunks = [];
  let totalLength = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    totalLength += value.byteLength;
    if (totalLength > maxBytes) {
      await reader.cancel();
      throw new Error("document is larger than the bridge limit");
    }
    chunks.push(value);
  }

  const result = new Uint8Array(totalLength);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function isClaudeTextDocument(contentType, fileName, format, bytes) {
  if (
    contentType.startsWith("text/") ||
    /^(?:application\/(?:json|ld\+json|xml|javascript|x-yaml|yaml))$/.test(contentType)
  ) {
    return true;
  }

  const extension = (
    String(fileName || "").match(/\.([a-z0-9.+_-]{1,12})$/i)?.[1] ||
    format ||
    ""
  ).toLowerCase();
  if (
    /^(?:md|markdown|txt|text|json|jsonl|csv|tsv|yaml|yml|xml|html?|css|js|jsx|ts|tsx|py|rs|c|cc|cpp|h|hpp|java|kt|go|rb|php|sh|bash|zsh|ps1|sql|toml|ini|cfg|conf|log)$/.test(
      extension,
    )
  ) {
    return true;
  }

  const sampleLength = Math.min(bytes.length, 4_096);
  let zeroBytes = 0;
  for (let index = 0; index < sampleLength; index += 1) {
    if (bytes[index] === 0) {
      zeroBytes += 1;
    }
  }
  return sampleLength > 0 && zeroBytes / sampleLength < 0.01;
}

function decodeClaudeDocumentBytes(bytes) {
  let encoding = "utf-8";
  let offset = 0;
  if (bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
    offset = 3;
  } else if (bytes[0] === 0xff && bytes[1] === 0xfe) {
    encoding = "utf-16le";
    offset = 2;
  } else if (bytes[0] === 0xfe && bytes[1] === 0xff) {
    encoding = "utf-16be";
    offset = 2;
  }
  return new TextDecoder(encoding).decode(bytes.subarray(offset)).replace(/\r\n?/g, "\n");
}

async function fetchCurrentClaudeConversationDocuments(root, cards) {
  const conversationId = getCurrentClaudeConversationId();
  if (!conversationId) {
    throw new Error("current Claude conversation ID was not found");
  }
  const organizationId = await getCurrentClaudeOrganizationId();
  if (!organizationId) {
    throw new Error("current Claude organization ID was not found");
  }

  const endpoint = new URL(
    `/api/organizations/${encodeURIComponent(organizationId)}/chat_conversations/${encodeURIComponent(conversationId)}`,
    window.location.origin,
  );
  endpoint.searchParams.set("tree", "True");
  endpoint.searchParams.set("rendering_mode", "messages");
  endpoint.searchParams.set("render_all_tools", "true");
  const payload = await fetchClaudeJson(endpoint.href);
  const messages = getClaudeConversationMessages(payload);
  const visibleText = extractClaudeTurnText(root);
  return selectCurrentClaudeMessageDocuments(messages, visibleText, cards);
}

function getCurrentClaudeConversationId() {
  const segments = window.location.pathname.split("/").filter(Boolean);
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f-]{20,}$/i;
  return [...segments].reverse().find((segment) => uuidPattern.test(segment)) || "";
}

async function getCurrentClaudeOrganizationId() {
  const cookieValue = getCookieValue("lastActiveOrg");
  if (cookieValue) {
    return cookieValue;
  }

  const account = await fetchClaudeJson(new URL("/api/account", window.location.origin).href);
  const memberships = Array.isArray(account?.memberships) ? account.memberships : [];
  return memberships
    .map((membership) => (
      membership?.organization?.uuid ||
      membership?.organization_uuid ||
      membership?.organization?.id ||
      ""
    ))
    .find(Boolean) || "";
}

function getCookieValue(name) {
  const encodedName = `${encodeURIComponent(name)}=`;
  for (const part of String(document.cookie || "").split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(encodedName)) {
      try {
        return decodeURIComponent(trimmed.slice(encodedName.length));
      } catch {
        return trimmed.slice(encodedName.length);
      }
    }
  }
  return "";
}

async function fetchClaudeJson(url) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), CLAUDE_DOCUMENT_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`Claude conversation returned HTTP ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function getClaudeConversationMessages(payload) {
  const result = [];
  const seen = new Set();

  const visit = (value, depth) => {
    if (!value || depth > 12) {
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        visit(item, depth + 1);
      }
      return;
    }
    if (typeof value !== "object" || seen.has(value)) {
      return;
    }
    seen.add(value);

    if (
      ("sender" in value || "role" in value) &&
      ("content" in value || "message" in value)
    ) {
      result.push(value);
    }
    for (const key of ["chat_messages", "messages", "children"]) {
      if (value[key]) {
        visit(value[key], depth + 1);
      }
    }
  };

  visit(payload?.chat_messages || payload?.messages || payload, 0);
  return result;
}

function selectCurrentClaudeMessageDocuments(messages, visibleText, cards) {
  const comparableVisibleText = comparableClaudeDocumentText(visibleText);
  let best = undefined;

  messages.forEach((message, index) => {
    const sender = String(message?.sender || message?.role || "").toLowerCase();
    if (!/(?:assistant|claude)/.test(sender)) {
      return;
    }
    const documents = extractClaudeDocumentsFromMessage(message);
    if (documents.length === 0) {
      return;
    }

    let score = index;
    const messageText = comparableClaudeDocumentText(getClaudeMessagePlainText(message));
    if (
      messageText.length >= 40 &&
      comparableVisibleText.includes(messageText.slice(0, Math.min(240, messageText.length)))
    ) {
      score += 50_000;
    }

    for (const card of cards) {
      if (documents.some((documentInfo) => claudeDocumentNamesMatch(card, documentInfo))) {
        score += 100_000;
      }
    }

    if (!best || score >= best.score) {
      best = { score, documents };
    }
  });

  return best?.documents || [];
}

function getClaudeMessagePlainText(message) {
  const blocks = Array.isArray(message?.content) ? message.content : [message?.content];
  return blocks
    .filter((block) => block && typeof block === "object" && block.type === "text")
    .map((block) => String(block.text || ""))
    .filter(Boolean)
    .join("\n");
}

function extractClaudeDocumentsFromMessage(message) {
  const blocks = Array.isArray(message?.content) ? message.content : [message?.content];
  const documents = [];

  for (const block of blocks) {
    if (!block || typeof block !== "object") {
      continue;
    }
    const type = String(block.type || "").toLowerCase();
    if (!/(?:tool_use|document|attachment|artifact|file)/.test(type)) {
      continue;
    }
    const root = type === "tool_use" ? (block.input || block) : block;
    collectClaudeDocumentPayloads(root, {
      toolName: String(block.name || ""),
      title: firstClaudeDocumentString(
        root?.file_name,
        root?.filename,
        root?.path,
        root?.title,
        root?.name,
      ),
      format: firstClaudeDocumentString(root?.language, root?.format, root?.file_type),
    }, documents, 0);
  }

  const deduped = [];
  const seen = new Set();
  for (const documentInfo of documents) {
    const key = `${documentInfo.fileName}\u0000${documentInfo.content}`;
    if (!seen.has(key)) {
      seen.add(key);
      deduped.push(documentInfo);
    }
  }
  return deduped;
}

function collectClaudeDocumentPayloads(value, hint, documents, depth) {
  if (!value || depth > 6) {
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      collectClaudeDocumentPayloads(item, hint, documents, depth + 1);
    }
    return;
  }
  if (typeof value !== "object") {
    return;
  }

  const title = firstClaudeDocumentString(
    value.file_name,
    value.filename,
    value.path,
    value.title,
    value.name,
    hint.title,
  );
  const format = normalizeClaudeDocumentFormat(firstClaudeDocumentString(
    value.language,
    value.format,
    value.file_type,
    hint.format,
    String(title).match(/\.([a-z0-9.+_-]{1,12})$/i)?.[1],
  ));
  const fileName = inferClaudeDocumentFileName(
    title || hint.toolName || "Claude document",
    format,
  );

  for (const key of [
    "content",
    "file_text",
    "file_content",
    "file_contents",
    "document_content",
    "markdown",
    "code",
    "body",
  ]) {
    const content = value[key];
    if (typeof content === "string" && content.trim()) {
      documents.push({
        title: title || fileName,
        fileName,
        format,
        content: content.replace(/\r\n?/g, "\n"),
      });
    }
  }

  for (const key of ["files", "documents", "attachments", "artifacts", "file", "document", "output"]) {
    if (value[key]) {
      collectClaudeDocumentPayloads(value[key], { ...hint, title, format }, documents, depth + 1);
    }
  }
}

function firstClaudeDocumentString(...values) {
  return values.find((value) => typeof value === "string" && value.trim())?.trim() || "";
}

function findClaudeConversationDocumentMatch(
  card,
  documents,
  usedIndexes,
  unresolvedCardCount,
) {
  const exactIndex = documents.findIndex((documentInfo, index) => (
    !usedIndexes.has(index) && claudeDocumentNamesMatch(card, documentInfo)
  ));
  if (exactIndex >= 0) {
    return exactIndex;
  }

  const formatIndex = documents.findIndex((documentInfo, index) => (
    !usedIndexes.has(index) &&
    card.format &&
    documentInfo.format &&
    card.format === documentInfo.format
  ));
  if (formatIndex >= 0) {
    return formatIndex;
  }

  const unusedIndexes = documents
    .map((_documentInfo, index) => index)
    .filter((index) => !usedIndexes.has(index));
  if (unresolvedCardCount === 1 && unusedIndexes.length === 1) {
    return unusedIndexes[0];
  }
  return -1;
}

function claudeDocumentNamesMatch(left, right) {
  const leftNames = [left.title, left.fileName].map(canonicalClaudeDocumentName).filter(Boolean);
  const rightNames = [right.title, right.fileName].map(canonicalClaudeDocumentName).filter(Boolean);
  return leftNames.some((leftName) => rightNames.some((rightName) => (
    leftName === rightName ||
    (leftName.length >= 8 && rightName.includes(leftName)) ||
    (rightName.length >= 8 && leftName.includes(rightName))
  )));
}

function canonicalClaudeDocumentName(value) {
  return String(value || "")
    .normalize("NFKD")
    .toLowerCase()
    .replace(/\.[a-z0-9]{1,12}$/i, "")
    .replace(/[^\p{L}\p{N}]+/gu, "");
}

function comparableClaudeDocumentText(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function appendClaudeDocuments(responseText, documents, unresolvedCards) {
  const sections = [];
  const seen = new Set();

  for (const documentInfo of documents) {
    const content = String(documentInfo.content || "").replace(/\r\n?/g, "\n");
    const key = `${canonicalClaudeDocumentName(documentInfo.fileName || documentInfo.title)}\u0000${content}`;
    if (!content.trim() || seen.has(key)) {
      continue;
    }
    seen.add(key);

    const displayName =
      documentInfo.fileName ||
      inferClaudeDocumentFileName(documentInfo.title, documentInfo.format);
    const heading = escapeMarkdownHeading(displayName || "Claude document");
    const suffix = content.endsWith("\n") ? "" : "\n";
    sections.push(`## Claude document: ${heading}\n\n${content}${suffix}`);
  }

  for (const card of unresolvedCards) {
    const displayName = card.fileName || card.title || "Claude document";
    const reason = card.error ? `: ${card.error}` : "";
    sections.push(
      `> AI Bridge could not read the attached Claude document “${displayName}”${reason}.`,
    );
  }

  const base = String(responseText || "").trimEnd();
  if (sections.length === 0) {
    return base;
  }
  return [base, ...sections].filter(Boolean).join("\n\n---\n\n");
}

function escapeMarkdownHeading(value) {
  return String(value || "").replace(/([\\`*_{}[\]<>])/g, "\\$1");
}

function utf8ByteLength(value) {
  return new TextEncoder().encode(String(value || "")).byteLength;
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

  const markdown = extractRichResponseMarkdown(root, "claude");
  if (markdown) {
    return markdown;
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
