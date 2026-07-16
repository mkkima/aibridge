from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from aibridge import broker, lifecycle, task_runner


class TaskRunnerTests(unittest.TestCase):
    def test_healthy_bridge_is_not_started_again(self) -> None:
        with (
            mock.patch.object(lifecycle, "is_healthy", return_value=True),
            mock.patch.object(asyncio, "run") as asyncio_run,
        ):
            result = task_runner.run_bridge()

        self.assertEqual(result, 0)
        asyncio_run.assert_not_called()

    def test_unhealthy_bridge_runs_in_current_process(self) -> None:
        async def bridge_main() -> None:
            return None

        with (
            mock.patch.object(lifecycle, "is_healthy", return_value=False),
            mock.patch.object(broker, "main", side_effect=bridge_main) as main,
        ):
            result = task_runner.run_bridge()

        self.assertEqual(result, 0)
        main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
