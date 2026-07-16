# AI Bridge

AI Bridge exposes the `ai` command on Windows and routes prompts through a
local broker to signed-in ChatGPT, Gemini, and Claude browser tabs.

The project is independent of Makima, Tauri, and local model/voice runtimes.

## Components

- `ai.cmd` and `aibridge.cli` provide the command-line interface.
- `aibridge.lifecycle` starts the broker only when a command needs it.
- `aibridge.broker` routes WebSocket messages by request ID.
- `aibridge.client` streams browser responses back to the CLI.
- `extension` injects prompts into supported sites and reads their responses.
- Gemini research-plan cards are continued automatically by clicking
  `Start Research` / `Начать исследование` and waiting for the research result.
- Completed Gemini Research documents are parsed from the immersive report
  view into headings, paragraphs, lists, quotes, tables, and code blocks.
- the optional debate modules implement `ai debate` and write to `reports`.

The broker listens only on loopback:

- WebSocket: `ws://127.0.0.1:8765`
- health and sources: `http://127.0.0.1:8766`

## Install on Windows

Install Python 3.11+ and the Python dependency:

```powershell
python -m pip install -r requirements.txt
```

Register the on-demand task and add this repository to the user `PATH`:

```powershell
.\scripts\install.ps1
```

For migration from the original Makima checkout:

```powershell
.\scripts\install.ps1 -RemovePath C:\workspace\makima\python_core
```

The task has no boot, logon, time, or event triggers. Installation does not
start it. The first `ai` command after boot performs a health check and starts
the task with `schtasks /Run`; later commands reuse the same broker process.

Load `extension` as an unpacked Chrome extension after disabling the old
Makima extension. See `extension/README.md`.

## Usage

```powershell
ai "Explain this error" --chatgpt
ai --status
ai "Compare these approaches" --chatgpt --gemini --claude
ai --get-last --claude
ai --file .\prompt.txt --gemini
ai debate "Choose an architecture"
```

With no model flag, `ai` targets every supported source that currently has an
open tab. Normal requests print a completion summary; `--get-last` prints the
latest response from exactly one selected model.

## Configuration

The broker accepts these environment variables:

- `AIBRIDGE_HOST`, `AIBRIDGE_WS_PORT`, `AIBRIDGE_HTTP_PORT`
- `AIBRIDGE_READY_TIMEOUT_SECONDS`, `AIBRIDGE_TASK_NAME`
- `AIBRIDGE_MAX_MESSAGE_BYTES`, `AIBRIDGE_MAX_PENDING_REQUESTS`
- `AIBRIDGE_PENDING_TTL_SECONDS`, `AIBRIDGE_QUIET`
- `AIBRIDGE_WS_URL`, `AIBRIDGE_HTTP_URL` for CLI endpoint overrides

The bundled extension currently uses the default loopback ports.

## Uninstall

```powershell
.\scripts\uninstall.ps1 -RemovePath
```
