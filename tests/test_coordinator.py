"""Write safety: loop prevention, echo handling and autoupdate=false items."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.openhab_bridge.const import (
    DOMAIN,
    EVENT_COMMAND_UNCONFIRMED,
    EVENT_ITEM_EVENT,
    OSCILLATION_THRESHOLD,
)
from custom_components.openhab_bridge.coordinator import OpenHabCoordinator
from custom_components.openhab_bridge.websocket import OpenHabEvent


async def _coordinator(hass, config_entry, mock_client) -> OpenHabCoordinator:
    coordinator = OpenHabCoordinator(hass, config_entry)
    coordinator.client = mock_client
    await coordinator.async_resync()
    return coordinator


def _state_event(item: str, value: str) -> OpenHabEvent:
    """A plain state post. openHAB sends this for every update, changed or not."""
    return OpenHabEvent(
        type="ItemStateEvent",
        topic=f"openhab/items/{item}/state",
        payload={"type": "OnOff", "value": value},
    )


def _changed_event(item: str, value: str, old: str) -> OpenHabEvent:
    """Sent only when the value actually changed, and it carries the old one."""
    return OpenHabEvent(
        type="ItemStateChangedEvent",
        topic=f"openhab/items/{item}/statechanged",
        payload={"type": "OnOff", "value": value, "oldValue": old},
    )


def _command_event(item: str, value: str) -> OpenHabEvent:
    """A command, which never moves state by itself."""
    return OpenHabEvent(
        type="ItemCommandEvent",
        topic=f"openhab/items/{item}/command",
        payload={"type": "OnOff", "value": value},
    )


async def test_initial_state_comes_from_openhab(hass, config_entry, mock_client):
    """openHAB always wins on startup."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    assert coordinator.states["Kitchen_Light"] == "OFF"
    assert coordinator.name_for("Kitchen_Light") == "Kitchen Light (openHAB)"


async def test_label_falls_back_to_item_name(hass, config_entry, mock_client):
    """An item without a label still gets a sensible friendly name."""
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, "items": {"No_Label": {"platform": "sensor"}}},
    )
    coordinator = await _coordinator(hass, config_entry, mock_client)
    assert coordinator.name_for("No_Label") == "No_Label (openHAB)"


async def test_inbound_event_never_writes_back(hass, config_entry, mock_client):
    """The invariant that stops HA -> openHAB -> HA from looping."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    mock_client.reset_mock()

    coordinator._handle_event(_state_event("Kitchen_Light", "ON"))
    await hass.async_block_till_done()

    assert coordinator.states["Kitchen_Light"] == "ON"
    assert mock_client.async_send_command.call_count == 0
    assert mock_client.async_post_update.call_count == 0


async def test_no_op_command_is_skipped(hass, config_entry, mock_client):
    """Commanding an autoupdate item to the value it already has is redundant."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    assert coordinator.states["Kitchen_Light"] == "OFF"

    await coordinator.async_send_command("Kitchen_Light", "OFF")
    assert mock_client.commands == []

    await coordinator.async_send_command("Kitchen_Light", "ON")
    assert mock_client.commands == [("Kitchen_Light", "ON")]


async def test_rapid_toggle_is_not_treated_as_a_no_op(hass, config_entry, mock_client):
    """Regression: state is never updated optimistically.

    After sending ON the cache still reads OFF, because we wait for openHAB to
    confirm. Comparing a new command against the cache rather than the
    in-flight one made a quick ON-then-OFF look redundant, and the OFF was
    silently dropped -- leaving the device on.
    """
    coordinator = await _coordinator(hass, config_entry, mock_client)
    assert coordinator.states["Kitchen_Light"] == "OFF"

    await coordinator.async_send_command("Kitchen_Light", "ON")
    await coordinator.async_send_command("Kitchen_Light", "OFF")

    assert mock_client.commands == [
        ("Kitchen_Light", "ON"),
        ("Kitchen_Light", "OFF"),
    ]


async def test_duplicate_of_inflight_command_is_skipped(
    hass, config_entry, mock_client
):
    """Re-sending the command already in flight is still redundant."""
    coordinator = await _coordinator(hass, config_entry, mock_client)

    await coordinator.async_send_command("Kitchen_Light", "ON")
    await coordinator.async_send_command("Kitchen_Light", "ON")

    assert mock_client.commands == [("Kitchen_Light", "ON")]


async def test_no_op_not_skipped_when_autoupdate_disabled(
    hass, config_entry, mock_client
):
    """For autoupdate=false items the state reflects the device, not the command.

    Re-sending a value the item already reads is legitimate, so suppression
    must not swallow it.
    """
    coordinator = await _coordinator(hass, config_entry, mock_client)
    assert coordinator.states["Garage_Gate"] == "OFF"
    assert coordinator.autoupdate("Garage_Gate") is False

    await coordinator.async_send_command("Garage_Gate", "OFF")
    assert mock_client.commands == [("Garage_Gate", "OFF")]


async def test_stale_echo_is_dropped(hass, config_entry, mock_client):
    """A superseded command's echo must not flip the UI backwards."""
    coordinator = await _coordinator(hass, config_entry, mock_client)

    await coordinator.async_send_command("Kitchen_Light", "ON")
    await coordinator.async_send_command("Kitchen_Light", "OFF")

    # The echo of the first command arrives after the second was sent.
    coordinator._handle_event(_state_event("Kitchen_Light", "ON"))
    assert coordinator.states["Kitchen_Light"] == "OFF"

    # The echo of the second command is applied normally.
    coordinator._handle_event(_state_event("Kitchen_Light", "OFF"))
    assert coordinator.states["Kitchen_Light"] == "OFF"


async def test_oscillation_detector_trips_and_releases(
    hass, config_entry, mock_client, freezer
):
    """A loop outside the integration must not be amplified by it."""
    coordinator = await _coordinator(hass, config_entry, mock_client)

    for index in range(OSCILLATION_THRESHOLD):
        await coordinator.async_send_command(
            "Garage_Gate", "ON" if index % 2 else "OFF"
        )

    assert "Garage_Gate" in coordinator.looping_items
    with pytest.raises(HomeAssistantError, match="feedback loop"):
        await coordinator.async_send_command("Garage_Gate", "ON")

    # Inbound state keeps working while outbound is paused.
    coordinator._handle_event(_state_event("Garage_Gate", "ON"))
    assert coordinator.states["Garage_Gate"] == "ON"

    freezer.tick(timedelta(seconds=61))
    await coordinator.async_send_command("Garage_Gate", "OFF")
    assert mock_client.commands[-1] == ("Garage_Gate", "OFF")


async def test_unconfirmed_command_fires_event(hass, config_entry, mock_client):
    """An autoupdate=false command that goes nowhere must be visible."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_COMMAND_UNCONFIRMED)

    await coordinator.async_send_command("Garage_Gate", "ON")
    assert coordinator.async_pending_command("Garage_Gate") == "ON"

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["item"] == "Garage_Gate"
    assert events[0].data["command"] == "ON"
    assert coordinator.unconfirmed_commands == 1
    # openHAB still wins: we re-read rather than assume.
    assert mock_client.async_get_state.await_count >= 1


async def test_confirmed_command_clears_pending(hass, config_entry, mock_client):
    """A matching echo confirms the command and cancels the timeout."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_COMMAND_UNCONFIRMED)

    await coordinator.async_send_command("Garage_Gate", "ON")
    coordinator._handle_event(_state_event("Garage_Gate", "ON"))
    assert coordinator.async_pending_command("Garage_Gate") is None

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert events == []


async def test_missing_item_raises_one_issue_and_clears(
    hass, config_entry, mock_client, items
):
    """Repairs are keyed per item, raised once, and cleared automatically."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    registry = ir.async_get(hass)

    mock_client.async_get_items.return_value = [
        item for item in items if item.name != "Kitchen_Light"
    ]
    await coordinator.async_resync()
    await coordinator.async_resync()

    issues = [
        issue
        for issue in registry.issues.values()
        if issue.domain == DOMAIN and "item_missing" in issue.issue_id
    ]
    assert len(issues) == 1
    assert "Kitchen_Light" in issues[0].issue_id

    mock_client.async_get_items.return_value = items
    await coordinator.async_resync()
    assert not [
        issue
        for issue in registry.issues.values()
        if issue.domain == DOMAIN and "item_missing" in issue.issue_id
    ]


async def test_repeat_update_is_not_an_event(hass, config_entry, mock_client):
    """Re-sending the value an item already holds means nothing changed.

    openHAB emits an ItemStateEvent for it, but there is no news in it, so it
    must not reach the bus. Only a real change or a command is reportable.
    """
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_ITEM_EVENT)
    assert coordinator.states["Kitchen_Light"] == "OFF"

    coordinator._handle_event(_state_event("Kitchen_Light", "OFF"))
    await hass.async_block_till_done()

    assert events == []
    assert coordinator.states["Kitchen_Light"] == "OFF"


async def test_change_is_reported_once_with_the_old_value(
    hass, config_entry, mock_client
):
    """openHAB sends two events per change; we must report exactly one.

    A real change produces ItemStateEvent *and* ItemStateChangedEvent. Firing
    on both would double-trigger every automation listening for changes.
    """
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_ITEM_EVENT)

    coordinator._handle_event(_state_event("Kitchen_Light", "ON"))
    coordinator._handle_event(_changed_event("Kitchen_Light", "ON", "OFF"))
    await hass.async_block_till_done()

    assert coordinator.states["Kitchen_Light"] == "ON"
    assert len(events) == 1
    assert events[0].data["type"] == "changed"
    assert events[0].data["value"] == "ON"
    assert events[0].data["old_value"] == "OFF"


async def test_command_and_change_are_distinguishable(hass, config_entry, mock_client):
    """The whole point: an automation can tell the two apart."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_ITEM_EVENT)

    coordinator._handle_event(_command_event("Kitchen_Light", "ON"))
    coordinator._handle_event(_changed_event("Kitchen_Light", "ON", "OFF"))
    await hass.async_block_till_done()

    assert [e.data["type"] for e in events] == ["command", "changed"]
    # Only a change has a "before"; a command does not.
    assert "old_value" not in events[0].data
    assert events[1].data["old_value"] == "OFF"


async def test_command_event_is_surfaced(hass, config_entry, mock_client):
    """A command is reportable whatever value it carries.

    Commanding an item to the value it already holds emits only a command
    event -- for an autoupdate=false item openHAB sends no state event at
    all -- so without this the press of a scene switch is invisible.
    """
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_ITEM_EVENT)

    coordinator._handle_event(_command_event("Garage_Gate", "OFF"))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["value"] == "OFF"
    # State is untouched: commands do not move it.
    assert coordinator.states["Garage_Gate"] == "OFF"


async def test_own_command_echo_is_labelled(hass, config_entry, mock_client):
    """Our own command must be distinguishable, or automations can loop."""
    coordinator = await _coordinator(hass, config_entry, mock_client)
    events = async_capture_events(hass, EVENT_ITEM_EVENT)

    await coordinator.async_send_command("Garage_Gate", "ON")
    coordinator._handle_event(_command_event("Garage_Gate", "ON"))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["origin"] == "home_assistant"


async def test_last_command_attribute_tracks_commands(hass, config_entry, mock_client):
    """A command must leave a mark, since it changes no state.

    Home Assistant's last_changed only moves on a real state change, so
    without this an item commanded to the value it already holds shows no
    evidence of the command anywhere.
    """
    coordinator = await _coordinator(hass, config_entry, mock_client)
    assert coordinator.last_command("Garage_Gate") is None

    coordinator._handle_event(_command_event("Garage_Gate", "ON"))
    await hass.async_block_till_done()
    first = coordinator.last_command("Garage_Gate")
    assert first is not None

    coordinator._handle_event(_command_event("Garage_Gate", "ON"))
    await hass.async_block_till_done()
    assert coordinator.last_command("Garage_Gate") >= first
    # A command to one item must not touch another.
    assert coordinator.last_command("Kitchen_Light") is None


async def test_state_change_does_not_set_last_command(hass, config_entry, mock_client):
    """last_command means commands only; changes are covered by last_changed."""
    coordinator = await _coordinator(hass, config_entry, mock_client)

    coordinator._handle_event(_changed_event("Kitchen_Light", "ON", "OFF"))
    await hass.async_block_till_done()

    assert coordinator.states["Kitchen_Light"] == "ON"
    assert coordinator.last_command("Kitchen_Light") is None
