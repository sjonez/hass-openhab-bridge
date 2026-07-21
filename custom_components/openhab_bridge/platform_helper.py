"""Shared helper for building a platform's entity list from the options."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_PLATFORM, DOMAIN
from .coordinator import OpenHabCoordinator


def items_for_platform(
    hass: HomeAssistant, entry: ConfigEntry, platform: Platform
) -> tuple[OpenHabCoordinator, list[str]]:
    """Coordinator plus the item names configured for ``platform``."""
    coordinator: OpenHabCoordinator = hass.data[DOMAIN][entry.entry_id]
    names = [
        name
        for name, config in coordinator.configured_items.items()
        if config.get(CONF_PLATFORM) == platform.value
    ]
    return coordinator, names


def build(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: Platform,
    factory: Callable[[OpenHabCoordinator, str], Any],
) -> Iterable[Any]:
    """Instantiate one entity per configured item on this platform."""
    coordinator, names = items_for_platform(hass, entry, platform)
    return [factory(coordinator, name) for name in names]
