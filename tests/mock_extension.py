from __future__ import annotations

import asyncio
import json

import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8765") as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "BRIDGE_READY",
                    "source": "mock",
                    "sources": ["mock"],
                }
            )
        )
        async for raw_message in websocket:
            payload = json.loads(raw_message)
            if payload.get("type") != "COMPLETION_REQUEST":
                continue
            await websocket.send(
                json.dumps(
                    {
                        "type": "FINAL",
                        "id": payload["id"],
                        "source": "mock",
                        "content": "AIBRIDGE_E2E_OK",
                    }
                )
            )
            return


if __name__ == "__main__":
    asyncio.run(main())

