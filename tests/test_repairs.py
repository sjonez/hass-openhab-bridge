"""Repair fix flows, opened the way Home Assistant opens them."""

from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.openhab_bridge import repairs
from custom_components.openhab_bridge.const import CONF_ITEMS, DOMAIN
from custom_components.openhab_bridge.coordinator import OpenHabCoordinator


@pytest.fixture
async def repairs_ready(hass):
    """The repairs component must be loaded to drive fix flows."""
    assert await async_setup_component(hass, "repairs", {})
    await hass.async_block_till_done()


async def _coordinator(hass, config_entry, mock_client) -> OpenHabCoordinator:
    coordinator = OpenHabCoordinator(hass, config_entry)
    coordinator.client = mock_client
    await coordinator.async_resync()
    hass.data.setdefault(DOMAIN, {})[config_entry.entry_id] = coordinator
    return coordinator


@pytest.mark.parametrize(
    "kind", [repairs.ISSUE_TYPE_MISMATCH, repairs.ISSUE_PARSE_FAILURES]
)
async def test_fix_flow_opens_with_populated_user_input(
    hass, config_entry, mock_client, repairs_ready, kind
):
    """Regression: opening these fix flows used to raise KeyError and 500.

    Home Assistant hands the init step a populated dict (the issue context),
    not None, so ``if user_input is not None`` treated the opening call as a
    submission. Calling the step directly with None -- as the earlier tests
    did -- never exercised that path.
    """
    await _coordinator(hass, config_entry, mock_client)
    data = {
        "entry_id": config_entry.entry_id,
        "item": "Kitchen_Light",
        "kind": kind,
    }
    flow = await repairs.async_create_fix_flow(hass, "irrelevant", data)
    flow.hass = hass

    step = await flow.async_step_init({"issue_id": "irrelevant"})
    assert step["type"] is FlowResultType.FORM
    assert step["step_id"] == "init"


async def test_platform_fix_flow_applies(
    hass, config_entry, mock_client, repairs_ready
):
    """Submitting the form changes the platform and clears the issue."""
    await _coordinator(hass, config_entry, mock_client)
    flow = repairs.PlatformRepairFlow(
        {"entry_id": config_entry.entry_id, "item": "Kitchen_Light"}
    )
    flow.hass = hass

    await flow.async_step_init({"issue_id": "x"})
    result = await flow.async_step_init({"platform": "binary_sensor"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_ITEMS]["Kitchen_Light"]["platform"] == (
        "binary_sensor"
    )


async def test_remap_search_rejects_bad_terms(
    hass, config_entry, mock_client, repairs_ready
):
    """The search step guards against no matches and too many."""
    await _coordinator(hass, config_entry, mock_client)
    flow = repairs.ItemMissingRepairFlow(
        {"entry_id": config_entry.entry_id, "item": "Kitchen_Light"}
    )
    flow.hass = hass

    # Opening call carries a dict; it must render the form, not search.
    opened = await flow.async_step_remap({"issue_id": "x"})
    assert opened["type"] is FlowResultType.FORM
    assert not opened.get("errors")

    missing = await flow.async_step_remap({"search": "no_such_item_anywhere"})
    assert missing["errors"] == {"search": "no_matches"}

    found = await flow.async_step_remap({"search": "outdoor"})
    assert found["step_id"] == "remap_pick"


async def test_remap_migrates_entity(hass, config_entry, mock_client, repairs_ready):
    """Re-mapping moves the item across and keeps the other items intact."""
    await _coordinator(hass, config_entry, mock_client)
    flow = repairs.ItemMissingRepairFlow(
        {"entry_id": config_entry.entry_id, "item": "Kitchen_Light"}
    )
    flow.hass = hass

    await flow.async_step_remap({"search": "outdoor"})
    result = await flow.async_step_remap_pick({"new_item": "Outdoor_Temp"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    items = config_entry.options[CONF_ITEMS]
    assert "Kitchen_Light" not in items
    assert "Outdoor_Temp" in items
    assert "Garage_Gate" in items


async def test_remove_branch_drops_item(hass, config_entry, mock_client, repairs_ready):
    """The remove branch stops exposing just that item."""
    await _coordinator(hass, config_entry, mock_client)
    flow = repairs.ItemMissingRepairFlow(
        {"entry_id": config_entry.entry_id, "item": "Kitchen_Light"}
    )
    flow.hass = hass

    result = await flow.async_step_remove()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(config_entry.options[CONF_ITEMS]) == {"Garage_Gate"}


async def test_issue_registered_as_fixable(hass, config_entry, mock_client):
    """Only the issues with a fix flow are marked fixable."""
    registry = ir.async_get(hass)
    repairs.async_raise_item_missing(hass, config_entry, "Kitchen_Light")
    repairs.async_raise_feedback_loop(hass, config_entry, "Garage_Gate")

    issues = {
        issue.issue_id: issue
        for issue in registry.issues.values()
        if issue.domain == DOMAIN
    }
    missing = next(i for k, i in issues.items() if "item_missing" in k)
    loop = next(i for k, i in issues.items() if "feedback_loop" in k)
    assert missing.is_fixable is True
    assert loop.is_fixable is False


async def test_platform_change_does_not_orphan_the_old_entity(
    hass, config_entry, mock_client
):
    """Regression: changing an item's entity type left the old one behind.

    The purge only dropped entities whose unique ID was no longer wanted. A
    platform change keeps the same unique ID, so the old entity survived as
    permanently unavailable alongside the new one.
    """
    from homeassistant.helpers import entity_registry as er

    from custom_components.openhab_bridge import _async_purge_removed_entities

    registry = er.async_get(hass)
    unique_id = f"{config_entry.entry_id}_Kitchen_Light"
    stale = registry.async_get_or_create(
        "switch", DOMAIN, unique_id, config_entry=config_entry
    )

    # Re-expose the same item as a sensor instead.
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            **config_entry.options,
            CONF_ITEMS: {"Kitchen_Light": {"platform": "sensor"}},
        },
    )
    _async_purge_removed_entities(hass, config_entry)

    assert registry.async_get(stale.entity_id) is None


async def test_purge_keeps_diagnostics_and_matching_entities(
    hass, config_entry, mock_client
):
    """The purge must not sweep up diagnostics or correctly-placed entities."""
    from homeassistant.helpers import entity_registry as er

    from custom_components.openhab_bridge import _async_purge_removed_entities

    registry = er.async_get(hass)
    diag = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{config_entry.entry_id}_diag_connected",
        config_entry=config_entry,
    )
    good = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{config_entry.entry_id}_Kitchen_Light",
        config_entry=config_entry,
    )

    _async_purge_removed_entities(hass, config_entry)

    assert registry.async_get(diag.entity_id) is not None
    assert registry.async_get(good.entity_id) is not None
