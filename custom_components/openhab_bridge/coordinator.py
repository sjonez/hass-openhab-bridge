"""Owns the openHAB connection, the state cache and all write safety rules."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from . import repairs
from .api import (
    OpenHabAuthError,
    OpenHabClient,
    OpenHabError,
    OpenHabItem,
    OpenHabNotFoundError,
)
from .const import (
    CONF_BASE_URL,
    CONF_ITEMS,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    EVENT_COMMAND_UNCONFIRMED,
    EVENT_ITEM_EVENT,
    EVENT_TYPE_CHANGED,
    EVENT_TYPE_COMMAND,
    NAME_SUFFIX,
    OSCILLATION_COOLDOWN,
    OSCILLATION_THRESHOLD,
    OSCILLATION_WINDOW,
    PARSE_FAILURE_REPAIR_THRESHOLD,
    PENDING_TIMEOUT,
    PENDING_TIMEOUT_NO_AUTOUPDATE,
    SIGNAL_CONNECTION,
    SIGNAL_LAST_EVENT,
    SIGNAL_STATE_UPDATED,
    UNCONFIRMED_REPAIR_THRESHOLD,
    UNUSABLE_STATES,
    platform_is_compatible,
)
from .websocket import OpenHabEvent, OpenHabWebsocket, item_topic_filters

_LOGGER = logging.getLogger(__name__)

STALE_ECHO_GRACE = 10.0
MAX_SUPERSEDED = 4
# How long setup waits for the first WebSocket connection before adding
# entities anyway.
INITIAL_CONNECT_TIMEOUT = 10.0
# A reconnect this soon after a resync does not need another one.
RESYNC_DEBOUNCE = 5.0


@dataclass(slots=True)
class PendingWrite:
    """A command we sent that openHAB has not echoed back yet."""

    value: str
    seq: int
    sent_at: float
    cancel: Any = None


@dataclass(slots=True)
class WriteGuard:
    """Per-item bookkeeping for the loop-safety rules."""

    writes: deque[tuple[float, str]] = field(default_factory=deque)
    suppressed_until: float = 0.0
    unconfirmed_streak: int = 0
    parse_failures: int = 0


class OpenHabCoordinator:
    """Single source of truth for one openHAB config entry.

    Invariant: state only ever flows *into* Home Assistant from openHAB
    events. Nothing in the inbound path issues a write, which is what stops
    the HA -> openHAB -> HA round trip from looping.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Set up the client and WebSocket for this entry."""
        self.hass = hass
        self.entry = entry
        session = async_get_clientsession(hass, verify_ssl=entry.data[CONF_VERIFY_SSL])
        self.client = OpenHabClient(
            session,
            entry.data[CONF_BASE_URL],
            entry.data[CONF_TOKEN],
            entry.data[CONF_VERIFY_SSL],
        )
        self.states: dict[str, str] = {}
        self.items: dict[str, OpenHabItem] = {}
        self.missing_items: set[str] = set()

        self._pending: dict[str, PendingWrite] = {}
        self._superseded: dict[str, list[tuple[str, float]]] = {}
        self._guards: dict[str, WriteGuard] = {}
        self._seq = 0
        self.unconfirmed_commands = 0
        self.last_unconfirmed: dict[str, str] | None = None
        self.looping_items: set[str] = set()

        self.websocket = OpenHabWebsocket(
            self.client,
            session,
            on_event=self._handle_event,
            topic_filters=self._topic_filters,
            task_factory=self._create_background_task,
            on_connected=self._handle_connected,
            on_disconnected=self._handle_disconnected,
            on_auth_failed=self._handle_auth_failed,
            on_unsupported=self._handle_unsupported,
        )
        self._auth_failed: str | None = None
        self._last_resync: float = 0.0
        self._last_command: dict[str, datetime] = {}

    @callback
    def last_command(self, item_name: str) -> datetime | None:
        """When this item last received a command, from anywhere."""
        return self._last_command.get(item_name)

    def _create_background_task(self, coro, name: str):
        """Hand a long-running task to Home Assistant to own and cancel.

        Anything spawned with a bare hass.async_create_task survives entry
        unload and delays Home Assistant's shutdown, which it warns about.
        """
        return self.entry.async_create_background_task(self.hass, coro, name)

    # -- configuration ----------------------------------------------------

    @property
    def configured_items(self) -> dict[str, dict[str, Any]]:
        """The item -> {platform, name} map from the entry options."""
        return dict(self.entry.options.get(CONF_ITEMS, {}))

    def _topic_filters(self) -> list[str]:
        """Server-side subscription list: only the items we actually expose."""
        return item_topic_filters(list(self.configured_items))

    def platform_for(self, item_name: str) -> str | None:
        """Configured HA platform for an item."""
        config = self.configured_items.get(item_name)
        return config.get("platform") if config else None

    def name_for(self, item_name: str) -> str:
        """Friendly name: the openHAB label (or an override) plus a suffix."""
        config = self.configured_items.get(item_name, {})
        override = config.get("name")
        if override:
            base = override
        else:
            item = self.items.get(item_name)
            base = (item.label if item and item.label else None) or item_name
        return f"{base} {NAME_SUFFIX}"

    def is_available(self, item_name: str) -> bool:
        """An entity is available when connected, present and holding a value."""
        if not self.websocket.stats.connected:
            return False
        if item_name in self.missing_items:
            return False
        state = self.states.get(item_name)
        return state is not None and state not in UNUSABLE_STATES

    def autoupdate(self, item_name: str) -> bool:
        """Whether openHAB predicts state for this item after a command."""
        item = self.items.get(item_name)
        return True if item is None else item.autoupdate

    # -- lifecycle --------------------------------------------------------

    async def async_setup(self) -> None:
        """Seed state from REST (openHAB wins), then start streaming."""
        try:
            await self.async_resync()
        except OpenHabAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OpenHabError as err:
            # openHAB may simply not be up yet; let HA retry the whole setup.
            raise ConfigEntryNotReady(str(err)) from err

        self.websocket.start()
        # Give the socket a moment to come up so entities are not added in an
        # unavailable state and then flip a beat later. We do not fail setup if
        # it times out -- REST already worked, so the entry is usable and the
        # reconnect loop will keep trying.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self.websocket.connected_event.wait(), INITIAL_CONNECT_TIMEOUT
            )

    async def async_shutdown(self) -> None:
        """Stop the WebSocket and cancel any pending write timers."""
        await self.websocket.stop()
        for pending in self._pending.values():
            if pending.cancel:
                pending.cancel()
        self._pending.clear()

    async def async_resync(self) -> None:
        """Re-read every configured item over REST. openHAB always wins."""
        self._last_resync = time.monotonic()
        items = await self.client.async_get_items()
        by_name = {item.name: item for item in items}
        configured = self.configured_items

        for name in configured:
            item = by_name.get(name)
            if item is None:
                self._mark_missing(name)
                continue
            self._mark_present(name)
            self.items[name] = item
            if item.state is not None:
                self._apply_state(name, item.state, from_resync=True)
            self._check_type_compatibility(name, item)

    # -- inbound events ---------------------------------------------------

    @callback
    def _handle_event(self, event: OpenHabEvent) -> None:
        """Apply an openHAB event. This path never writes back to openHAB."""
        name = event.item_name
        if name is None:
            return

        # Fired for every event regardless of item, so the "Last event"
        # diagnostic reflects the whole bus -- not just exposed items -- and
        # can actually reveal a silently dead socket.
        async_dispatcher_send(self.hass, SIGNAL_LAST_EVENT.format(self.entry.entry_id))

        if event.type == "ItemRemovedEvent":
            if name in self.configured_items:
                self._mark_missing(name)
            return

        if event.type in ("ItemAddedEvent", "ItemUpdatedEvent"):
            self._create_background_task(
                self._refresh_item(name), f"openhab_refresh_{name}"
            )
            return

        if name not in self.configured_items:
            return

        value = event.state_value
        if value is None:
            return

        if event.type == "ItemCommandEvent":
            # A command is an action, so it is worth surfacing whatever value
            # it carries. It never moves entity state by itself, and when the
            # item already holds the commanded value openHAB emits nothing
            # else at all, so this is the only way to see it.
            self._last_command[name] = dt_util.utcnow()
            self._fire_item_event(name, value, EVENT_TYPE_COMMAND)
            # Re-render the entity so its last_command attribute is live. The
            # state value is unchanged, so this updates last_updated but not
            # last_changed -- which is exactly right: nothing changed, but
            # something happened.
            async_dispatcher_send(
                self.hass,
                SIGNAL_STATE_UPDATED.format(self.entry.entry_id, name),
                self.states.get(name),
            )
            return

        if self._is_stale_echo(name, value):
            _LOGGER.debug("Dropping stale echo for %s: %s", name, value)
            return

        pending = self._pending.get(name)
        if pending is not None and pending.value == value:
            self._clear_pending(name, confirmed=True)

        self._apply_state(name, value)

        # openHAB sends both ItemStateEvent and ItemStateChangedEvent for a
        # real change, so key the event off the changed one only -- otherwise
        # every change would be reported twice. It is also the only one
        # carrying the previous value. An update repeating the value an item
        # already held produces neither, which is correct: nothing changed.
        if event.type == "ItemStateChangedEvent":
            self._fire_item_event(
                name, value, EVENT_TYPE_CHANGED, old_value=event.old_value
            )

    @callback
    def _fire_item_event(
        self, name: str, value: str, kind: str, old_value: str | None = None
    ) -> None:
        """Announce openHAB activity, mirroring openHAB's own distinction.

        openHAB rules separate "received command" from "changed"; automations
        here can do the same by filtering on ``type``.
        """
        pending = self._pending.get(name)
        data: dict[str, Any] = {
            "item": name,
            "type": kind,
            "value": value,
            "config_entry_id": self.entry.entry_id,
            # Lets automations ignore the echo of their own command and
            # avoid building a loop out of this event.
            "origin": (
                "home_assistant"
                if pending is not None and pending.value == value
                else "openhab"
            ),
        }
        if kind == EVENT_TYPE_CHANGED:
            data["old_value"] = old_value
        self.hass.bus.async_fire(EVENT_ITEM_EVENT, data)

    async def _refresh_item(self, name: str) -> None:
        """Re-read one item after openHAB reports it added or updated."""
        if name not in self.configured_items:
            return
        try:
            item = await self.client.async_get_item(name)
        except OpenHabNotFoundError:
            self._mark_missing(name)
            return
        except OpenHabError as err:
            _LOGGER.debug("Could not refresh %s: %s", name, err)
            return

        self._mark_present(name)
        self.items[name] = item
        self._check_type_compatibility(name, item)
        if item.state is not None:
            self._apply_state(name, item.state)

    @callback
    def _apply_state(self, name: str, value: str, from_resync: bool = False) -> None:
        """Update the cache and tell that one entity to refresh."""
        if self.states.get(name) == value and not from_resync:
            return
        self.states[name] = value
        async_dispatcher_send(
            self.hass, SIGNAL_STATE_UPDATED.format(self.entry.entry_id, name), value
        )

    async def _handle_connected(self) -> None:
        """Runs before buffered events are replayed, so REST cannot win late."""
        # Setup resyncs, then immediately starts the socket. Fetching the whole
        # item list again milliseconds later is pure waste on a large install.
        if time.monotonic() - self._last_resync > RESYNC_DEBOUNCE:
            try:
                await self.async_resync()
            except OpenHabAuthError:
                raise
            except OpenHabError as err:
                _LOGGER.warning("Resync after reconnect failed: %s", err)
        repairs.async_clear_unreachable(self.hass, self.entry)
        self._notify_connection()

    @callback
    def _handle_disconnected(self) -> None:
        self._notify_connection()

    @callback
    def _handle_auth_failed(self, message: str) -> None:
        self._auth_failed = message
        self._notify_connection()
        self.entry.async_start_reauth(self.hass)

    @callback
    def _handle_unsupported(self) -> None:
        repairs.async_raise_websocket_unsupported(self.hass, self.entry)
        self._notify_connection()

    @callback
    def _notify_connection(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_CONNECTION.format(self.entry.entry_id))

    # -- outbound writes --------------------------------------------------

    async def async_send_command(self, name: str, command: str) -> None:
        """sendCommand, with the loop-safety rules applied."""
        guard = self._guards.setdefault(name, WriteGuard())
        now = time.monotonic()

        if now < guard.suppressed_until:
            raise HomeAssistantError(
                f"Command to '{name}' suppressed: a feedback loop was detected. "
                "Check for an automation or openHAB rule commanding this item "
                "whenever it changes."
            )
        if guard.suppressed_until and name in self.looping_items:
            # Cooldown has expired: release the item and clear the warning.
            guard.suppressed_until = 0.0
            guard.writes.clear()
            self.looping_items.discard(name)
            repairs.async_clear_feedback_loop(self.hass, self.entry, name)
            self._notify_connection()

        # No redundant-write suppression: a command that matches the cached
        # state is still sent. Real devices drift out of sync with what
        # openHAB last reported -- a switch OH thinks is already ON may be
        # physically off -- and a user re-sending ON must reach the device
        # regardless. Loop safety against a genuine runaway (an automation
        # or openHAB rule re-triggering on every echo) rests entirely on the
        # oscillation detector below, which bounds the damage without ever
        # silently dropping a command a user asked for.
        if self._record_write(guard, now, command):
            self.looping_items.add(name)
            guard.suppressed_until = now + OSCILLATION_COOLDOWN
            _LOGGER.warning(
                "Feedback loop detected on openHAB item '%s': %d commands in %ds. "
                "Outbound commands are paused for %ds; inbound state is unaffected.",
                name,
                OSCILLATION_THRESHOLD,
                int(OSCILLATION_WINDOW),
                int(OSCILLATION_COOLDOWN),
            )
            repairs.async_raise_feedback_loop(self.hass, self.entry, name)
            # Drop in-flight and superseded bookkeeping for this item. Those
            # entries exist to swallow echoes of our own commands, and after a
            # burst there are enough of them to also swallow genuine updates
            # from openHAB -- which would break the promise that inbound state
            # keeps working while outbound is paused.
            self._clear_pending(name, confirmed=False)
            self._superseded.pop(name, None)
            self._notify_connection()
            return

        await self._write(name, command, is_command=True)

    async def async_post_update(self, name: str, state: str) -> None:
        """postUpdate: writes state directly, unaffected by autoupdate."""
        await self._write(name, state, is_command=False)

    async def _write(self, name: str, value: str, is_command: bool) -> None:
        self._supersede_pending(name)
        try:
            if is_command:
                await self.client.async_send_command(name, value)
            else:
                await self.client.async_post_update(name, value)
        except OpenHabAuthError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(str(err)) from err
        except OpenHabError as err:
            raise HomeAssistantError(f"Writing '{name}' failed: {err}") from err

        self._seq += 1
        timeout = (
            PENDING_TIMEOUT if self.autoupdate(name) else PENDING_TIMEOUT_NO_AUTOUPDATE
        )
        pending = PendingWrite(value=value, seq=self._seq, sent_at=time.monotonic())
        # functools.partial keeps the @callback marker visible to Home
        # Assistant, so expiry runs on the event loop. A bare lambda is treated
        # as a plain function and dispatched to an executor thread, where
        # touching hass.bus or entity state is unsafe.
        pending.cancel = async_call_later(
            self.hass, timeout, partial(self._on_pending_expired, name)
        )
        self._pending[name] = pending

    def _record_write(self, guard: WriteGuard, now: float, value: str) -> bool:
        """Record a write; return True if this trips the oscillation detector."""
        guard.writes.append((now, value))
        cutoff = now - OSCILLATION_WINDOW
        while guard.writes and guard.writes[0][0] < cutoff:
            guard.writes.popleft()
        if len(guard.writes) < OSCILLATION_THRESHOLD:
            return False
        # Genuine bulk activity uses many values; a loop flips between two.
        return len({value for _, value in guard.writes}) <= 2

    def _supersede_pending(self, name: str) -> None:
        """Remember the value of a write we are about to replace."""
        pending = self._pending.pop(name, None)
        if pending is None:
            return
        if pending.cancel:
            pending.cancel()
        entries = self._superseded.setdefault(name, [])
        entries.append((pending.value, time.monotonic() + STALE_ECHO_GRACE))
        # Keep only the most recent few. Each entry can swallow one matching
        # inbound event, so an unbounded history of rapid writes would start
        # eating genuine state changes from openHAB.
        del entries[:-MAX_SUPERSEDED]

    def _is_stale_echo(self, name: str, value: str) -> bool:
        """True if this is the echo of a command we have already replaced."""
        entries = self._superseded.get(name)
        if not entries:
            return False
        now = time.monotonic()
        entries[:] = [(v, exp) for v, exp in entries if exp > now]
        for index, (superseded_value, _) in enumerate(entries):
            if superseded_value == value:
                entries.pop(index)
                return True
        return False

    def _clear_pending(self, name: str, confirmed: bool) -> None:
        pending = self._pending.pop(name, None)
        if pending and pending.cancel:
            pending.cancel()
        if not confirmed:
            return
        guard = self._guards.setdefault(name, WriteGuard())
        if guard.unconfirmed_streak:
            guard.unconfirmed_streak = 0
            repairs.async_clear_unconfirmed(self.hass, self.entry, name)

    @callback
    def _on_pending_expired(self, name: str, _now: Any = None) -> None:
        """openHAB never echoed our command."""
        pending = self._pending.pop(name, None)
        if pending is None:
            return

        self.unconfirmed_commands += 1
        self.last_unconfirmed = {"item": name, "command": pending.value}
        guard = self._guards.setdefault(name, WriteGuard())
        guard.unconfirmed_streak += 1

        _LOGGER.info(
            "openHAB item '%s' did not report a state change after command '%s'. "
            "The bound thing may be offline or may have rejected the command.",
            name,
            pending.value,
        )
        self.hass.bus.async_fire(
            EVENT_COMMAND_UNCONFIRMED,
            {
                "item": name,
                "command": pending.value,
                "config_entry_id": self.entry.entry_id,
            },
        )
        if guard.unconfirmed_streak >= UNCONFIRMED_REPAIR_THRESHOLD:
            repairs.async_raise_unconfirmed(self.hass, self.entry, name)

        self._notify_connection()
        self._create_background_task(self._resync_item(name), f"openhab_resync_{name}")

    async def _resync_item(self, name: str) -> None:
        """openHAB still wins, even when a command went nowhere."""
        try:
            state = await self.client.async_get_state(name)
        except OpenHabNotFoundError:
            self._mark_missing(name)
            return
        except OpenHabError as err:
            _LOGGER.debug("Could not resync %s: %s", name, err)
            return
        self._apply_state(name, state)

    # -- entity feedback --------------------------------------------------

    @callback
    def async_report_parse_failure(self, name: str, value: str) -> None:
        """An entity could not make sense of a state openHAB sent."""
        guard = self._guards.setdefault(name, WriteGuard())
        guard.parse_failures += 1
        if guard.parse_failures == PARSE_FAILURE_REPAIR_THRESHOLD:
            repairs.async_raise_parse_failures(
                self.hass, self.entry, name, value, self.platform_for(name) or ""
            )

    @callback
    def async_report_parse_ok(self, name: str) -> None:
        """Clear the parse-failure streak once values make sense again."""
        guard = self._guards.get(name)
        if guard and guard.parse_failures:
            guard.parse_failures = 0
            repairs.async_clear_parse_failures(self.hass, self.entry, name)

    @callback
    def async_pending_command(self, name: str) -> str | None:
        """The in-flight command for an item, for the entity attribute."""
        pending = self._pending.get(name)
        return pending.value if pending else None

    # -- item presence / type ---------------------------------------------

    @callback
    def _mark_missing(self, name: str) -> None:
        if name in self.missing_items:
            return
        self.missing_items.add(name)
        _LOGGER.warning("openHAB item '%s' no longer exists", name)
        repairs.async_raise_item_missing(self.hass, self.entry, name)
        async_dispatcher_send(
            self.hass,
            SIGNAL_STATE_UPDATED.format(self.entry.entry_id, name),
            self.states.get(name),
        )

    @callback
    def _mark_present(self, name: str) -> None:
        if name not in self.missing_items:
            return
        self.missing_items.discard(name)
        repairs.async_clear_item_missing(self.hass, self.entry, name)

    @callback
    def _check_type_compatibility(self, name: str, item: OpenHabItem) -> None:
        """Raise a repair only when the type change actually breaks things."""
        platform = self.platform_for(name)
        if not platform:
            return
        if platform_is_compatible(item.type, platform, item.group_type):
            repairs.async_clear_type_mismatch(self.hass, self.entry, name)
            return
        repairs.async_raise_type_mismatch(
            self.hass, self.entry, name, item.type, platform
        )
