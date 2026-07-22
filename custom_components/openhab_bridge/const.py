"""Constants and openHAB -> Home Assistant type mapping."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "openhab_bridge"

# Config entry data
CONF_BASE_URL: Final = "base_url"
CONF_TOKEN: Final = "token"
CONF_VERIFY_SSL: Final = "verify_ssl"

# Config entry options
CONF_ITEMS: Final = "items"
CONF_PLATFORM: Final = "platform"
CONF_NAME_OVERRIDE: Final = "name"

# Options flow menu steps
STEP_ADD_ITEMS: Final = "add_items"
STEP_EDIT_ITEM: Final = "edit_item"
STEP_REMOVE_ITEMS: Final = "remove_items"
STEP_CONNECTION: Final = "connection"

# Dispatcher signals
SIGNAL_STATE_UPDATED: Final = f"{DOMAIN}_state_{{}}_{{}}"
SIGNAL_CONNECTION: Final = f"{DOMAIN}_connection_{{}}"
SIGNAL_LAST_EVENT: Final = f"{DOMAIN}_last_event_{{}}"

# Events fired on the HA bus
EVENT_COMMAND_UNCONFIRMED: Final = f"{DOMAIN}_command_unconfirmed"
# Fired for openHAB item activity, mirroring the distinction openHAB rules
# make between "received command" and "changed". Commands are reported
# whatever value they carry, since a command is an action and produces no
# Home Assistant state change. Updates that merely repeat the value an item
# already holds are deliberately NOT reported: nothing changed.
EVENT_ITEM_EVENT: Final = f"{DOMAIN}_item_event"
EVENT_TYPE_COMMAND: Final = "command"
EVENT_TYPE_CHANGED: Final = "changed"

# Services
SERVICE_GET_ITEM_STATE: Final = "get_item_state"
SERVICE_POST_UPDATE: Final = "post_update"
SERVICE_SEND_COMMAND: Final = "send_command"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_ITEM: Final = "item"
ATTR_STATE: Final = "state"
ATTR_COMMAND: Final = "command"

# openHAB sentinel states meaning "no usable value"
STATE_NULL: Final = "NULL"
STATE_UNDEF: Final = "UNDEF"
UNUSABLE_STATES: Final = frozenset({STATE_NULL, STATE_UNDEF})

# Naming
NAME_SUFFIX: Final = "(openHAB)"
ENTITY_ID_PREFIX: Final = "openhab"

# Connection tuning
HEARTBEAT_INTERVAL: Final = 5.0  # openHAB idle timeout is 10s
HEARTBEAT_TOPIC: Final = "openhab/websocket/heartbeat"
FILTER_TYPE_TOPIC: Final = "openhab/websocket/filter/type"
FILTER_TOPIC_TOPIC: Final = "openhab/websocket/filter/topic"
WS_EVENT_TYPE: Final = "WebSocketEvent"

BACKOFF_INITIAL: Final = 1.0
BACKOFF_MAX: Final = 60.0
# How long the backoff may sit at its ceiling before we raise a repair.
UNREACHABLE_REPAIR_AFTER: Final = 15 * 60

# Loop / echo safety
PENDING_TIMEOUT: Final = 5.0
PENDING_TIMEOUT_NO_AUTOUPDATE: Final = 60.0
OSCILLATION_WINDOW: Final = 30.0
OSCILLATION_THRESHOLD: Final = 10
OSCILLATION_COOLDOWN: Final = 60.0
UNCONFIRMED_REPAIR_THRESHOLD: Final = 3
PARSE_FAILURE_REPAIR_THRESHOLD: Final = 5

# Event types we ask openHAB to send us.
SUBSCRIBED_EVENT_TYPES: Final = [
    "ItemStateEvent",
    "ItemStateChangedEvent",
    # An item that is already ON and is commanded ON again emits ONLY this --
    # openHAB sends no state event at all. Scene switches and button items
    # behave this way, so without it those are invisible to Home Assistant.
    "ItemCommandEvent",
    "ItemAddedEvent",
    "ItemUpdatedEvent",
    "ItemRemovedEvent",
]

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

# --- openHAB item type -> HA platform -------------------------------------
#
# openHAB item types may carry a dimension suffix ("Number:Temperature"); use
# base_item_type() to strip it before looking anything up here.

OH_SWITCH: Final = "Switch"
OH_CONTACT: Final = "Contact"
OH_DIMMER: Final = "Dimmer"
OH_COLOR: Final = "Color"
OH_NUMBER: Final = "Number"
OH_ROLLERSHUTTER: Final = "Rollershutter"
OH_STRING: Final = "String"
OH_DATETIME: Final = "DateTime"
OH_LOCATION: Final = "Location"
OH_PLAYER: Final = "Player"
OH_IMAGE: Final = "Image"
OH_GROUP: Final = "Group"

DEFAULT_PLATFORM: dict[str, Platform] = {
    OH_SWITCH: Platform.SWITCH,
    OH_CONTACT: Platform.BINARY_SENSOR,
    OH_DIMMER: Platform.LIGHT,
    OH_COLOR: Platform.LIGHT,
    OH_NUMBER: Platform.SENSOR,
    OH_ROLLERSHUTTER: Platform.NUMBER,
    OH_STRING: Platform.SENSOR,
    OH_DATETIME: Platform.SENSOR,
    OH_LOCATION: Platform.SENSOR,
    OH_PLAYER: Platform.SENSOR,
    OH_IMAGE: Platform.SENSOR,
}

ALLOWED_PLATFORMS: dict[str, tuple[Platform, ...]] = {
    OH_SWITCH: (Platform.SWITCH, Platform.BINARY_SENSOR, Platform.LIGHT),
    OH_CONTACT: (Platform.BINARY_SENSOR, Platform.SENSOR),
    OH_DIMMER: (Platform.LIGHT, Platform.NUMBER, Platform.SENSOR),
    OH_COLOR: (Platform.LIGHT, Platform.SENSOR),
    OH_NUMBER: (Platform.SENSOR, Platform.NUMBER, Platform.BINARY_SENSOR),
    OH_ROLLERSHUTTER: (Platform.NUMBER, Platform.SENSOR),
    OH_STRING: (Platform.SENSOR, Platform.TEXT),
    OH_DATETIME: (Platform.SENSOR, Platform.TEXT),
    OH_LOCATION: (Platform.SENSOR, Platform.TEXT),
    OH_PLAYER: (Platform.SENSOR, Platform.TEXT),
    OH_IMAGE: (Platform.SENSOR,),
}

FALLBACK_PLATFORM: Final = Platform.SENSOR


def base_item_type(item_type: str | None) -> str:
    """Strip any dimension suffix, e.g. ``Number:Temperature`` -> ``Number``."""
    if not item_type:
        return OH_STRING
    return item_type.split(":", 1)[0]


def item_dimension(item_type: str | None) -> str | None:
    """Return the dimension of a ``Number:Dimension`` item, if any."""
    if not item_type or ":" not in item_type:
        return None
    return item_type.split(":", 1)[1]


def default_platform_for(
    item_type: str | None, group_type: str | None = None
) -> Platform:
    """Best-guess HA platform for an openHAB item type.

    Groups delegate to their ``groupType`` where one is defined, since that is
    the type the group's aggregated state actually has.
    """
    base = base_item_type(item_type)
    if base == OH_GROUP:
        if group_type:
            return default_platform_for(group_type)
        return FALLBACK_PLATFORM
    return DEFAULT_PLATFORM.get(base, FALLBACK_PLATFORM)


def allowed_platforms_for(
    item_type: str | None, group_type: str | None = None
) -> tuple[Platform, ...]:
    """Platforms a user may legitimately choose for an openHAB item type."""
    base = base_item_type(item_type)
    if base == OH_GROUP:
        if group_type:
            return allowed_platforms_for(group_type)
        return (FALLBACK_PLATFORM, Platform.TEXT)
    return ALLOWED_PLATFORMS.get(base, (FALLBACK_PLATFORM,))


def platform_is_compatible(
    item_type: str | None, platform: str, group_type: str | None = None
) -> bool:
    """Whether ``platform`` can still represent an item of ``item_type``."""
    return platform in allowed_platforms_for(item_type, group_type)
