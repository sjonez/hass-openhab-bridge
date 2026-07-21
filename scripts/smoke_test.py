r"""Read-only smoke test against a real openHAB server.

Validates the protocol assumptions this integration is built on, none of which
the unit tests can cover because they mock the client entirely:

  * the REST item list and the shape of the autoupdate metadata
  * whether every item type in use maps to a Home Assistant platform
  * the events WebSocket: handshake, filters, heartbeat, payload encoding
  * that the per-item topic filter really is applied server-side

This script never writes: it sends no commands and no state updates.

Credentials come from a gitignored .env file in the repository root, or from
the environment if you prefer. The token is never printed by this script.

    # .env
    OPENHAB_URL=http://openhab.local:8080
    OPENHAB_TOKEN=your-api-token
    OPENHAB_VERIFY_SSL=0    # only if using HTTPS with a self-signed cert

    .venv\\Scripts\\python scripts\\smoke_test.py [--seconds 60]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from openhab_bridge.api import OpenHabClient
from openhab_bridge.const import (
    DEFAULT_PLATFORM,
    OH_GROUP,
    base_item_type,
    default_platform_for,
)
from openhab_bridge.websocket import item_topic_filters

OK = "  ok  "
WARN = " warn "
FAIL = " FAIL "


def report(status: str, message: str) -> None:
    """Print one check result."""
    print(f"[{status}] {message}")


def load_env_file() -> None:
    """Load KEY=VALUE lines from a gitignored .env in the repository root.

    Deliberately minimal, and it never echoes what it reads. Existing
    environment variables win, so an explicit `set` overrides the file.
    """
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    print("Loaded credentials from .env")


async def check_rest(client: OpenHabClient) -> list:
    """Item list, autoupdate metadata and type coverage."""
    print("\n== REST ==")
    items = await client.async_get_items()
    report(OK, f"fetched {len(items)} items")

    without_label = [item for item in items if not item.label]
    report(OK, f"{len(items) - len(without_label)} items have a label")

    no_autoupdate = [item for item in items if not item.autoupdate]
    if all(not item.autoupdate for item in items):
        report(
            WARN,
            "every item reports autoupdate=false -- the metadata query probably "
            "failed and the client fell back to its cautious default. Check the "
            "token has metadata read rights.",
        )
    else:
        report(
            OK,
            f"autoupdate metadata parsed: {len(no_autoupdate)} of {len(items)} "
            "items have autoupdate disabled",
        )
        for item in no_autoupdate[:10]:
            print(f"         autoupdate=false: {item.name} ({item.type})")

    # The important one on a large install: any openHAB type this integration
    # does not know about falls back to a plain sensor, which may be wrong.
    types = Counter(item.type for item in items)
    unmapped = {
        item_type: count
        for item_type, count in types.items()
        if base_item_type(item_type) not in DEFAULT_PLATFORM
        and base_item_type(item_type) != OH_GROUP
    }
    if unmapped:
        report(WARN, f"{len(unmapped)} item type(s) have no explicit mapping:")
        for item_type, count in sorted(unmapped.items(), key=lambda kv: -kv[1]):
            print(f"         {item_type}: {count} item(s) -> falling back to sensor")
    else:
        report(OK, "every item type in use maps to a platform explicitly")

    print("\n         type distribution (top 15):")
    for item_type, count in types.most_common(15):
        print(
            f"         {count:>5}  {item_type:<28} -> {default_platform_for(item_type)}"
        )

    return items


async def _heartbeat(ws: aiohttp.ClientWebSocketResponse) -> None:
    """openHAB closes idle connections after 10s."""
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


async def _send_filters(
    ws: aiohttp.ClientWebSocketResponse, patterns: list[str]
) -> None:
    await ws.send_json(
        {
            "type": "WebSocketEvent",
            "topic": "openhab/websocket/filter/type",
            "payload": json.dumps(
                ["ItemStateEvent", "ItemStateChangedEvent", "ItemRemovedEvent"]
            ),
        }
    )
    await ws.send_json(
        {
            "type": "WebSocketEvent",
            "topic": "openhab/websocket/filter/topic",
            "payload": json.dumps(patterns),
        }
    )


async def _collect(
    ws: aiohttp.ClientWebSocketResponse, seconds: int, sample: bool
) -> tuple[Counter, bool | None, bool, int]:
    """Listen for `seconds`, returning per-item counts and payload findings.

    Only frames on openhab/items/... count as events -- openHAB echoes the
    filter messages straight back, and mistaking one of those for an item
    event would "verify" the payload encoding against our own message.
    """
    seen: Counter = Counter()
    double_encoded: bool | None = None
    pong = False
    closed_early = 0
    printed = False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        try:
            message = await asyncio.wait_for(
                ws.receive(), timeout=max(deadline - loop.time(), 0.1)
            )
        except TimeoutError:
            break
        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            closed_early = int(loop.time() - (deadline - seconds))
            break
        if message.type is not aiohttp.WSMsgType.TEXT:
            continue

        envelope = json.loads(message.data)
        topic = envelope.get("topic", "")
        if topic == "openhab/websocket/heartbeat":
            pong = pong or envelope.get("payload") == "PONG"
            continue

        parts = topic.split("/")
        if len(parts) < 3 or parts[1] != "items":
            continue  # filter echoes and anything else non-item

        seen[parts[2]] += 1
        if double_encoded is None:
            double_encoded = isinstance(envelope.get("payload"), str)
        if sample and not printed:
            printed = True
            print("\n         a real item event:")
            print(f"         type    : {envelope.get('type')}")
            print(f"         topic   : {topic}")
            print(f"         payload : {envelope.get('payload')!r}")
            print(f"         source  : {envelope.get('source')!r}")

    return seen, double_encoded, pong, closed_early


async def _open(
    client: OpenHabClient, session: aiohttp.ClientSession
) -> aiohttp.ClientWebSocketResponse | None:
    try:
        return await session.ws_connect(
            client.websocket_url(), ssl=client.verify_ssl, heartbeat=None
        )
    except aiohttp.WSServerHandshakeError as err:
        report(FAIL, f"handshake rejected with HTTP {err.status}")
        if err.status == 404:
            print("         no /ws/events endpoint -- openHAB 4.0+ is required")
        if err.status in (401, 403):
            print("         the token was rejected for the WebSocket")
        return None


async def check_websocket(
    client: OpenHabClient,
    session: aiohttp.ClientSession,
    total_items: int,
    seconds: int,
    watch_override: list[str],
) -> None:
    """Two phases: measure the firehose, then prove the per-item filter works."""
    print("\n== WebSocket phase 1: how much traffic is there? ==")
    ws = await _open(client, session)
    if ws is None:
        return
    report(OK, "connected to /ws/events with the token as a query parameter")

    beat = asyncio.create_task(_heartbeat(ws))
    await _send_filters(ws, ["openhab/items/*"])
    report(OK, "subscribed to ALL items (openhab/items/*)")
    print(f"         listening for {seconds}s ...")

    busiest, encoded, pong, closed = await _collect(ws, seconds, sample=True)
    beat.cancel()
    await ws.close()

    print()
    if closed:
        report(
            FAIL,
            f"socket closed after ~{closed}s despite heartbeats -- "
            "the 5s heartbeat interval may not be enough",
        )
    else:
        report(OK, f"socket stayed open for the full {seconds}s with 5s heartbeats")
    if pong:
        report(OK, "heartbeat PING answered with PONG")

    total_events = sum(busiest.values())
    rate = total_events / seconds
    report(
        OK,
        f"{total_events} item events from {len(busiest)} distinct items "
        f"({rate:.1f}/s across {total_items} items)",
    )
    if encoded is True:
        report(OK, "payload is a JSON-encoded string inside the envelope, as assumed")
    elif encoded is False:
        report(FAIL, "payload was NOT double-encoded -- _decode_payload assumes it is")
    else:
        report(WARN, "no item events arrived; try a longer --seconds")

    if busiest:
        print("\n         busiest items:")
        for name, count in busiest.most_common(10):
            print(f"         {count:>5}  {name}")

    # Phase 2: subscribe to only the chattiest items. If the server-side filter
    # works, everything else must disappear.
    watch = watch_override or [name for name, _ in busiest.most_common(10)]
    if not watch:
        report(WARN, "no traffic seen, so the topic filter could not be tested")
        return

    print("\n== WebSocket phase 2: does the per-item filter actually filter? ==")
    ws = await _open(client, session)
    if ws is None:
        return
    beat = asyncio.create_task(_heartbeat(ws))
    patterns = item_topic_filters(watch)
    await _send_filters(ws, patterns)
    report(OK, f"subscribed to {len(patterns)} specific item(s)")
    print(f"         listening for {seconds}s ...")

    filtered, _encoded, _pong, closed = await _collect(ws, seconds, sample=False)
    beat.cancel()
    await ws.close()

    print()
    leaked = {name: count for name, count in filtered.items() if name not in watch}
    if leaked:
        report(
            FAIL,
            f"filter LEAKED: {sum(leaked.values())} event(s) for unwatched items, "
            f"e.g. {sorted(leaked)[:5]}",
        )
    else:
        report(OK, "no events for unwatched items -- server-side filter works")

    kept = sum(filtered.values())
    if total_events:
        saved = 100 * (1 - (kept / seconds) / max(rate, 0.001))
        report(
            OK,
            f"{kept} events kept vs {total_events} on the firehose "
            f"-- the per-item filter discards ~{saved:.0f}% of traffic",
        )


async def main() -> int:
    """Run the checks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=45)
    parser.add_argument(
        "--watch",
        default="",
        help="comma-separated item names to subscribe to; "
        "default picks a few that change often",
    )
    args = parser.parse_args()

    load_env_file()
    url = os.environ.get("OPENHAB_URL")
    token = os.environ.get("OPENHAB_TOKEN")
    if not url or not token:
        print(
            "No credentials found. Create a .env file in the repository root with:\n"
            "  OPENHAB_URL=http://openhab.local:8080\n"
            "  OPENHAB_TOKEN=your-api-token"
        )
        return 2

    verify_ssl = os.environ.get("OPENHAB_VERIFY_SSL", "1") != "0"
    print(f"openHAB: {url}  (verify_ssl={verify_ssl})")

    async with aiohttp.ClientSession() as session:
        client = OpenHabClient(session, url, token, verify_ssl)
        try:
            items = await check_rest(client)
        except Exception as err:
            report(FAIL, f"REST checks failed: {type(err).__name__}: {err}")
            return 1

        watch = [name.strip() for name in args.watch.split(",") if name.strip()]

        try:
            await check_websocket(client, session, len(items), args.seconds, watch)
        except Exception as err:
            report(FAIL, f"WebSocket checks failed: {type(err).__name__}: {err}")
            return 1

    print("\nDone. No commands or state updates were sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
