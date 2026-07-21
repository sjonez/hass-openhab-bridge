"""Switch entities backed by openHAB Switch items."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import ENTITY_ID_FORMAT, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import OpenHabCoordinator
from .entity import OpenHabEntity
from .platform_helper import build


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up openHAB switches."""
    async_add_entities(build(hass, entry, Platform.SWITCH, OpenHabSwitch))


class OpenHabSwitch(OpenHabEntity, SwitchEntity):
    """An openHAB Switch item."""

    def __init__(self, coordinator: OpenHabCoordinator, item_name: str) -> None:
        """Initialise with the switch entity ID format."""
        super().__init__(coordinator, item_name, ENTITY_ID_FORMAT)

    @property
    def is_on(self) -> bool | None:
        """True when openHAB reports ON."""
        state = self.raw_state
        if state is None:
            return None
        return state.upper() == "ON"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send ON. State follows only once openHAB reports it."""
        await self.coordinator.async_send_command(self.item_name, "ON")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send OFF. State follows only once openHAB reports it."""
        await self.coordinator.async_send_command(self.item_name, "OFF")
