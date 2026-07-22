"""Sensors: openHAB items exposed read-only, plus connection diagnostics."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    ENTITY_ID_FORMAT,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    OH_DATETIME,
    OH_NUMBER,
    SIGNAL_LAST_EVENT,
    base_item_type,
    item_dimension,
)
from .coordinator import OpenHabCoordinator
from .diagnostic_entity import OpenHabDiagnosticEntity
from .entity import OpenHabEntity, unit_from_state
from .platform_helper import build

# Only the diagnostic entities poll; item entities are pushed and set
# should_poll = False, so this interval applies to the diagnostics that don't
# override should_poll themselves. "Last event" pushes on every openHAB event
# instead (see OpenHabLastEventSensor) -- it's meant to be excluded from the
# recorder (see the README), so updating it on every event costs no history.
SCAN_INTERVAL = timedelta(minutes=5)

# openHAB dimensions that map cleanly onto an HA device class.
DIMENSION_DEVICE_CLASS = {
    "Temperature": SensorDeviceClass.TEMPERATURE,
    "Humidity": SensorDeviceClass.HUMIDITY,
    "Pressure": SensorDeviceClass.PRESSURE,
    "Power": SensorDeviceClass.POWER,
    "Energy": SensorDeviceClass.ENERGY,
    "ElectricPotential": SensorDeviceClass.VOLTAGE,
    "ElectricCurrent": SensorDeviceClass.CURRENT,
    "Illuminance": SensorDeviceClass.ILLUMINANCE,
    "Speed": SensorDeviceClass.SPEED,
    "Length": SensorDeviceClass.DISTANCE,
    "Mass": SensorDeviceClass.WEIGHT,
    "Frequency": SensorDeviceClass.FREQUENCY,
    "DataAmount": SensorDeviceClass.DATA_SIZE,
    "Time": SensorDeviceClass.DURATION,
    "Volume": SensorDeviceClass.VOLUME,
    "VolumetricFlowRate": SensorDeviceClass.VOLUME_FLOW_RATE,
}

# Energy is a running total; the rest are instantaneous readings.
TOTAL_DIMENSIONS = {"Energy"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up openHAB sensors and the connection diagnostics."""
    coordinator: OpenHabCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = list(
        build(hass, entry, Platform.SENSOR, OpenHabSensor)
    )
    entities += [
        OpenHabLastConnectedSensor(coordinator),
        OpenHabLastEventSensor(coordinator),
        OpenHabReconnectSensor(coordinator),
        OpenHabUnconfirmedSensor(coordinator),
    ]
    async_add_entities(entities)


class OpenHabSensor(OpenHabEntity, SensorEntity):
    """Any openHAB item, read-only."""

    def __init__(self, coordinator: OpenHabCoordinator, item_name: str) -> None:
        """Derive device class, state class and unit from the item type."""
        super().__init__(coordinator, item_name, ENTITY_ID_FORMAT)
        item = coordinator.items.get(item_name)
        self._base_type = base_item_type(item.type if item else None)
        dimension = item_dimension(item.type if item else None)

        if self._base_type == OH_DATETIME:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif self._base_type == OH_NUMBER:
            if dimension and (device_class := DIMENSION_DEVICE_CLASS.get(dimension)):
                self._attr_device_class = device_class
                self._attr_state_class = (
                    SensorStateClass.TOTAL_INCREASING
                    if dimension in TOTAL_DIMENSIONS
                    else SensorStateClass.MEASUREMENT
                )
            else:
                self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_unit_of_measurement(self) -> str | None:
        """openHAB carries the unit in the state, e.g. ``21.5 °C``."""
        if self._base_type != OH_NUMBER:
            return None
        return unit_from_state(self.coordinator.states.get(self.item_name))

    @property
    def native_value(self) -> Any:
        """Numeric, timestamp or plain string depending on the item type."""
        if self._base_type == OH_NUMBER:
            return self._parsed_float()
        if self._base_type == OH_DATETIME:
            state = self.raw_state
            if state is None:
                return None
            parsed = dt_util.parse_datetime(state)
            if parsed is None:
                self._report_parse_failure(state)
                return None
            self.coordinator.async_report_parse_ok(self.item_name)
            return parsed
        return self.raw_state


class OpenHabLastConnectedSensor(OpenHabDiagnosticEntity, SensorEntity):
    """When the event stream last connected successfully."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OpenHabCoordinator) -> None:
        """Initialise the last-connected diagnostic."""
        super().__init__(
            coordinator, "last_connected", "Last connected", ENTITY_ID_FORMAT
        )

    @property
    def native_value(self) -> datetime | None:
        """Timestamp of the last successful connection."""
        return self.coordinator.websocket.stats.last_connected


class OpenHabLastEventSensor(OpenHabDiagnosticEntity, SensorEntity):
    """When an openHAB event last arrived: catches a silently dead socket."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Pushed on every event via SIGNAL_LAST_EVENT instead of polled -- unlike
    # the other diagnostics, it changes far too often for a 5-minute poll to
    # be anything but misleading, and there's no recorder cost to updating it
    # live since users are expected to exclude it (see the README).
    _attr_should_poll = False

    def __init__(self, coordinator: OpenHabCoordinator) -> None:
        """Initialise the last-event diagnostic."""
        super().__init__(coordinator, "last_event", "Last event", ENTITY_ID_FORMAT)

    async def async_added_to_hass(self) -> None:
        """Refresh on connection changes and on every openHAB event."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LAST_EVENT.format(self.coordinator.entry.entry_id),
                self._handle_update,
            )
        )

    @property
    def native_value(self) -> datetime | None:
        """Timestamp of the most recent item event."""
        return self.coordinator.websocket.stats.last_event


class OpenHabReconnectSensor(OpenHabDiagnosticEntity, SensorEntity):
    """Reconnect attempts since the last successful connection."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    # No device class fits a retry counter, so pick the icon explicitly
    # rather than accept the generic default.
    _attr_icon = "mdi:lan-disconnect"

    def __init__(self, coordinator: OpenHabCoordinator) -> None:
        """Initialise the reconnect-attempts diagnostic."""
        super().__init__(
            coordinator, "reconnect_attempts", "Reconnect attempts", ENTITY_ID_FORMAT
        )

    @property
    def native_value(self) -> int:
        """Attempts since the last successful connect."""
        return self.coordinator.websocket.stats.reconnect_attempts


class OpenHabUnconfirmedSensor(OpenHabDiagnosticEntity, SensorEntity):
    """Commands that openHAB never echoed back as a state change."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:comment-question-outline"

    def __init__(self, coordinator: OpenHabCoordinator) -> None:
        """Initialise the unconfirmed-commands diagnostic."""
        super().__init__(
            coordinator,
            "unconfirmed_commands",
            "Unconfirmed commands",
            ENTITY_ID_FORMAT,
        )

    @property
    def native_value(self) -> int:
        """Total unconfirmed commands since startup."""
        return self.coordinator.unconfirmed_commands

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The most recent unconfirmed item and command."""
        return dict(self.coordinator.last_unconfirmed or {})
