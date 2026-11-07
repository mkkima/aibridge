# AI Bridge

AI Bridge exposes the `ai` command on Windows and routes prompts through a
local broker to signed-in ChatGPT, Gemini, and Claude browser tabs.

The project is independent of Makima, Tauri, and local model/voice runtimes.

## Components

- `ai.cmd` and `aibridge.cli` provide the command-line interface.
- `aibridge.lifecycle` starts the broker only when a command needs it.
- `aibridge.broker` routes WebSocket messages by request ID and commits job
  outcomes to a durable SQLite journal before reporting success.
- `aibridge.client` streams browser responses back to the CLI.
- `extension` injects prompts into supported sites and reads their responses.
- Gemini research-plan cards are continued automatically by clicking
  `Start Research` / `Начать исследование` and waiting for the research result.
- Completed Gemini Research documents are parsed from the immersive report
  view into headings, paragraphs, lists, quotes, tables, and code blocks.
- When a new Gemini prompt arrives while an immersive Research report is open,
  the extension closes it, opens a new chat, enables `Deep Research` from the
  composer `+` menu, and only then submits the prompt.
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
ai 'Explain this error' --claude
ai --status
ai "Compare these approaches" --chatgpt --gemini --claude
ai --get-last --claude
ai --file .\prompt.txt --gemini
ai debate "Choose an architecture"
```

Inline prompts must be one argument enclosed in matching single or double
quotes. Unquoted text and multiple positional fragments are rejected before the
broker or any browser tab receives a request. Commands that do not carry inline
text, such as `--status`, `--get-last`, and `--file`, continue to work normally.

With no model flag, `ai` targets every supported source that currently has an
open tab. Normal requests print a completion summary; `--get-last` reads the
latest visible assistant response directly from the selected model's open tab
without submitting a new prompt.

The journal is stored at `%LOCALAPPDATA%\AIBridge\data\jobs-v1.sqlite3` by
default. Prompt bodies are represented by their SHA-256 digest and byte length;
completed response bodies are stored in full so they survive broker and browser
restarts. Set `AIBRIDGE_DB_PATH` to use another location.

### Debate reports

`ai debate "<topic>"` runs a multi-round debate across the connected AI tabs and writes two files to `reports/` (created on first use, git-ignored):

- `<YYYY-MM-DD_HH-MM-SS>_debate_<slug>.md`: Markdown report with YAML frontmatter (`title`, `date`, `type: debate`), followed by the problem formalization, round 1 positions, round 2 critiques, synthesis and decision matrix.
- `<YYYY-MM-DD_HH-MM-SS>_debate_<slug>.json`: the full debate state.

The frontmatter `title` is the topic with double quotes removed, written as a JSON-style double-quoted string, so backslashes and line breaks are escaped and the frontmatter stays valid YAML. Timestamps have one-second resolution: two reports for the same topic in the same second overwrite each other.

A debate sends many long prompts to every participating AI tab (ChatGPT, Claude and Gemini) and consumes model quota. It needs the broker running and those tabs open.

## Configuration

The broker accepts these environment variables:

- `AIBRIDGE_HOST`, `AIBRIDGE_WS_PORT`, `AIBRIDGE_HTTP_PORT`
- `AIBRIDGE_READY_TIMEOUT_SECONDS`, `AIBRIDGE_TASK_NAME`
- `AIBRIDGE_MAX_MESSAGE_BYTES`, `AIBRIDGE_MAX_PENDING_REQUESTS`
- `AIBRIDGE_PENDING_TTL_SECONDS`, `AIBRIDGE_QUIET`
- `AIBRIDGE_DB_PATH`, `AIBRIDGE_JOB_TIMEOUT_SECONDS`
- `AIBRIDGE_RESPONSE_TIMEOUT_SECONDS`, `AIBRIDGE_MAINTENANCE_INTERVAL_SECONDS`
- `AIBRIDGE_WS_URL`, `AIBRIDGE_HTTP_URL` for CLI endpoint overrides

The bundled extension currently uses the default loopback ports.

## Development

### Python compatibility and tests

The code must run on Python 3.11, the minimum version `scripts/install.ps1` accepts. Avoid syntax that only Python 3.12+ accepts, such as backslashes or reused quote characters inside f-string `{...}` expressions (PEP 701). Check with:

```
python -m compileall -q -f aibridge tests scripts
python -m unittest discover -s tests -v
```

## Uninstall

```powershell
.\scripts\uninstall.ps1 -RemovePath
```
