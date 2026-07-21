"""openHAB events WebSocket client: connect, heartbeat, reconnect."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiohttp

from .api import OpenHabAuthError, OpenHabClient
from .const import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    FILTER_TOPIC_TOPIC,
    FILTER_TYPE_TOPIC,
    HEARTBEAT_INTERVAL,
    HEARTBEAT_TOPIC,
    SUBSCRIBED_EVENT_TYPES,
    WS_EVENT_TYPE,
)

_LOGGER = logging.getLogger(__name__)

# Filtering happens server-side, per exposed item. On a large openHAB install
# a bare "openhab/items/*" would push every state change for every item into
# Home Assistant's event loop, where all but a few percent would be decoded
# (twice -- the payload is nested JSON) only to be thrown away.
#
# openHAB replaces the topic filter wholesale each time it is sent, and we
# send it on every connect, so the filter cannot drift from the exposed set.
ITEM_TOPIC_WILDCARD = "openhab/items/*"

# Guard against an unreasonably large filter payload. Well past any sane
# number of exposed items; if it is ever hit, the wildcard plus client-side
# filtering still produces correct behaviour, just noisier.
MAX_TOPIC_FILTERS = 500


def item_topic_filters(item_names: list[str]) -> list[str]:
    """Topic patterns covering state, command and registry events per item."""
    if not item_names or len(item_names) > MAX_TOPIC_FILTERS:
        return [ITEM_TOPIC_WILDCARD]
    return [f"openhab/items/{name}/*" for name in sorted(item_names)]


@dataclass
class OpenHabEvent:
    """A decoded openHAB item event."""

    type: str
    topic: str
    payload: dict | str | None
    source: str | None = None

    @property
    def item_name(self) -> str | None:
        """Item name from an ``openhab/items/{name}/...`` topic."""
        parts = self.topic.split("/")
        if len(parts) >= 3 and parts[1] == "items":
            return parts[2]
        return None

    @property
    def state_value(self) -> str | None:
        """The state/command string carried by this event, if any."""
        if isinstance(self.payload, dict):
            value = self.payload.get("value")
            return None if value is None else str(value)
        return None

    @property
    def old_value(self) -> str | None:
        """The previous state, present only on ItemStateChangedEvent."""
        if isinstance(self.payload, dict):
            value = self.payload.get("oldValue")
            return None if value is None else str(value)
        return None


@dataclass
class ConnectionStats:
    """Connection health, surfaced through the diagnostic entities."""

    connected: bool = False
    last_connected: datetime | None = None
    last_event: datetime | None = None
    reconnect_attempts: int = 0
    last_error: str | None = None
    at_ceiling_since: float | None = field(default=None, repr=False)

    @property
    def seconds_at_ceiling(self) -> float:
        """How long the backoff has been stuck at its maximum."""
        if self.at_ceiling_since is None:
            return 0.0
        return time.monotonic() - self.at_ceiling_since


class OpenHabWebsocket:
    """Maintains a live connection to ``/ws/events``.

    Events that arrive while ``on_connected`` is still running are buffered and
    replayed afterwards. ``on_connected`` re-reads state over REST, so without
    buffering a stale REST snapshot could land on top of a fresher event.
    """

    def __init__(
        self,
        client: OpenHabClient,
        session: aiohttp.ClientSession,
        *,
        on_event: Callable[[OpenHabEvent], None],
        topic_filters: Callable[[], list[str]],
        task_factory: Callable[[Coroutine, str], asyncio.Task],
        on_connected: Callable[[], Awaitable[None]],
        on_disconnected: Callable[[], None],
        on_auth_failed: Callable[[str], None],
        on_unsupported: Callable[[], None],
    ) -> None:
        """Wire up the callbacks the coordinator supplies."""
        self._client = client
        self._session = session
        self._on_event = on_event
        self._topic_filters = topic_filters
        self._task_factory = task_factory
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_auth_failed = on_auth_failed
        self._on_unsupported = on_unsupported

        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._closing = False
        self._buffer: list[OpenHabEvent] | None = None
        self.stats = ConnectionStats()
        # Lets setup wait for the first connection, so entities are not
        # briefly unavailable while the socket is still being established.
        self.connected_event = asyncio.Event()

    def start(self) -> None:
        """Begin connecting, and keep reconnecting until stopped."""
        if self._task is None or self._task.done():
            self._closing = False
            # Created through Home Assistant so it is tracked and cancelled on
            # shutdown, rather than outliving the event loop.
            self._task = self._task_factory(self._run(), "openhab_bridge_ws")

    async def stop(self) -> None:
        """Close the socket and stop reconnecting."""
        self._closing = True
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await self._task
            self._task = None
        self.connected_event.clear()

    async def _run(self) -> None:
        backoff = BACKOFF_INITIAL
        while not self._closing:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except (_AuthFailedError, OpenHabAuthError) as err:
                # No amount of retrying fixes a bad token; hand off to reauth.
                self.stats.last_error = str(err)
                self._mark_disconnected()
                self._on_auth_failed(str(err))
                return
            except _UnsupportedError:
                self.stats.last_error = "openHAB has no /ws/events endpoint"
                self._mark_disconnected()
                self._on_unsupported()
                return
            except Exception as err:
                self.stats.last_error = str(err)
                _LOGGER.debug("openHAB WebSocket dropped: %s", err)
            finally:
                self._mark_disconnected()

            if self._closing:
                return

            self.stats.reconnect_attempts += 1
            if backoff >= BACKOFF_MAX and self.stats.at_ceiling_since is None:
                self.stats.at_ceiling_since = time.monotonic()
            # Jitter stops every HA instance retrying in lockstep after an
            # openHAB restart.
            delay = min(backoff, BACKOFF_MAX) * (0.8 + random.random() * 0.4)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _connect_and_read(self) -> None:
        url = self._client.websocket_url()
        _LOGGER.debug("Connecting to openHAB WebSocket at %s", url.with_query(None))
        try:
            ws = await self._session.ws_connect(
                url,
                ssl=self._client.verify_ssl,
                heartbeat=None,  # openHAB wants its own JSON heartbeat
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise _AuthFailedError("openHAB rejected the API token") from err
            if err.status == 404:
                raise _UnsupportedError from err
            raise

        self._ws = ws
        self._buffer = []
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        reader: asyncio.Task | None = None
        try:
            await self._send_filters(ws)
            reader = asyncio.create_task(self._read(ws))

            # Mark connected before resyncing: the resync dispatches state
            # updates, and entities check this flag to decide availability.
            self.stats.connected = True
            self.stats.last_connected = datetime.now(UTC)
            self.stats.reconnect_attempts = 0
            self.stats.at_ceiling_since = None
            self.stats.last_error = None
            self.connected_event.set()

            # Resync from REST first, then replay anything that arrived while
            # we were doing it, so a stale snapshot cannot land on top of a
            # fresher event.
            await self._on_connected()
            buffered, self._buffer = self._buffer or [], None
            for event in buffered:
                self._on_event(event)

            await reader
        finally:
            # Cleanup runs on cancellation too, including at Home Assistant
            # shutdown when the loop may already be going away. Nothing here
            # may raise, or the cancellation turns into a stray task error.
            for task in (heartbeat, reader):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                    await task
            self._buffer = None
            self._ws = None
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.close()

    async def _send_filters(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_json(
            {
                "type": WS_EVENT_TYPE,
                "topic": FILTER_TYPE_TOPIC,
                "payload": json.dumps(SUBSCRIBED_EVENT_TYPES),
            }
        )
        # Rebuilt from the current exposed set on every connect, so adding or
        # removing items never needs a Home Assistant restart.
        patterns = self._topic_filters()
        _LOGGER.debug("Subscribing to %d openHAB topic patterns", len(patterns))
        await ws.send_json(
            {
                "type": WS_EVENT_TYPE,
                "topic": FILTER_TOPIC_TOPIC,
                "payload": json.dumps(patterns),
            }
        )

    async def _heartbeat(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """openHAB closes idle connections after 10s."""
        while not ws.closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if ws.closed:
                return
            with contextlib.suppress(Exception):
                await ws.send_json(
                    {
                        "type": WS_EVENT_TYPE,
                        "topic": HEARTBEAT_TOPIC,
                        "payload": "PING",
                    }
                )

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type is aiohttp.WSMsgType.TEXT:
                self._handle_text(message.data)
            elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    def _handle_text(self, raw: str) -> None:
        try:
            envelope = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring non-JSON WebSocket frame: %s", raw[:200])
            return

        topic = envelope.get("topic", "")
        if topic == HEARTBEAT_TOPIC:
            return

        event = OpenHabEvent(
            type=envelope.get("type", ""),
            topic=topic,
            payload=_decode_payload(envelope.get("payload")),
            source=envelope.get("source"),
        )
        if not event.item_name:
            return

        self.stats.last_event = datetime.now(UTC)
        if self._buffer is not None:
            self._buffer.append(event)
        else:
            self._on_event(event)

    def _mark_disconnected(self) -> None:
        self.connected_event.clear()
        if self.stats.connected:
            self.stats.connected = False
            self._on_disconnected()
        else:
            self.stats.connected = False


def _decode_payload(payload: object) -> dict | str | None:
    """openHAB double-encodes payloads: a JSON string inside JSON."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except ValueError:
            return payload
        return decoded if isinstance(decoded, (dict, str)) else str(decoded)
    return str(payload)


class _AuthFailedError(Exception):
    """The WebSocket handshake was rejected."""


class _UnsupportedError(Exception):
    """This openHAB has no events WebSocket."""
