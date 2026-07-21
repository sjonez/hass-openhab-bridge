"""Diagnostics for the openHAB Bridge config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN, DOMAIN
from .coordinator import OpenHabCoordinator

REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted config plus live connection state."""
    coordinator: OpenHabCoordinator = hass.data[DOMAIN][entry.entry_id]
    stats = coordinator.websocket.stats
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        "options": dict(entry.options),
        "connection": {
            "connected": stats.connected,
            "last_connected": stats.last_connected,
            "last_event": stats.last_event,
            "reconnect_attempts": stats.reconnect_attempts,
            "last_error": stats.last_error,
            "seconds_at_backoff_ceiling": round(stats.seconds_at_ceiling, 1),
        },
        "items": {
            name: {
                "type": item.type,
                "group_type": item.group_type,
                "autoupdate": item.autoupdate,
                "state": coordinator.states.get(name),
                "platform": coordinator.platform_for(name),
                "missing": name in coordinator.missing_items,
            }
            for name, item in coordinator.items.items()
        },
        "safety": {
            "unconfirmed_commands": coordinator.unconfirmed_commands,
            "last_unconfirmed": coordinator.last_unconfirmed,
            "looping_items": sorted(coordinator.looping_items),
        },
    }
