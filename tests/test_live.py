"""End-to-end tests against a real openHAB server, inside real Home Assistant.

These run the genuine Home Assistant core in-process -- real config entry
lifecycle, real entity and issue registries, real state machine -- but talk to
an actual openHAB instead of a mock. That covers everything the mocked tests
cannot: whether entities really appear, whether live events really reach the
state machine, and whether openHAB's own behaviour matches our assumptions.

Skipped entirely unless OPENHAB_URL and OPENHAB_TOKEN are set (see .env), so
CI and other contributors are unaffected.

Writes: only the item named by OPENHAB_TEST_ITEM is ever written to, and only
by the tests marked as such. Every other test here is read-only. If
OPENHAB_TEST_ITEM is unset, no test writes anything.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.openhab_bridge.api import OpenHabClient
from custom_components.openhab_bridge.const import (
    CONF_BASE_URL,
    CONF_ITEMS,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DOMAIN,
    EVENT_COMMAND_UNCONFIRMED,
    SERVICE_GET_ITEM_STATE,
    SERVICE_POST_UPDATE,
)

LIVE_URL = os.environ.get("OPENHAB_URL")
LIVE_TOKEN = os.environ.get("OPENHAB_TOKEN")
LIVE_VERIFY_SSL = os.environ.get("OPENHAB_VERIFY_SSL", "1") != "0"
TEST_ITEM = os.environ.get("OPENHAB_TEST_ITEM")

pytestmark = pytest.mark.skipif(
    not (LIVE_URL and LIVE_TOKEN),
    reason="live openHAB credentials not configured (set OPENHAB_URL/OPENHAB_TOKEN)",
)

needs_write_item = pytest.mark.skipif(
    not TEST_ITEM,
    reason="OPENHAB_TEST_ITEM not set; refusing to write to an unnominated item",
)


@pytest.fixture(autouse=True)
def use_real_dns():
    """Live tests must reach a real host, so undo the harness's DNS stub.

    Home Assistant's test harness swaps in a resolver that cannot look
    anything up. We patch it back to aiohttp's threaded resolver rather than
    using phcc's own disable fixture, which is an async generator whose
    finalizer runs after the event loop has closed.
    """
    from aiohttp.resolver import ThreadedResolver

    def make_resolver(*_args, **_kwargs):
        resolver = ThreadedResolver()
        # Home Assistant's own resolver teardown calls real_close(), which it
        # attaches when it builds the resolver itself.
        resolver.real_close = resolver.close
        return resolver

    with patch(
        "homeassistant.helpers.aiohttp_client._async_make_resolver",
        side_effect=make_resolver,
    ):
        yield


async def wait_for(check: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Poll until `check` passes or the timeout expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if check():
            return True
        await asyncio.sleep(0.25)
    return check()


@pytest.fixture
async def live_items(hass: HomeAssistant) -> list:
    """The real item list, fetched once."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    client = OpenHabClient(
        async_get_clientsession(hass, verify_ssl=LIVE_VERIFY_SSL),
        LIVE_URL,
        LIVE_TOKEN,
        LIVE_VERIFY_SSL,
    )
    return await client.async_get_items()


def _pick(items: list, item_type: str, exclude: set[str]) -> str | None:
    """A usable item of the given type, preferring one with a label."""
    for item in items:
        if (
            item.type == item_type
            and item.name not in exclude
            and item.state not in (None, "NULL", "UNDEF")
            and item.label
        ):
            return item.name
    return None


async def _setup(hass: HomeAssistant, items_config: dict) -> MockConfigEntry:
    """Set up a real config entry against the real openHAB."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=LIVE_URL,
        unique_id=LIVE_URL,
        data={
            CONF_BASE_URL: LIVE_URL,
            CONF_TOKEN: LIVE_TOKEN,
            CONF_VERIFY_SSL: LIVE_VERIFY_SSL,
        },
        options={CONF_ITEMS: items_config},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_are_created_with_openhab_naming(hass, live_items):
    """Entity IDs are prefixed and friendly names carry the openHAB label."""
    switch = _pick(live_items, "Switch", set())
    number = _pick(live_items, "Number:Temperature", set())
    assert switch, "no usable Switch item found on this openHAB"

    config = {switch: {"platform": "switch"}}
    if number:
        config[number] = {"platform": "sensor"}
    entry = await _setup(hass, config)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "switch", DOMAIN, f"{entry.entry_id}_{switch}"
    )
    assert entity_id, f"no switch entity was created for {switch}"
    assert entity_id.startswith("switch.openhab_")

    state = hass.states.get(entity_id)
    assert state is not None
    label = next(i.label for i in live_items if i.name == switch)
    assert state.attributes["friendly_name"] == f"{label} (openHAB)"
    assert state.attributes["openhab_item"] == switch


async def test_initial_state_matches_openhab(hass, live_items):
    """openHAB always wins: the entity mirrors REST at startup."""
    switch = _pick(live_items, "Switch", set())
    await _setup(hass, {switch: {"platform": "switch"}})

    expected = next(i.state for i in live_items if i.name == switch)
    entity_id = f"switch.openhab_{switch.lower()}"
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} was not created"
    assert state.state == ("on" if expected.upper() == "ON" else "off")


async def test_diagnostic_entities_report_connected(hass, live_items):
    """The connection diagnostics reflect a real, live WebSocket."""
    switch = _pick(live_items, "Switch", set())
    await _setup(hass, {switch: {"platform": "switch"}})

    connected = hass.states.get("binary_sensor.openhab_connected")
    assert connected is not None
    assert connected.state == "on", "WebSocket did not report connected"

    loop_detected = hass.states.get("binary_sensor.openhab_feedback_loop")
    assert loop_detected is not None
    assert loop_detected.state == "off"

    last_connected = hass.states.get("sensor.openhab_last_connected")
    assert last_connected is not None
    assert last_connected.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)


async def test_missing_item_raises_a_repair(hass, live_items):
    """Read-only: configure an item that cannot exist and expect a repair."""
    bogus = "ZZ_Does_Not_Exist_openhab_bridge_test"
    await _setup(hass, {bogus: {"platform": "switch"}})

    registry = ir.async_get(hass)
    issues = [
        issue
        for issue in registry.issues.values()
        if issue.domain == DOMAIN and "item_missing" in issue.issue_id
    ]
    assert len(issues) == 1
    assert bogus in issues[0].issue_id


async def test_get_item_state_action(hass, live_items):
    """The action reads any item, not just exposed ones."""
    switch = _pick(live_items, "Switch", set())
    other = _pick(live_items, "String", {switch})
    await _setup(hass, {switch: {"platform": "switch"}})

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_ITEM_STATE,
        {"item": other},
        blocking=True,
        return_response=True,
    )
    assert response["item"] == other
    assert response["type"] == "String"


async def test_options_flow_add_and_remove(hass, live_items):
    """Adding and removing items takes effect on reload, without a restart."""
    switch = _pick(live_items, "Switch", set())
    extra = _pick(live_items, "String", {switch})
    entry = await _setup(hass, {switch: {"platform": "switch"}})
    registry = er.async_get(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_items"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"items": [extra]}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {extra: "sensor"}
    )
    await hass.async_block_till_done()

    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{extra}")
    # The original entity must be untouched by the addition.
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{switch}")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_items"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"items": [extra]}
    )
    await hass.async_block_till_done()

    assert not registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{extra}"
    )
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_{switch}")


@needs_write_item
async def test_state_update_reaches_home_assistant(hass, live_items):
    """The whole path: postUpdate -> openHAB -> WebSocket -> HA state machine.

    Writes only to OPENHAB_TEST_ITEM. postUpdate sets state directly, so this
    works regardless of the item's autoupdate setting and does not ask any
    bound thing to do anything.
    """
    item = next((i for i in live_items if i.name == TEST_ITEM), None)
    assert item is not None, f"OPENHAB_TEST_ITEM '{TEST_ITEM}' does not exist"
    assert item.type == "Switch", (
        f"this test expects a Switch item; '{TEST_ITEM}' is a {item.type}"
    )

    await _setup(hass, {TEST_ITEM: {"platform": "switch"}})
    entity_id = f"switch.openhab_{TEST_ITEM.lower()}"
    assert hass.states.get(entity_id) is not None

    original = hass.states.get(entity_id).state
    target = "OFF" if original == "on" else "ON"

    await hass.services.async_call(
        DOMAIN,
        SERVICE_POST_UPDATE,
        {"item": TEST_ITEM, "state": target},
        blocking=True,
    )

    expected = "on" if target == "ON" else "off"
    reached = await wait_for(
        lambda: (
            (hass.states.get(entity_id) or None)
            and hass.states.get(entity_id).state == expected
        )
    )
    assert reached, (
        f"{entity_id} did not reach '{expected}' -- the live event never arrived"
    )

    # Put it back the way we found it.
    await hass.services.async_call(
        DOMAIN,
        SERVICE_POST_UPDATE,
        {"item": TEST_ITEM, "state": "ON" if original == "on" else "OFF"},
        blocking=True,
    )
    await wait_for(lambda: hass.states.get(entity_id).state == original)


@needs_write_item
async def test_command_from_home_assistant(hass, live_items):
    """Toggling the HA entity commands openHAB, and state follows openHAB.

    For an autoupdate=true item the echo comes back and the entity flips. For
    an autoupdate=false item the entity correctly does NOT move until the
    bound thing reports, which is the behaviour we want to confirm.
    """
    item = next((i for i in live_items if i.name == TEST_ITEM), None)
    assert item is not None

    await _setup(hass, {TEST_ITEM: {"platform": "switch"}})
    entity_id = f"switch.openhab_{TEST_ITEM.lower()}"
    original = hass.states.get(entity_id).state
    service = "turn_off" if original == "on" else "turn_on"
    expected = "off" if original == "on" else "on"

    print(
        f"\n  test item '{TEST_ITEM}' is a {item.type} with "
        f"autoupdate={'true' if item.autoupdate else 'FALSE'}"
    )

    await hass.services.async_call(
        "switch", service, {"entity_id": entity_id}, blocking=True
    )

    flipped = await wait_for(lambda: hass.states.get(entity_id).state == expected)
    print(f"  entity {'moved to' if flipped else 'stayed put; expected'} {expected!r}")

    if item.autoupdate:
        assert flipped, (
            "autoupdate is enabled for this item, so openHAB should have echoed "
            "the command back as a state change"
        )
        # Restore.
        await hass.services.async_call(
            "switch",
            "turn_on" if original == "on" else "turn_off",
            {"entity_id": entity_id},
            blocking=True,
        )
        await wait_for(lambda: hass.states.get(entity_id).state == original)
    else:
        assert not flipped, (
            "autoupdate is disabled for this item, so the entity must NOT move "
            "until the bound thing reports back -- moving would mean we guessed"
        )


@needs_write_item
async def test_unconfirmed_command_is_reported(hass, live_items):
    """A command that openHAB never acts on must surface, not vanish.

    Only meaningful for an autoupdate=false item with nothing bound to it:
    the command goes nowhere, so after the timeout we expect the event, the
    counter and the state to stay truthful. Takes over a minute by design --
    the window for these items is 60s.
    """
    item = next((i for i in live_items if i.name == TEST_ITEM), None)
    assert item is not None
    if item.autoupdate:
        pytest.skip("test item has autoupdate enabled; nothing would go unconfirmed")

    await _setup(hass, {TEST_ITEM: {"platform": "switch"}})
    entity_id = f"switch.openhab_{TEST_ITEM.lower()}"
    before = hass.states.get(entity_id).state
    events = async_capture_events(hass, EVENT_COMMAND_UNCONFIRMED)

    await hass.services.async_call(
        "switch",
        "turn_off" if before == "on" else "turn_on",
        {"entity_id": entity_id},
        blocking=True,
    )

    fired = await wait_for(lambda: len(events) > 0, timeout=90)
    assert fired, "no openhab_bridge_command_unconfirmed event was fired"
    assert events[0].data["item"] == TEST_ITEM

    counter = hass.states.get("sensor.openhab_unconfirmed_commands")
    assert counter is not None
    assert int(counter.state) >= 1
    # openHAB never moved, so neither should we.
    assert hass.states.get(entity_id).state == before


@needs_write_item
async def test_last_command_attribute_live(hass, live_items):
    """A real command through real openHAB must surface on the entity."""
    item = next((i for i in live_items if i.name == TEST_ITEM), None)
    assert item is not None

    await _setup(hass, {TEST_ITEM: {"platform": "switch"}})
    entity_id = f"switch.openhab_{TEST_ITEM.lower()}"
    assert hass.states.get(entity_id).attributes.get("last_command") is None

    current = hass.states.get(entity_id).state
    await hass.services.async_call(
        "switch",
        "turn_on" if current == "off" else "turn_off",
        {"entity_id": entity_id},
        blocking=True,
    )

    seen = await wait_for(
        lambda: hass.states.get(entity_id).attributes.get("last_command") is not None
    )
    assert seen, "last_command was never set by a real openHAB command event"
