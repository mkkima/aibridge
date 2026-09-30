"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "extension", "src", "content.js"),
  "utf8",
);

let activeInput;
let execCommandCalls = 0;
const dispatchedEvents = [];

class FakeEvent {
  constructor(type, options = {}) {
    this.type = type;
    Object.assign(this, options);
  }
}

class FakeDataTransfer {
  constructor() {
    this.values = new Map();
  }

  setData(type, value) {
    this.values.set(type, value);
  }

  getData(type) {
    return this.values.get(type) || "";
  }
}

class FakeClipboardEvent extends FakeEvent {}

const input = {
  tagName: "DIV",
  _text: "old text",
  click() {},
  focus() {
    activeInput = this;
  },
  dispatchEvent(event) {
    dispatchedEvents.push(event.type);
    // This models the ChatGPT behavior that exposed the regression: accepting
    // a synthetic paste mutates the editor, so a later insertText duplicates it.
    if (event.type === "paste") {
      this._text += event.clipboardData.getData("text/plain");
    }
    return true;
  },
  get innerText() {
    return this._text;
  },
  set innerText(value) {
    this._text = value;
  },
  get textContent() {
    return this._text;
  },
  set textContent(value) {
    this._text = value;
  },
};

const range = {
  selectNodeContents(node) {
    activeInput = node;
  },
  deleteContents() {
    activeInput._text = "";
  },
  collapse() {},
};

const context = vm.createContext({
  __input: input,
  __prompt: "hello my friend",
  AbortController,
  Blob,
  ClipboardEvent: FakeClipboardEvent,
  DataTransfer: FakeDataTransfer,
  Event: FakeEvent,
  InputEvent: FakeEvent,
  TextDecoder,
  TextEncoder,
  URL,
  chrome: {
    runtime: {
      onMessage: { addListener() {} },
      sendMessage() {},
    },
  },
  console: { info() {}, warn() {}, error() {} },
  document: {
    createRange() {
      return range;
    },
    execCommand(command, _showUi, value) {
      assert.equal(command, "insertText");
      execCommandCalls += 1;
      activeInput._text += value;
      return true;
    },
  },
  setTimeout,
  clearTimeout,
  window: {
    location: { hostname: "chatgpt.com" },
    getSelection() {
      return {
        removeAllRanges() {},
        addRange() {},
      };
    },
  },
});

vm.runInContext(source, context, { filename: "content.js" });
const insertedLength = vm.runInContext(
  "insertPrompt(__input, __prompt, 'chatgpt')",
  context,
);

assert.equal(input.textContent, "hello my friend");
assert.equal(insertedLength, "hello my friend".length);
assert.equal(execCommandCalls, 1);
assert.equal(dispatchedEvents.includes("paste"), false);
assert.equal(dispatchedEvents.includes("beforeinput"), false);

console.log("ChatGPT prompt insertion regression test passed.");
