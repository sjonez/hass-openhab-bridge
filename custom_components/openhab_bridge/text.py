"""Text entities backed by openHAB String items."""

from __future__ import annotations

from homeassistant.components.text import ENTITY_ID_FORMAT, TextEntity
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
    """Set up openHAB text entities."""
    async_add_entities(build(hass, entry, Platform.TEXT, OpenHabText))


class OpenHabText(OpenHabEntity, TextEntity):
    """A free-text openHAB item."""

    _attr_native_max = 255

    def __init__(self, coordinator: OpenHabCoordinator, item_name: str) -> None:
        """Initialise with the text entity ID format."""
        super().__init__(coordinator, item_name, ENTITY_ID_FORMAT)

    @property
    def native_value(self) -> str | None:
        """The item state as text."""
        return self.raw_state

    async def async_set_value(self, value: str) -> None:
        """Send the new text as a command."""
        await self.coordinator.async_send_command(self.item_name, value)
