"""Base for the per-entry connection diagnostic entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, async_generate_entity_id

from .const import DOMAIN, ENTITY_ID_PREFIX, SIGNAL_CONNECTION
from .coordinator import OpenHabCoordinator


class OpenHabDiagnosticEntity(Entity):
    """A diagnostic entity describing the bridge itself, not an item."""

    _attr_has_entity_name = False
    # Polled, unlike the item entities. Connection stats such as "last event"
    # change without any dispatcher signal firing -- an arriving item event
    # notifies only that item's entity -- so without polling these would sit
    # at their startup values forever. "Last event" in particular exists to
    # reveal a silently dead socket, which it cannot do if it never updates.
    _attr_should_poll = True

    def __init__(
        self,
        coordinator: OpenHabCoordinator,
        key: str,
        label: str,
        entity_id_format: str,
    ) -> None:
        """Identify the diagnostic and suggest its entity ID."""
        self.coordinator = coordinator
        self._key = key
        # The "_diag_" marker keeps these out of the item-entity cleanup.
        self._attr_unique_id = f"{coordinator.entry.entry_id}_diag_{key}"
        # No "(openHAB)" suffix here. That exists to mark entities mirroring an
        # openHAB item among a user's own entities; these belong to the openHAB
        # device itself, which already says so.
        self._attr_name = label
        self.entity_id = async_generate_entity_id(
            entity_id_format,
            f"{ENTITY_ID_PREFIX}_{key}",
            hass=coordinator.hass,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=f"openHAB ({coordinator.client.base_url})",
            manufacturer="openHAB",
            configuration_url=coordinator.client.base_url,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Refresh whenever the connection state changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self.coordinator.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
