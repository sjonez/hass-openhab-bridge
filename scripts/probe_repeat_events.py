r"""Probe what openHAB emits when a command or update does not change state.

Answers a question the integration's behaviour depends on: if an item is
already ON and receives ON again, is anything observable on the event stream?
If nothing is emitted, no Home Assistant automation can ever trigger on it.

Writes ONLY to the item named by OPENHAB_TEST_ITEM, and restores its original
state at the end.

    .venv\Scripts\python scripts\probe_repeat_events.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from openhab_bridge.api import OpenHabClient

sys.path.insert(0, os.path.dirname(__file__))
from smoke_test import load_env_file

# Everything openHAB might send for an item, so nothing is missed.
ALL_TYPES = [
    "ItemStateEvent",
    "ItemStateChangedEvent",
    "ItemStatePredictedEvent",
    "ItemStateUpdatedEvent",
    "ItemCommandEvent",
]


async def heartbeat(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Keep the connection alive past openHAB's 10s idle timeout."""
    while not ws.closed:
        await asyncio.sleep(5)
        if ws.closed:
            return
        try:
            await ws.send_json(
                {
                    "type": "WebSocketEvent",
                    "topic": "openhab/websocket/heartbeat",
                    "payload": "PING",
                }
            )
        except Exception:
            return


async def main() -> int:
    """Post the same value repeatedly and record what comes back."""
    load_env_file()
    url = os.environ.get("OPENHAB_URL")
    token = os.environ.get("OPENHAB_TOKEN")
    item = os.environ.get("OPENHAB_TEST_ITEM")
    verify_ssl = os.environ.get("OPENHAB_VERIFY_SSL", "1") != "0"
    if not (url and token and item):
        print("Need OPENHAB_URL, OPENHAB_TOKEN and OPENHAB_TEST_ITEM in .env")
        return 2

    frames: list[tuple[str, str, str]] = []

    async with aiohttp.ClientSession() as session:
        client = OpenHabClient(session, url, token, verify_ssl)
        original = await client.async_get_state(item)
        print(f"item      : {item}")
        print(f"state now : {original!r}\n")

        ws = await session.ws_connect(
            client.websocket_url(), ssl=verify_ssl, heartbeat=None
        )
        beat = asyncio.create_task(heartbeat(ws))
        await ws.send_json(
            {
                "type": "WebSocketEvent",
                "topic": "openhab/websocket/filter/type",
                "payload": json.dumps(ALL_TYPES),
            }
        )
        await ws.send_json(
            {
                "type": "WebSocketEvent",
                "topic": "openhab/websocket/filter/topic",
                "payload": json.dumps([f"openhab/items/{item}/*"]),
            }
        )

        stop = asyncio.Event()

        async def reader() -> None:
            async for message in ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                envelope = json.loads(message.data)
                topic = envelope.get("topic", "")
                if "/items/" not in topic:
                    continue
                frames.append(
                    (
                        datetime.now().strftime("%H:%M:%S.%f")[:-3],
                        envelope.get("type", "?"),
                        f"{topic.rsplit('/', 1)[-1]} = {envelope.get('payload')}",
                    )
                )

        read_task = asyncio.create_task(reader())
        await asyncio.sleep(1)

        async def step(label: str, action) -> None:
            print(f"--- {label} ---")
            marker = len(frames)
            await action()
            await asyncio.sleep(3)
            new = frames[marker:]
            if not new:
                print("    (nothing emitted)\n")
            for when, kind, detail in new:
                print(f"    {when}  {kind:<26} {detail}")
            print()

        opposite = "OFF" if original.upper() == "ON" else "ON"

        await step(
            f"postUpdate {opposite} (a real change)",
            lambda: client.async_post_update(item, opposite),
        )
        await step(
            f"postUpdate {opposite} again (SAME value)",
            lambda: client.async_post_update(item, opposite),
        )
        await step(
            f"sendCommand {opposite} (SAME value as current state)",
            lambda: client.async_send_command(item, opposite),
        )

        stop.set()
        beat.cancel()
        read_task.cancel()
        await ws.close()

        print(f"--- restoring {item} to {original!r} ---")
        await client.async_post_update(item, original)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
