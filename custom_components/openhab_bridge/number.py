"""Number entities backed by openHAB Number, Dimmer or Rollershutter items."""

from __future__ import annotations

from homeassistant.components.number import ENTITY_ID_FORMAT, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import OH_DIMMER, OH_ROLLERSHUTTER, base_item_type
from .coordinator import OpenHabCoordinator
from .entity import OpenHabEntity, unit_from_state
from .platform_helper import build

# Dimmer and Rollershutter are percentages in openHAB.
PERCENT_TYPES = {OH_DIMMER, OH_ROLLERSHUTTER}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up openHAB numbers."""
    async_add_entities(build(hass, entry, Platform.NUMBER, OpenHabNumber))


class OpenHabNumber(OpenHabEntity, NumberEntity):
    """A writable numeric openHAB item."""

    def __init__(self, coordinator: OpenHabCoordinator, item_name: str) -> None:
        """Bound percentage types to 0-100; leave plain numbers unbounded."""
        super().__init__(coordinator, item_name, ENTITY_ID_FORMAT)
        item = coordinator.items.get(item_name)
        self._base_type = base_item_type(item.type if item else None)
        if self._base_type in PERCENT_TYPES:
            self._attr_native_min_value = 0
            self._attr_native_max_value = 100
            self._attr_native_step = 1
            self._attr_native_unit_of_measurement = "%"

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Percentages are fixed; other items carry their unit in the state."""
        if self._base_type in PERCENT_TYPES:
            return "%"
        return unit_from_state(self.coordinator.states.get(self.item_name))

    @property
    def native_value(self) -> float | None:
        """The numeric part of the openHAB state."""
        return self._parsed_float()

    async def async_set_native_value(self, value: float) -> None:
        """Command the new value; state follows when openHAB reports it."""
        if self._base_type in PERCENT_TYPES or value == int(value):
            command = str(int(value))
        else:
            command = str(value)
        await self.coordinator.async_send_command(self.item_name, command)
