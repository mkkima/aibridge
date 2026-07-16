from __future__ import annotations

import asyncio
import sys

from .command import main as command_main, validate_without_bridge
from .lifecycle import BridgeEnsureError, ensure_bridge


async def run() -> int:
    fast_exit_code = validate_without_bridge(sys.argv[1:])
    if fast_exit_code is not None:
        return fast_exit_code

    try:
        await asyncio.to_thread(ensure_bridge)
    except BridgeEnsureError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        await command_main()
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

