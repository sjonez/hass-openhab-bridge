"""The openHAB Bridge integration."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from . import repairs
from .api import OpenHabError, OpenHabNotFoundError
from .const import (
    ATTR_COMMAND,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_ITEM,
    ATTR_STATE,
    CONF_ITEMS,
    CONF_PLATFORM,
    DOMAIN,
    PLATFORMS,
    SERVICE_GET_ITEM_STATE,
    SERVICE_POST_UPDATE,
    SERVICE_SEND_COMMAND,
    UNREACHABLE_REPAIR_AFTER,
)
from .coordinator import OpenHabCoordinator

_LOGGER = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = timedelta(seconds=60)

_ITEM_SCHEMA = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_ITEM): cv.string,
}
GET_STATE_SCHEMA = vol.Schema(_ITEM_SCHEMA)
POST_UPDATE_SCHEMA = vol.Schema({**_ITEM_SCHEMA, vol.Required(ATTR_STATE): cv.string})
SEND_COMMAND_SCHEMA = vol.Schema(
    {**_ITEM_SCHEMA, vol.Required(ATTR_COMMAND): cv.string}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up openHAB Bridge from a config entry."""
    coordinator = OpenHabCoordinator(hass, entry)
    await coordinator.async_setup()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    _async_purge_removed_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda _now: _async_check_health(hass, entry, coordinator),
            HEALTH_CHECK_INTERVAL,
        )
    )
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: OpenHabCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_GET_ITEM_STATE,
                SERVICE_POST_UPDATE,
                SERVICE_SEND_COMMAND,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload so added, edited and removed items take effect immediately."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_purge_removed_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop registry entries that no longer match the configuration.

    Two cases, and the second is easy to miss: an item is no longer exposed at
    all, or it is still exposed but on a different platform. Changing an
    item's entity type creates a new entity on the new platform and leaves the
    old one behind, permanently unavailable, because its unique ID is still
    "wanted" -- just not on that platform.
    """
    registry = er.async_get(hass)
    wanted = {
        f"{entry.entry_id}_{item}": config.get(CONF_PLATFORM)
        for item, config in entry.options.get(CONF_ITEMS, {}).items()
    }
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        # Diagnostic entities use a different unique-id shape and are kept.
        if entity.unique_id.startswith(f"{entry.entry_id}_diag_"):
            continue
        if entity.unique_id not in wanted:
            _LOGGER.debug("Removing entity %s for unexposed item", entity.entity_id)
            registry.async_remove(entity.entity_id)
        elif entity.domain != wanted[entity.unique_id]:
            _LOGGER.debug(
                "Removing entity %s: item now exposed as %s",
                entity.entity_id,
                wanted[entity.unique_id],
            )
            registry.async_remove(entity.entity_id)


def _async_check_health(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: OpenHabCoordinator
) -> None:
    """Raise a repair if reconnects have been failing for a long time."""
    stats = coordinator.websocket.stats
    if not stats.connected and stats.seconds_at_ceiling > UNREACHABLE_REPAIR_AFTER:
        repairs.async_raise_unreachable(
            hass, entry, stats.last_error or "unknown error"
        )


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_ITEM_STATE):
        return

    def _resolve(call: ServiceCall) -> OpenHabCoordinator:
        entries: dict[str, OpenHabCoordinator] = hass.data.get(DOMAIN, {})
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_id is None:
            if len(entries) == 1:
                return next(iter(entries.values()))
            raise ServiceValidationError(
                "Several openHAB servers are configured; specify config_entry_id."
            )
        if entry_id not in entries:
            raise ServiceValidationError(
                f"No loaded openHAB Bridge entry with id '{entry_id}'."
            )
        return entries[entry_id]

    async def _get_item_state(call: ServiceCall) -> ServiceResponse:
        coordinator = _resolve(call)
        name = call.data[ATTR_ITEM]
        try:
            item = await coordinator.client.async_get_item(name)
        except OpenHabNotFoundError as err:
            raise ServiceValidationError(
                f"openHAB item '{name}' does not exist."
            ) from err
        except OpenHabError as err:
            raise HomeAssistantError(f"Reading '{name}' failed: {err}") from err
        return {
            "item": item.name,
            "state": item.state,
            "type": item.type,
            "label": item.label,
        }

    async def _post_update(call: ServiceCall) -> None:
        coordinator = _resolve(call)
        await coordinator.async_post_update(call.data[ATTR_ITEM], call.data[ATTR_STATE])

    async def _send_command(call: ServiceCall) -> None:
        coordinator = _resolve(call)
        await coordinator.async_send_command(
            call.data[ATTR_ITEM], call.data[ATTR_COMMAND]
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ITEM_STATE,
        _get_item_state,
        schema=GET_STATE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_POST_UPDATE, _post_update, schema=POST_UPDATE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, _send_command, schema=SEND_COMMAND_SCHEMA
    )
