from __future__ import annotations

import asyncio

from .cli import run


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))

