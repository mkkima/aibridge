from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor"


def configure_import_path() -> None:
    for path in (VENDOR, ROOT):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))


def open_task_logs() -> tuple[TextIO, TextIO]:
    candidates = []
    if local_app_data := os.getenv("LOCALAPPDATA"):
        candidates.append(Path(local_app_data) / "AIBridge" / "logs")
    candidates.append(Path(tempfile.gettempdir()) / "AIBridge" / "logs")

    last_error: OSError | None = None
    for log_dir in candidates:
        stdout: TextIO | None = None
        stderr: TextIO | None = None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout = (log_dir / "bridge-task.out.log").open(
                "a", encoding="utf-8", buffering=1
            )
            stderr = (log_dir / "bridge-task.err.log").open(
                "a", encoding="utf-8", buffering=1
            )
            return stdout, stderr
        except OSError as exc:
            last_error = exc
            if stdout is not None:
                stdout.close()
            if stderr is not None:
                stderr.close()

    assert last_error is not None
    raise last_error


def run_bridge() -> int:
    configure_import_path()

    from .lifecycle import is_healthy

    if is_healthy():
        return 0

    from .broker import main as bridge_main

    asyncio.run(bridge_main())
    return 0


def main() -> int:
    stdout: TextIO | None = None
    stderr: TextIO | None = None
    try:
        os.chdir(ROOT)
        stdout, stderr = open_task_logs()
        sys.stdout = stdout
        sys.stderr = stderr
        return run_bridge()
    except BaseException:
        if stderr is not None:
            traceback.print_exc(file=stderr)
            stderr.flush()
        return 1
    finally:
        if stdout is not None:
            stdout.flush()
        if stderr is not None:
            stderr.flush()


if __name__ == "__main__":
    raise SystemExit(main())
