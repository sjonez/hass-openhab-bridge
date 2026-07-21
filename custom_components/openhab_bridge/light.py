"""Light entities backed by openHAB Switch, Dimmer or Color items."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ENTITY_ID_FORMAT,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import OH_COLOR, OH_DIMMER, base_item_type
from .coordinator import OpenHabCoordinator
from .entity import OpenHabEntity
from .platform_helper import build


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up openHAB lights."""
    async_add_entities(build(hass, entry, Platform.LIGHT, OpenHabLight))


def _percent_to_brightness(percent: float) -> int:
    return round(percent * 255 / 100)


def _brightness_to_percent(brightness: int) -> int:
    return round(brightness * 100 / 255)


class OpenHabLight(OpenHabEntity, LightEntity):
    """A Switch, Dimmer or Color item exposed as a light."""

    def __init__(self, coordinator: OpenHabCoordinator, item_name: str) -> None:
        """Pick the colour mode that matches the openHAB item type."""
        super().__init__(coordinator, item_name, ENTITY_ID_FORMAT)
        item = coordinator.items.get(item_name)
        self._base_type = base_item_type(item.type if item else None)

        if self._base_type == OH_COLOR:
            mode = ColorMode.HS
        elif self._base_type == OH_DIMMER:
            mode = ColorMode.BRIGHTNESS
        else:
            mode = ColorMode.ONOFF
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    def _hsb(self) -> tuple[float, float, float] | None:
        """Parse an openHAB Color state of the form ``h,s,b``."""
        state = self.raw_state
        if state is None:
            return None
        parts = state.split(",")
        if len(parts) != 3:
            self._report_parse_failure(state)
            return None
        try:
            hue, saturation, brightness = (float(part) for part in parts)
        except ValueError:
            self._report_parse_failure(state)
            return None
        self.coordinator.async_report_parse_ok(self.item_name)
        return hue, saturation, brightness

    @property
    def is_on(self) -> bool | None:
        """On when the item reports ON, or any non-zero brightness."""
        state = self.raw_state
        if state is None:
            return None
        if self._base_type == OH_COLOR:
            hsb = self._hsb()
            return None if hsb is None else hsb[2] > 0
        upper = state.upper()
        if upper == "ON":
            return True
        if upper == "OFF":
            return False
        value = self._parsed_float()
        return None if value is None else value > 0

    @property
    def brightness(self) -> int | None:
        """Brightness scaled from openHAB's 0-100 to HA's 0-255."""
        if self._base_type == OH_COLOR:
            hsb = self._hsb()
            return None if hsb is None else _percent_to_brightness(hsb[2])
        if self._base_type != OH_DIMMER:
            return None
        state = self.raw_state
        if state is None:
            return None
        if state.upper() in ("ON", "OFF"):
            return 255 if state.upper() == "ON" else 0
        value = self._parsed_float()
        return None if value is None else _percent_to_brightness(value)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Hue and saturation for Color items."""
        if self._base_type != OH_COLOR:
            return None
        hsb = self._hsb()
        return None if hsb is None else (hsb[0], hsb[1])

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Send the most specific command the item type supports."""
        if self._base_type == OH_COLOR and ATTR_HS_COLOR in kwargs:
            hue, saturation = kwargs[ATTR_HS_COLOR]
            brightness = kwargs.get(ATTR_BRIGHTNESS)
            if brightness is not None:
                level = _brightness_to_percent(brightness)
            else:
                current = self._hsb()
                level = int(current[2]) if current and current[2] else 100
            command = f"{round(hue)},{round(saturation)},{level}"
        elif ATTR_BRIGHTNESS in kwargs and self._base_type in (OH_DIMMER, OH_COLOR):
            command = str(_brightness_to_percent(kwargs[ATTR_BRIGHTNESS]))
        else:
            command = "ON"
        await self.coordinator.async_send_command(self.item_name, command)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Send OFF."""
        await self.coordinator.async_send_command(self.item_name, "OFF")
