"""Shared fixtures."""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.openhab_bridge.api import OpenHabItem
from custom_components.openhab_bridge.const import (
    CONF_BASE_URL,
    CONF_ITEMS,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"


def _load_env_file() -> None:
    """Load the gitignored .env so the live tests can find credentials.

    Runs at conftest import, which is before test modules are imported, so
    module-level skip conditions in test_live.py see the values.
    """
    path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip("'\"")


_load_env_file()

if sys.platform == "win32":
    # The Home Assistant test harness blocks sockets but allows AF_UNIX ones,
    # which is enough for the asyncio event loop's self-pipe on Linux. Windows
    # has no AF_UNIX, so the ProactorEventLoop self-pipe is a TCP socket and is
    # blocked -- in a session-scoped fixture, before any per-test fixture could
    # re-enable it. Neutralise the block here, at import time.
    #
    # Linux and CI keep the protection, so an accidental real network call in a
    # test is still caught there.
    import pytest_socket

    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load this custom component during tests."""
    yield


@pytest.fixture
def items() -> list[OpenHabItem]:
    """A small, representative openHAB item set."""
    return [
        OpenHabItem(
            name="Kitchen_Light",
            type="Switch",
            label="Kitchen Light",
            state="OFF",
            autoupdate=True,
        ),
        OpenHabItem(
            name="Garage_Gate",
            type="Switch",
            label="Garage Gate",
            state="OFF",
            autoupdate=False,
        ),
        OpenHabItem(
            name="Outdoor_Temp",
            type="Number:Temperature",
            label="Outdoor Temperature",
            state="21.5 °C",
            autoupdate=True,
        ),
        OpenHabItem(name="No_Label", type="String", state="hello", autoupdate=True),
    ]


@pytest.fixture
def config_entry(hass) -> MockConfigEntry:
    """A configured entry exposing two items."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="http://openhab.local:8080",
        unique_id="http://openhab.local:8080",
        data={
            CONF_BASE_URL: "http://openhab.local:8080",
            CONF_TOKEN: "secret-token",
            CONF_VERIFY_SSL: True,
        },
        options={
            CONF_ITEMS: {
                "Kitchen_Light": {"platform": "switch"},
                "Garage_Gate": {"platform": "switch"},
            }
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_client(items):
    """A stand-in OpenHabClient that records writes."""
    client = AsyncMock()
    client.base_url = "http://openhab.local:8080"
    client.verify_ssl = True
    client.async_get_items.return_value = items
    client.async_get_state.return_value = "OFF"
    client.commands = []
    client.updates = []

    async def _send_command(name, command):
        client.commands.append((name, command))

    async def _post_update(name, state):
        client.updates.append((name, state))

    client.async_send_command.side_effect = _send_command
    client.async_post_update.side_effect = _post_update
    return client
