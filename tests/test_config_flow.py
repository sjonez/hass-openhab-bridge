"""Config flow and the menu-driven options flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.openhab_bridge.api import OpenHabAuthError, OpenHabError
from custom_components.openhab_bridge.const import (
    CONF_BASE_URL,
    CONF_ITEMS,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DOMAIN,
)

USER_INPUT = {
    CONF_BASE_URL: "http://openhab.local:8080/",
    CONF_TOKEN: "secret-token",
    CONF_VERIFY_SSL: True,
}

CLIENT = "custom_components.openhab_bridge.config_flow.OpenHabClient"


async def test_user_flow(hass, items):
    """A valid URL and token create the entry."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The trailing slash must not survive, or a second entry could be added.
    assert result["data"][CONF_BASE_URL] == "http://openhab.local:8080"
    assert result["options"] == {CONF_ITEMS: {}}


async def test_invalid_auth(hass):
    """A rejected token is reported distinctly from an unreachable server."""
    with patch(f"{CLIENT}.async_get_items", side_effect=OpenHabAuthError):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect(hass):
    """An unreachable server is reported distinctly from a bad token."""
    with patch(f"{CLIENT}.async_get_items", side_effect=OpenHabError("boom")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_url_aborts(hass, config_entry, items):
    """One entry per openHAB server."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_add_item_keeps_existing(hass, config_entry, items):
    """Adding an item must not disturb the ones already configured."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        assert result["type"] is FlowResultType.MENU

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_items"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"items": ["Outdoor_Temp"]}
        )
        # The closest match for Number:Temperature is pre-selected.
        assert result["step_id"] == "add_types"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"Outdoor_Temp": "sensor"}
        )
        # sensor has advanced overrides, so this step isn't skipped.
        assert result["step_id"] == "add_advanced"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )

    items_config = result["data"][CONF_ITEMS]
    assert items_config["Outdoor_Temp"] == {"platform": "sensor"}
    assert items_config["Kitchen_Light"] == {"platform": "switch"}


async def test_options_remove_item(hass, config_entry, items):
    """Removing one item leaves the rest untouched."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "remove_items"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"items": ["Garage_Gate"]}
        )

    assert set(result["data"][CONF_ITEMS]) == {"Kitchen_Light"}


async def test_options_edit_item_platform_and_name(hass, config_entry, items):
    """Editing changes only the chosen item."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "edit_item"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"item": "Kitchen_Light"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"platform": "binary_sensor", "name": "Cooker Lamp"}
        )
        # binary_sensor has an advanced override (device class), so this
        # step isn't skipped.
        assert result["step_id"] == "edit_advanced"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )

    config = result["data"][CONF_ITEMS]
    assert config["Kitchen_Light"] == {
        "platform": "binary_sensor",
        "name": "Cooker Lamp",
    }
    assert config["Garage_Gate"] == {"platform": "switch"}


async def test_options_add_item_with_advanced_override(hass, config_entry, items):
    """A chosen device class and unit are saved; a blank state class is not."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_items"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"items": ["Outdoor_Temp"]}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"Outdoor_Temp": "sensor"}
        )
        assert result["step_id"] == "add_advanced"
        # A single item's advanced fields use plain, translated keys --
        # only a multi-item add needs the item name to disambiguate.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "device_class": "temperature",
                "state_class": "",
                "unit_of_measurement": "°F",
            },
        )

    config = result["data"][CONF_ITEMS]["Outdoor_Temp"]
    assert config["platform"] == "sensor"
    assert config["device_class"] == "temperature"
    assert config["unit_of_measurement"] == "°F"
    assert "state_class" not in config


async def test_options_add_multiple_items_with_advanced_overrides(
    hass, config_entry, items
):
    """Adding several items at once disambiguates each field by item name."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_items"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"items": ["Outdoor_Temp", "No_Label"]}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"Outdoor_Temp": "sensor", "No_Label": "sensor"}
        )
        assert result["step_id"] == "add_advanced"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "Outdoor_Temp (device class)": "humidity",
                "No_Label (unit)": "kg",
            },
        )

    config = result["data"][CONF_ITEMS]
    assert config["Outdoor_Temp"]["device_class"] == "humidity"
    assert config["No_Label"]["unit_of_measurement"] == "kg"


async def test_options_edit_advanced_override_cleared_on_platform_change(
    hass, config_entry, items
):
    """An override that no longer applies is dropped, not left stale.

    A switch has no device class field at all, so an override set while the
    item was a binary_sensor must not silently survive the platform change.
    """
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "edit_item"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"item": "Kitchen_Light"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"platform": "binary_sensor", "name": ""}
        )
        assert result["step_id"] == "edit_advanced"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"device_class": "power"}
        )

    config = result["data"][CONF_ITEMS]["Kitchen_Light"]
    assert config == {"platform": "binary_sensor", "device_class": "power"}

    # Now edit it back to a switch: the device class override must go away.
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "edit_item"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"item": "Kitchen_Light"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"platform": "switch", "name": ""}
        )
        # switch has no advanced overrides, so the step is skipped entirely.
        assert result["type"] is FlowResultType.CREATE_ENTRY

    assert result["data"][CONF_ITEMS]["Kitchen_Light"] == {"platform": "switch"}


async def test_options_connection_settings_keep_items(hass, config_entry, items):
    """Changing the connection must not touch the exposed items."""
    with patch(f"{CLIENT}.async_get_items", return_value=items):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "connection"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_BASE_URL: "http://openhab.local:8081",
                CONF_TOKEN: "new-token",
                CONF_VERIFY_SSL: False,
            },
        )

    assert set(result["data"][CONF_ITEMS]) == {"Kitchen_Light", "Garage_Gate"}
    assert config_entry.data[CONF_BASE_URL] == "http://openhab.local:8081"
    assert config_entry.data[CONF_TOKEN] == "new-token"
