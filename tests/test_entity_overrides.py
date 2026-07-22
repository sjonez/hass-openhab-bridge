"""Per-item device class, state class and unit overrides win over auto-derivation."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openhab_bridge.binary_sensor import OpenHabBinarySensor
from custom_components.openhab_bridge.const import (
    CONF_BASE_URL,
    CONF_ITEMS,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from custom_components.openhab_bridge.coordinator import OpenHabCoordinator
from custom_components.openhab_bridge.number import OpenHabNumber
from custom_components.openhab_bridge.sensor import OpenHabSensor


async def _coordinator(hass, items_config, oh_items, mock_client) -> OpenHabCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="http://openhab.local:8080",
        unique_id="http://openhab.local:8080-overrides",
        data={
            CONF_BASE_URL: "http://openhab.local:8080",
            CONF_TOKEN: "secret-token",
            CONF_VERIFY_SSL: True,
        },
        options={CONF_ITEMS: items_config},
    )
    entry.add_to_hass(hass)
    mock_client.async_get_items.return_value = oh_items
    coordinator = OpenHabCoordinator(hass, entry)
    coordinator.client = mock_client
    await coordinator.async_resync()
    return coordinator


async def test_sensor_overrides_win_over_derived_dimension(
    hass, items, mock_client
):
    """A configured device class, state class and unit beat the dimension guess."""
    config = {
        "Outdoor_Temp": {
            "platform": "sensor",
            "device_class": "humidity",
            "state_class": "total",
            "unit_of_measurement": "%",
        }
    }
    coordinator = await _coordinator(hass, config, items, mock_client)
    entity = OpenHabSensor(coordinator, "Outdoor_Temp")

    # Left alone, Number:Temperature would derive device_class=temperature,
    # state_class=measurement and unit=°C -- the override replaces all three.
    assert entity.device_class == "humidity"
    assert entity.state_class == "total"
    assert entity.native_unit_of_measurement == "%"


async def test_sensor_with_no_override_still_derives_normally(hass, items, mock_client):
    """Items without an override are unaffected by the new code path."""
    config = {"Outdoor_Temp": {"platform": "sensor"}}
    coordinator = await _coordinator(hass, config, items, mock_client)
    entity = OpenHabSensor(coordinator, "Outdoor_Temp")

    assert entity.device_class == "temperature"
    assert entity.state_class == "measurement"
    assert entity.native_unit_of_measurement == "°C"


async def test_number_device_class_and_unit_override(hass, items, mock_client):
    """A number entity's derived percentage unit can be overridden too."""
    config = {
        "Garage_Gate": {
            "platform": "number",
            "device_class": "distance",
            "unit_of_measurement": "cm",
        }
    }
    coordinator = await _coordinator(hass, config, items, mock_client)
    entity = OpenHabNumber(coordinator, "Garage_Gate")

    assert entity.device_class == "distance"
    assert entity.native_unit_of_measurement == "cm"


async def test_binary_sensor_device_class_override(hass, items, mock_client):
    """A Switch defaults to no device class; an override still applies."""
    config = {"Kitchen_Light": {"platform": "binary_sensor", "device_class": "power"}}
    coordinator = await _coordinator(hass, config, items, mock_client)
    entity = OpenHabBinarySensor(coordinator, "Kitchen_Light")

    assert entity.device_class == "power"


async def test_binary_sensor_override_beats_contact_default(hass, items, mock_client):
    """Contact items default to "opening"; an override still wins."""
    config = {"Garage_Gate": {"platform": "binary_sensor", "device_class": "problem"}}
    # Garage_Gate is a Switch in the shared fixture; give it a Contact type
    # here since that's the case with a non-empty default to beat.
    contact_items = [item for item in items if item.name != "Garage_Gate"]
    garage = next(item for item in items if item.name == "Garage_Gate")
    contact_items.append(
        type(garage)(
            name=garage.name,
            type="Contact",
            label=garage.label,
            state=garage.state,
            autoupdate=garage.autoupdate,
        )
    )
    coordinator = await _coordinator(hass, config, contact_items, mock_client)
    entity = OpenHabBinarySensor(coordinator, "Garage_Gate")

    assert entity.device_class == "problem"
