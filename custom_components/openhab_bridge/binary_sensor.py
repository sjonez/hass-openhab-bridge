"""Binary sensors: openHAB Contact/Switch items, plus connection diagnostics."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    ENTITY_ID_FORMAT,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_CLASS, DOMAIN
from .coordinator import OpenHabCoordinator
from .diagnostic_entity import OpenHabDiagnosticEntity
from .entity import OpenHabEntity
from .platform_helper import build

# See the note in sensor.py: this applies only to the polled diagnostics.
SCAN_INTERVAL = timedelta(minutes=5)

ON_STATES = {"ON", "OPEN", "PLAY", "UP"}
OFF_STATES = {"OFF", "CLOSED", "PAUSE", "DOWN"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up openHAB binary sensors and the connection diagnostics."""
    coordinator: OpenHabCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = list(
        build(hass, entry, Platform.BINARY_SENSOR, OpenHabBinarySensor)
    )
    entities.append(OpenHabConnectedSensor(coordinator))
    entities.append(OpenHabLoopSensor(coordinator))
    async_add_entities(entities)


class OpenHabBinarySensor(OpenHabEntity, BinarySensorEntity):
    """A Contact or Switch item exposed as a binary sensor."""

    def __init__(self, coordinator: OpenHabCoordinator, item_name: str) -> None:
        """Initialise with the binary_sensor entity ID format."""
        super().__init__(coordinator, item_name, ENTITY_ID_FORMAT)
        item = coordinator.items.get(item_name)
        if item and item.type == "Contact":
            self._attr_device_class = BinarySensorDeviceClass.OPENING
        if device_class_override := self.config.get(CONF_DEVICE_CLASS):
            self._attr_device_class = device_class_override

    @property
    def is_on(self) -> bool | None:
        """Interpret the openHAB state as on/off."""
        state = self.raw_state
        if state is None:
            return None
        upper = state.upper()
        if upper in ON_STATES:
            return True
        if upper in OFF_STATES:
            return False
        # Numeric items: anything other than zero counts as on.
        try:
            value = float(upper.split(" ", 1)[0])
        except ValueError:
            self._report_parse_failure(state)
            return None
        self.coordinator.async_report_parse_ok(self.item_name)
        return value != 0


class OpenHabConnectedSensor(OpenHabDiagnosticEntity, BinarySensorEntity):
    """Whether the openHAB event stream is currently connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OpenHabCoordinator) -> None:
        """Initialise the connectivity diagnostic."""
        super().__init__(coordinator, "connected", "Connected", ENTITY_ID_FORMAT)

    @property
    def available(self) -> bool:
        """Always available: reporting "not connected" is the whole point."""
        return True

    @property
    def is_on(self) -> bool:
        """True while the WebSocket is up."""
        return self.coordinator.websocket.stats.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface the last connection error, when there is one."""
        stats = self.coordinator.websocket.stats
        return {"last_error": stats.last_error}


class OpenHabLoopSensor(OpenHabDiagnosticEntity, BinarySensorEntity):
    """On when a feedback loop has been detected on any item."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OpenHabCoordinator) -> None:
        """Initialise the feedback-loop diagnostic."""
        super().__init__(
            coordinator, "feedback_loop", "Feedback loop detected", ENTITY_ID_FORMAT
        )

    @property
    def available(self) -> bool:
        """Always available."""
        return True

    @property
    def is_on(self) -> bool:
        """True while any item is being suppressed."""
        return bool(self.coordinator.looping_items)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the affected items."""
        return {"items": sorted(self.coordinator.looping_items)}
