# AI Browser Bridge

This cross-browser extension connects open ChatGPT, Gemini, and Claude tabs
to the local AI Bridge broker.

It supports **Chrome / Chromium-based browsers** (Edge, Brave, Vivaldi, Opera)
and **Mozilla Firefox** (v120+).

## Installation in Chrome / Chromium

1. Open `chrome://extensions`.
2. Enable **Developer mode** (toggle in the top-right corner).
3. Remove or disable any old versions of the extension.
4. Click **Load unpacked** and select this `extension` directory.
5. Open a supported AI site (ChatGPT, Gemini, or Claude) and sign in.

## Installation in Mozilla Firefox

1. Open `about:debugging#/runtime/this-firefox` in the address bar.
2. Click **Load Temporary Add-on...** (Загрузить временное дополнение...).
3. Select the `manifest.json` file inside this `extension` directory.
4. Open a supported AI site (ChatGPT, Gemini, or Claude) and sign in.

> **Note**: In standard Firefox, unsigned extensions are loaded as temporary add-ons for the active browser session. To reload after changes, click the **Reload** button on the `about:debugging` page.

## Communication Details

The extension checks `http://127.0.0.1:8766/health` and then connects to
`ws://127.0.0.1:8765`. It does not start the broker itself; the `ai` command
starts the on-demand broker when needed.


