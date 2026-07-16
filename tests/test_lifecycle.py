from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aibridge import lifecycle


def run_lock_child(lock_path: Path, events_path: Path) -> int:
    deadline = time.monotonic() + 10
    with lifecycle.StartupLock(lock_path) as startup_lock:
        while not startup_lock.try_acquire():
            if time.monotonic() >= deadline:
                return 1
            time.sleep(0.01)

        with events_path.open("a", encoding="utf-8") as events:
            events.write("enter\n")
            events.flush()
            time.sleep(0.05)
            events.write("exit\n")
            events.flush()
    return 0


class LifecycleTests(unittest.TestCase):
    def test_fast_path_does_not_start_bridge(self) -> None:
        with (
            mock.patch.object(lifecycle, "is_healthy", return_value=True),
            mock.patch.object(lifecycle, "start_scheduled_bridge") as start_task,
            mock.patch.object(lifecycle, "spawn_bridge") as spawn_bridge,
        ):
            lifecycle.ensure_bridge()

        start_task.assert_not_called()
        spawn_bridge.assert_not_called()

    def test_on_demand_task_is_preferred_over_direct_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                mock.patch.object(lifecycle, "is_healthy", side_effect=[False, False]),
                mock.patch.object(
                    lifecycle,
                    "startup_lock_path",
                    return_value=Path(temp_dir) / "bridge.lock",
                ),
                mock.patch.object(
                    lifecycle,
                    "start_scheduled_bridge",
                    return_value=True,
                ) as start_task,
                mock.patch.object(lifecycle, "wait_until_healthy", return_value=True),
                mock.patch.object(lifecycle, "spawn_bridge") as spawn_bridge,
            ):
                lifecycle.ensure_bridge()

        start_task.assert_called_once_with()
        spawn_bridge.assert_not_called()

    def test_startup_lock_serializes_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "bridge.lock"
            events_path = Path(temp_dir) / "events.log"
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--lock-child",
                        str(lock_path),
                        str(events_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(8)
            ]

            results = [process.communicate(timeout=15) for process in processes]
            for process, (stdout, stderr) in zip(processes, results):
                self.assertEqual(process.returncode, 0, (stdout, stderr))

            active = 0
            max_active = 0
            events = events_path.read_text(encoding="utf-8").splitlines()
            for event in events:
                if event == "enter":
                    active += 1
                    max_active = max(max_active, active)
                elif event == "exit":
                    active -= 1

            self.assertEqual(len(events), len(processes) * 2)
            self.assertEqual(max_active, 1)
            self.assertEqual(active, 0)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--lock-child":
    raise SystemExit(run_lock_child(Path(sys.argv[2]), Path(sys.argv[3])))


if __name__ == "__main__":
    unittest.main()
