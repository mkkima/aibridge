from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = os.getenv("AIBRIDGE_HOST", "127.0.0.1")
WS_PORT = int(os.getenv("AIBRIDGE_WS_PORT", "8765"))
HTTP_PORT = int(os.getenv("AIBRIDGE_HTTP_PORT", "8766"))
HEALTH_URL = f"http://{HOST}:{HTTP_PORT}/health"
READY_TIMEOUT_SECONDS = float(os.getenv("AIBRIDGE_READY_TIMEOUT_SECONDS", "10"))
LOCK_POLL_SECONDS = 0.1
WINDOWS_TASK_NAME = os.getenv("AIBRIDGE_TASK_NAME", "AI Bridge")
WINDOWS_TASK_READY_SECONDS = 5.0


class BridgeEnsureError(RuntimeError):
    pass


class StartupLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        self._acquired = False

    def __enter__(self) -> StartupLock:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise BridgeEnsureError(
                f"Failed to open AI Bridge startup lock at {self._path}: {exc}"
            ) from exc
        os.set_inheritable(self._fd, False)

        if os.fstat(self._fd).st_size == 0:
            os.write(self._fd, b"\0")
        os.lseek(self._fd, 0, os.SEEK_SET)
        return self

    def try_acquire(self) -> bool:
        if self._fd is None:
            raise RuntimeError("Startup lock is not open")
        if self._acquired:
            return True

        os.lseek(self._fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise BridgeEnsureError(
                    f"Failed to acquire AI Bridge startup lock: {exc}"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    return False
                raise BridgeEnsureError(
                    f"Failed to acquire AI Bridge startup lock: {exc}"
                ) from exc

        self._acquired = True
        return True

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self._fd is None:
            return

        try:
            if self._acquired:
                os.lseek(self._fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    with contextlib.suppress(OSError):
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    with contextlib.suppress(OSError):
                        fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            self._acquired = False


def startup_lock_path() -> Path:
    endpoint = f"{HOST.casefold()}:{WS_PORT}:{HTTP_PORT}"
    digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
    base = Path(tempfile.gettempdir())
    return base / "AIBridge" / "locks" / f"bridge-{digest}.lock"


def is_healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.0) as response:
            if response.status < 200 or response.status >= 300:
                return False

            body = response.read().decode("utf-8", errors="replace").strip()
            if not body:
                return True

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return True

            if isinstance(payload, dict):
                service = payload.get("service")
                return service in (None, "aibridge") and payload.get("ok", True) is not False

            return True
    except Exception:
        return False


def pythonw_or_current() -> str:
    if os.name != "nt":
        return sys.executable

    if "codex-runtimes" in sys.executable.lower():
        return sys.executable

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw) if pythonw.exists() else sys.executable


def start_scheduled_bridge() -> bool:
    if os.name != "nt":
        return False

    try:
        result = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", WINDOWS_TASK_NAME],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def open_bridge_logs():
    log_directories = [
        ROOT / "logs",
        Path(tempfile.gettempdir()) / "AIBridge" / "logs",
    ]
    last_error: OSError | None = None

    for log_dir in log_directories:
        stdout = None
        stderr = None
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout = (log_dir / "aibridge.out.log").open("ab")
            stderr = (log_dir / "aibridge.err.log").open("ab")
            return stdout, stderr
        except OSError as exc:
            last_error = exc
            if stdout is not None:
                stdout.close()
            if stderr is not None:
                stderr.close()

    assert last_error is not None
    raise last_error


def spawn_bridge() -> subprocess.Popen[bytes]:
    creationflags = 0
    startupinfo = None

    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    stdout, stderr = open_bridge_logs()
    try:
        command = [pythonw_or_current(), "-m", "aibridge.broker"]
        options = {
            "cwd": str(ROOT),
            "stdin": subprocess.DEVNULL,
            "stdout": stdout,
            "stderr": stderr,
            "creationflags": creationflags,
            "startupinfo": startupinfo,
            "close_fds": True,
        }

        try:
            return subprocess.Popen(command, **options)
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 87):
                raise

            # Some Windows job objects forbid breakaway. Normal terminals do not
            # need it, so retry with the standard detached flags.
            options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
            return subprocess.Popen(command, **options)
    finally:
        stdout.close()
        stderr.close()


def wait_until_healthy(
    deadline: float,
    process: subprocess.Popen[bytes] | None = None,
) -> bool:
    while time.monotonic() < deadline:
        if is_healthy():
            return True
        if process is not None and process.poll() is not None:
            return False
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return is_healthy()


def ensure_bridge() -> None:
    if is_healthy():
        return

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    with StartupLock(startup_lock_path()) as startup_lock:
        while not startup_lock.try_acquire():
            if is_healthy():
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeEnsureError(
                    "Timed out waiting for another process to start AI Bridge at "
                    f"{HEALTH_URL}."
                )
            time.sleep(min(LOCK_POLL_SECONDS, remaining))

        # Another process may have completed startup while this process waited.
        if is_healthy():
            return

        if start_scheduled_bridge():
            task_deadline = min(
                deadline,
                time.monotonic() + WINDOWS_TASK_READY_SECONDS,
            )
            if wait_until_healthy(task_deadline):
                return

        try:
            process = spawn_bridge()
        except OSError as exc:
            raise BridgeEnsureError(f"Failed to start AI Bridge: {exc}") from exc

        if wait_until_healthy(deadline, process):
            return

        exit_code = process.poll()
        exit_detail = f" Child exit code: {exit_code}." if exit_code is not None else ""
        raise BridgeEnsureError(
            "AI Bridge did not become ready at "
            f"{HEALTH_URL}.{exit_detail} Another process may be using ports "
            f"{WS_PORT}/{HTTP_PORT}, or aibridge.broker failed to start. "
            "See logs/aibridge.err.log."
        )


def main() -> int:
    try:
        ensure_bridge()
        return 0
    except BridgeEnsureError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
