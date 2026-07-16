# AI Browser Bridge

This unpacked Chrome extension connects open ChatGPT, Gemini, and Claude tabs
to the local AI Bridge broker.

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Remove or disable the old **Makima Cloud Bridge** extension.
4. Click **Load unpacked** and select this `extension` directory.
5. Open a supported AI site and sign in.

The service worker checks `http://127.0.0.1:8766/health` and then connects to
`ws://127.0.0.1:8765`. It does not start the broker itself; the `ai` command
starts the on-demand Windows task when needed.

