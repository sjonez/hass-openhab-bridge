"""The openHAB type -> Home Assistant platform mapping."""

from __future__ import annotations

import pytest
from homeassistant.const import Platform

from custom_components.openhab_bridge.const import (
    allowed_platforms_for,
    base_item_type,
    default_platform_for,
    item_dimension,
    platform_is_compatible,
)


@pytest.mark.parametrize(
    ("item_type", "expected"),
    [
        ("Switch", Platform.SWITCH),
        ("Contact", Platform.BINARY_SENSOR),
        ("Dimmer", Platform.LIGHT),
        ("Color", Platform.LIGHT),
        ("Number", Platform.SENSOR),
        ("Number:Temperature", Platform.SENSOR),
        ("Rollershutter", Platform.NUMBER),
        ("String", Platform.SENSOR),
        ("DateTime", Platform.SENSOR),
        # Unknown types must still produce something usable.
        ("SomethingNew", Platform.SENSOR),
        (None, Platform.SENSOR),
    ],
)
def test_default_platform(item_type, expected):
    """Each openHAB type maps to its closest Home Assistant platform."""
    assert default_platform_for(item_type) is expected


def test_dimension_is_stripped():
    """A dimension suffix must not confuse the type lookup."""
    assert base_item_type("Number:Temperature") == "Number"
    assert item_dimension("Number:Temperature") == "Temperature"
    assert item_dimension("Number") is None


def test_group_uses_group_type():
    """A group's aggregated state has the group type, so follow that."""
    assert default_platform_for("Group", "Switch") is Platform.SWITCH
    assert default_platform_for("Group", None) is Platform.SENSOR


def test_default_is_always_allowed():
    """The pre-selected platform must be one the user could have chosen."""
    for item_type in ("Switch", "Contact", "Dimmer", "Color", "Number", "String"):
        assert default_platform_for(item_type) in allowed_platforms_for(item_type)


def test_compatibility():
    """Compatibility drives the type-mismatch repair, so check both ways."""
    assert platform_is_compatible("Switch", Platform.SWITCH)
    assert platform_is_compatible("Switch", Platform.BINARY_SENSOR)
    assert not platform_is_compatible("String", Platform.SWITCH)
    # A Number gaining a dimension stays usable: no repair should fire.
    assert platform_is_compatible("Number:Temperature", Platform.SENSOR)
