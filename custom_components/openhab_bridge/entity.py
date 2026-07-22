"""Shared base for entities backed by an openHAB item."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, async_generate_entity_id

from .const import (
    DOMAIN,
    ENTITY_ID_PREFIX,
    SIGNAL_CONNECTION,
    SIGNAL_STATE_UPDATED,
    UNUSABLE_STATES,
)
from .coordinator import OpenHabCoordinator

_LOGGER = logging.getLogger(__name__)


class OpenHabEntity(Entity):
    """An entity mirroring one openHAB item.

    Inbound state and outbound commands are deliberately kept in separate
    paths: everything here that reacts to openHAB only ever writes HA state,
    and never issues a request back to openHAB. That is what stops an echoed
    change from bouncing back as a new command.
    """

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: OpenHabCoordinator,
        item_name: str,
        entity_id_format: str,
    ) -> None:
        """Set the identity and suggested entity ID for this item."""
        self.coordinator = coordinator
        self.item_name = item_name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{item_name}"
        # A suggestion only: the entity registry keeps a user's own rename.
        self.entity_id = async_generate_entity_id(
            entity_id_format,
            f"{ENTITY_ID_PREFIX}_{item_name}",
            hass=coordinator.hass,
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=f"openHAB ({coordinator.client.base_url})",
            manufacturer="openHAB",
            configuration_url=coordinator.client.base_url,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def name(self) -> str:
        """The openHAB label (or override) with the openHAB suffix."""
        return self.coordinator.name_for(self.item_name)

    @property
    def config(self) -> dict[str, Any]:
        """This item's options entry: platform, name and advanced overrides."""
        return self.coordinator.configured_items.get(self.item_name, {})

    @property
    def available(self) -> bool:
        """Available while connected and holding a usable openHAB state."""
        return self.coordinator.is_available(self.item_name)

    @property
    def raw_state(self) -> str | None:
        """The item state exactly as openHAB reports it."""
        state = self.coordinator.states.get(self.item_name)
        if state in UNUSABLE_STATES:
            return None
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the openHAB identity and any in-flight command."""
        item = self.coordinator.items.get(self.item_name)
        attributes: dict[str, Any] = {
            "openhab_item": self.item_name,
            "openhab_type": item.type if item else None,
        }
        # Home Assistant's own last_changed covers state changes, but a command
        # is not a state change -- and for an item already holding the
        # commanded value, openHAB reports nothing else at all. Without this
        # there is no record that the item was commanded.
        last_command = self.coordinator.last_command(self.item_name)
        if last_command is not None:
            attributes["last_command"] = last_command.isoformat()

        pending = self.coordinator.async_pending_command(self.item_name)
        if pending is not None:
            attributes["pending_command"] = pending
        if item is not None and not item.autoupdate:
            # Worth surfacing: commands to these items only take effect if the
            # bound thing actually acts on them.
            attributes["autoupdate"] = False
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to this item's state signal and to connection changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATE_UPDATED.format(
                    self.coordinator.entry.entry_id, self.item_name
                ),
                self._handle_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_CONNECTION.format(self.coordinator.entry.entry_id),
                self._handle_connection,
            )
        )

    @callback
    def _handle_state(self, _value: str | None) -> None:
        """openHAB reported a new state. Never writes back to openHAB."""
        self.async_write_ha_state()

    @callback
    def _handle_connection(self) -> None:
        self.async_write_ha_state()

    # -- parsing helpers ---------------------------------------------------

    def _parsed_float(self) -> float | None:
        """Numeric part of the state, tolerating a unit suffix."""
        state = self.raw_state
        if state is None:
            return None
        try:
            value = float(state.split(" ", 1)[0])
        except (TypeError, ValueError):
            self._report_parse_failure(state)
            return None
        self.coordinator.async_report_parse_ok(self.item_name)
        return value

    def _report_parse_failure(self, value: str) -> None:
        _LOGGER.debug(
            "Entity %s could not parse openHAB state %r", self.entity_id, value
        )
        self.coordinator.async_report_parse_failure(self.item_name, value)


def unit_from_state(state: str | None) -> str | None:
    """Unit suffix of a ``Number:Dimension`` state, e.g. ``21.5 °C`` -> ``°C``."""
    if not state or " " not in state:
        return None
    _value, _sep, unit = state.partition(" ")
    unit = unit.strip()
    return unit or None
