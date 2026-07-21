"""Decoding of openHAB WebSocket frames."""

from __future__ import annotations

import json

from custom_components.openhab_bridge.api import _clean_label, _parse_autoupdate
from custom_components.openhab_bridge.websocket import (
    ITEM_TOPIC_WILDCARD,
    MAX_TOPIC_FILTERS,
    OpenHabEvent,
    _decode_payload,
    item_topic_filters,
)


def test_topic_filter_is_per_item():
    """Only exposed items are subscribed to, not the whole item registry."""
    patterns = item_topic_filters(["Kitchen_Light", "Garage_Gate"])
    assert patterns == [
        "openhab/items/Garage_Gate/*",
        "openhab/items/Kitchen_Light/*",
    ]
    assert ITEM_TOPIC_WILDCARD not in patterns


def test_topic_filter_covers_registry_events():
    """Removal and update events share the item's topic prefix."""
    (pattern,) = item_topic_filters(["Kitchen_Light"])
    prefix = pattern.removesuffix("*")
    for topic in (
        "openhab/items/Kitchen_Light/state",
        "openhab/items/Kitchen_Light/statechanged",
        "openhab/items/Kitchen_Light/removed",
        "openhab/items/Kitchen_Light/updated",
    ):
        assert topic.startswith(prefix)


def test_topic_filter_falls_back_when_huge():
    """An absurd number of items must not build an unusable payload."""
    names = [f"Item_{index}" for index in range(MAX_TOPIC_FILTERS + 1)]
    assert item_topic_filters(names) == [ITEM_TOPIC_WILDCARD]


def test_topic_filter_with_no_items():
    """No exposed items yet: nothing sensible to subscribe to."""
    assert item_topic_filters([]) == [ITEM_TOPIC_WILDCARD]


def test_payload_is_double_encoded():
    """openHAB nests a JSON string inside the JSON envelope."""
    payload = _decode_payload(json.dumps({"type": "OnOff", "value": "ON"}))
    assert payload == {"type": "OnOff", "value": "ON"}


def test_payload_survives_non_json():
    """Heartbeat replies are bare strings, not JSON."""
    assert _decode_payload("PONG") == "PONG"


def test_item_name_from_topic():
    """Item name comes from the topic, not the payload."""
    event = OpenHabEvent(
        type="ItemStateChangedEvent",
        topic="openhab/items/Kitchen_Light/statechanged",
        payload={"type": "OnOff", "value": "ON"},
    )
    assert event.item_name == "Kitchen_Light"
    assert event.state_value == "ON"


def test_non_item_topic_ignored():
    """Thing and rule events carry no item name."""
    event = OpenHabEvent(
        type="ThingStatusInfoEvent",
        topic="openhab/things/mqtt:topic:gate/status",
        payload={},
    )
    assert event.item_name is None


def test_quantity_state_keeps_unit():
    """Number:Dimension states arrive with their unit attached."""
    event = OpenHabEvent(
        type="ItemStateChangedEvent",
        topic="openhab/items/Outdoor_Temp/statechanged",
        payload={"type": "Quantity", "value": "21.5 °C"},
    )
    assert event.state_value == "21.5 °C"


def test_unusable_states_pass_through():
    """NULL and UNDEF are real openHAB states; entities decide what to do."""
    event = OpenHabEvent(
        type="ItemStateEvent",
        topic="openhab/items/Kitchen_Light/state",
        payload={"type": "UnDef", "value": "NULL"},
    )
    assert event.state_value == "NULL"


def test_label_strips_state_presentation_pattern():
    """openHAB labels carry a formatting pattern that is not part of the name.

    Real example: "Outdoor Temperature [%.1f °C]" would otherwise become
    the Home Assistant friendly name verbatim.
    """
    assert _clean_label("Boiler Pressure [%.1f bar]") == "Boiler Pressure"
    assert _clean_label("Outdoor Temperature [%.1f °C]") == "Outdoor Temperature"
    assert _clean_label("Kitchen Light") == "Kitchen Light"
    assert _clean_label(None) is None
    # Brackets that are not a trailing pattern belong to the name.
    assert _clean_label("Zone [1] Lamp") == "Zone [1] Lamp"
    # Stripping everything would leave nothing useful, so keep the original.
    assert _clean_label("[%d]") == "[%d]"


def test_autoupdate_metadata_parsing():
    """Only an explicit "false" disables autoupdate."""
    assert _parse_autoupdate(None) is True
    assert _parse_autoupdate({}) is True
    assert _parse_autoupdate({"autoupdate": {"value": "true"}}) is True
    assert _parse_autoupdate({"autoupdate": {"value": "false"}}) is False
    assert _parse_autoupdate({"autoupdate": {"value": "FALSE"}}) is False
