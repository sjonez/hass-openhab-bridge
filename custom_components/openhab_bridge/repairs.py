"""Repair issues, and the guided flows that fix them."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ITEMS,
    DOMAIN,
    allowed_platforms_for,
    default_platform_for,
)

ISSUE_ITEM_MISSING = "item_missing"
ISSUE_TYPE_MISMATCH = "type_mismatch"
ISSUE_PARSE_FAILURES = "parse_failures"
ISSUE_UNREACHABLE = "unreachable"
ISSUE_WS_UNSUPPORTED = "websocket_unsupported"
ISSUE_FEEDBACK_LOOP = "feedback_loop"
ISSUE_UNCONFIRMED = "commands_unconfirmed"

FIXABLE = {ISSUE_ITEM_MISSING, ISSUE_TYPE_MISMATCH, ISSUE_PARSE_FAILURES}

# Above this, the search is too broad to render as a usable picker.
MAX_REMAP_MATCHES = 30


def _issue_id(kind: str, entry: ConfigEntry, item: str | None = None) -> str:
    """Issues are keyed per entry and per item, so two broken items give two issues."""
    return f"{kind}.{entry.entry_id}" + (f".{item}" if item else "")


def _raise(
    hass: HomeAssistant,
    entry: ConfigEntry,
    kind: str,
    *,
    item: str | None = None,
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    placeholders: dict[str, str] | None = None,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(kind, entry, item),
        is_fixable=kind in FIXABLE,
        severity=severity,
        translation_key=kind,
        translation_placeholders={
            "entry_title": entry.title,
            **(placeholders or {}),
        },
        data={"entry_id": entry.entry_id, "item": item, "kind": kind},
    )


def _clear(
    hass: HomeAssistant, entry: ConfigEntry, kind: str, item: str | None = None
) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(kind, entry, item))


# -- raise / clear helpers -------------------------------------------------


@callback
def async_raise_item_missing(
    hass: HomeAssistant, entry: ConfigEntry, item: str
) -> None:
    """An exposed item no longer exists in openHAB."""
    _raise(
        hass,
        entry,
        ISSUE_ITEM_MISSING,
        item=item,
        severity=ir.IssueSeverity.ERROR,
        placeholders={"item": item},
    )


@callback
def async_clear_item_missing(
    hass: HomeAssistant, entry: ConfigEntry, item: str
) -> None:
    """The item came back."""
    _clear(hass, entry, ISSUE_ITEM_MISSING, item)


@callback
def async_raise_type_mismatch(
    hass: HomeAssistant, entry: ConfigEntry, item: str, new_type: str, platform: str
) -> None:
    """The item type changed to something the chosen platform cannot represent."""
    _raise(
        hass,
        entry,
        ISSUE_TYPE_MISMATCH,
        item=item,
        severity=ir.IssueSeverity.ERROR,
        placeholders={"item": item, "new_type": new_type, "platform": platform},
    )


@callback
def async_clear_type_mismatch(
    hass: HomeAssistant, entry: ConfigEntry, item: str
) -> None:
    """The type is usable again."""
    _clear(hass, entry, ISSUE_TYPE_MISMATCH, item)


@callback
def async_raise_parse_failures(
    hass: HomeAssistant, entry: ConfigEntry, item: str, value: str, platform: str
) -> None:
    """The entity keeps receiving values it cannot interpret."""
    _raise(
        hass,
        entry,
        ISSUE_PARSE_FAILURES,
        item=item,
        placeholders={"item": item, "value": value, "platform": platform},
    )


@callback
def async_clear_parse_failures(
    hass: HomeAssistant, entry: ConfigEntry, item: str
) -> None:
    """Values parse again."""
    _clear(hass, entry, ISSUE_PARSE_FAILURES, item)


@callback
def async_raise_unreachable(
    hass: HomeAssistant, entry: ConfigEntry, error: str
) -> None:
    """Reconnects have been failing at the backoff ceiling for a long time."""
    _raise(hass, entry, ISSUE_UNREACHABLE, placeholders={"error": error})


@callback
def async_clear_unreachable(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """We are connected again."""
    _clear(hass, entry, ISSUE_UNREACHABLE)


@callback
def async_raise_websocket_unsupported(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """This openHAB has no /ws/events endpoint."""
    _raise(
        hass,
        entry,
        ISSUE_WS_UNSUPPORTED,
        severity=ir.IssueSeverity.ERROR,
    )


@callback
def async_raise_feedback_loop(
    hass: HomeAssistant, entry: ConfigEntry, item: str
) -> None:
    """Something outside this integration is echoing changes back as commands."""
    _raise(hass, entry, ISSUE_FEEDBACK_LOOP, item=item, placeholders={"item": item})


@callback
def async_clear_feedback_loop(
    hass: HomeAssistant, entry: ConfigEntry, item: str
) -> None:
    """The oscillation stopped."""
    _clear(hass, entry, ISSUE_FEEDBACK_LOOP, item)


@callback
def async_raise_unconfirmed(hass: HomeAssistant, entry: ConfigEntry, item: str) -> None:
    """Repeated commands to an autoupdate=false item went unacknowledged."""
    _raise(hass, entry, ISSUE_UNCONFIRMED, item=item, placeholders={"item": item})


@callback
def async_clear_unconfirmed(hass: HomeAssistant, entry: ConfigEntry, item: str) -> None:
    """A command was confirmed again."""
    _clear(hass, entry, ISSUE_UNCONFIRMED, item)


# -- fix flows -------------------------------------------------------------


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Entry point Home Assistant calls when the user clicks "Fix"."""
    kind = (data or {}).get("kind", "")
    if kind == ISSUE_ITEM_MISSING:
        return ItemMissingRepairFlow(data or {})
    if kind in (ISSUE_TYPE_MISMATCH, ISSUE_PARSE_FAILURES):
        return PlatformRepairFlow(data or {})
    return ConfirmRepairFlow()


class _EntryRepairFlow(RepairsFlow):
    """Shared plumbing for flows that edit one item's options."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Remember which entry and item the issue was raised for."""
        self._entry_id: str = data.get("entry_id", "")
        self._item: str = data.get("item", "")

    @property
    def _entry(self) -> ConfigEntry | None:
        return self.hass.config_entries.async_get_entry(self._entry_id)

    def _coordinator(self):
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id)

    async def _async_apply(self, items: dict[str, Any]) -> FlowResult:
        entry = self._entry
        if entry is None:
            return self.async_abort(reason="entry_gone")
        options = {**entry.options, CONF_ITEMS: items}
        self.hass.config_entries.async_update_entry(entry, options=options)
        return self.async_create_entry(title="", data={})


class ItemMissingRepairFlow(_EntryRepairFlow):
    """Offer to drop the item, or re-map it to the item it was renamed to."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Track the search results between the two re-map steps."""
        super().__init__(data)
        self._matches: list[Any] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose between removing and re-mapping."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["remap", "remove"],
            description_placeholders={"item": self._item},
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Stop exposing the item."""
        entry = self._entry
        if entry is None:
            return self.async_abort(reason="entry_gone")
        items = dict(entry.options.get(CONF_ITEMS, {}))
        items.pop(self._item, None)
        return await self._async_apply(items)

    async def async_step_remap(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Search for the replacement item.

        A single select over thousands of items is unusable: Home Assistant
        renders it as an unsearchable list that preselects its first entry, so
        one careless Submit re-maps to the wrong item. Searching first keeps
        the picker short and never preselects anything meaningful.
        """
        coordinator = self._coordinator()
        if self._entry is None or coordinator is None:
            return self.async_abort(reason="entry_gone")

        errors: dict[str, str] = {}
        # See the note in PlatformRepairFlow: an opening call carries a dict.
        if user_input and "search" in user_input:
            term = (user_input.get("search") or "").strip().lower()
            try:
                available = await coordinator.client.async_get_items()
            except Exception:
                return self.async_abort(reason="cannot_connect")

            exposed = set(self._entry.options.get(CONF_ITEMS, {}))
            matches = [
                item
                for item in available
                if item.name not in exposed
                and (term in item.name.lower() or term in (item.label or "").lower())
            ]
            if not matches:
                errors["search"] = "no_matches"
            elif len(matches) > MAX_REMAP_MATCHES:
                errors["search"] = "too_many_matches"
            else:
                self._matches = sorted(matches, key=lambda i: i.name.lower())
                return await self.async_step_remap_pick()

        return self.async_show_form(
            step_id="remap",
            data_schema=vol.Schema({vol.Required("search"): TextSelector()}),
            errors=errors,
            description_placeholders={
                "item": self._item,
                "limit": str(MAX_REMAP_MATCHES),
            },
        )

    async def async_step_remap_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose from the search results and migrate the entity onto it.

        The unique ID is derived from the item name, so re-mapping has to
        migrate the registry entry for history and automations to survive.
        """
        entry = self._entry
        if entry is None:
            return self.async_abort(reason="entry_gone")

        if user_input and "new_item" in user_input:
            new_name = user_input["new_item"]
            items = dict(entry.options.get(CONF_ITEMS, {}))
            config = items.pop(self._item, {})
            items[new_name] = config
            await _async_migrate_unique_id(self.hass, entry, self._item, new_name)
            return await self._async_apply(items)

        options = [
            SelectOptionDict(
                value=item.name,
                label=f"{item.label or item.name} — {item.name} ({item.type})",
            )
            for item in self._matches
        ]
        return self.async_show_form(
            step_id="remap_pick",
            data_schema=vol.Schema(
                {
                    vol.Required("new_item"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            description_placeholders={"item": self._item},
        )


class PlatformRepairFlow(_EntryRepairFlow):
    """Let the user pick a platform that suits the item's current type."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the platforms valid for the item's current openHAB type."""
        entry = self._entry
        coordinator = self._coordinator()
        if entry is None or coordinator is None:
            return self.async_abort(reason="entry_gone")

        # Home Assistant opens a repair flow by calling the init step with a
        # populated dict (the issue context), not None -- so "is not None" is
        # not a submission check here. Look for the field we actually asked
        # for instead.
        if user_input and "platform" in user_input:
            items = dict(entry.options.get(CONF_ITEMS, {}))
            config = dict(items.get(self._item, {}))
            config["platform"] = user_input["platform"]
            items[self._item] = config
            return await self._async_apply(items)

        item = coordinator.items.get(self._item)
        item_type = item.type if item else None
        group_type = item.group_type if item else None
        choices = allowed_platforms_for(item_type, group_type)
        default = default_platform_for(item_type, group_type)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("platform", default=default.value): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=p.value, label=p.value)
                                for p in choices
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={
                "item": self._item,
                "item_type": item_type or "unknown",
            },
        )


async def _async_migrate_unique_id(
    hass: HomeAssistant, entry: ConfigEntry, old_item: str, new_item: str
) -> None:
    """Carry the registry entry (and thus history) across a rename."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    old_unique_id = f"{entry.entry_id}_{old_item}"
    new_unique_id = f"{entry.entry_id}_{new_item}"
    for entity in list(registry.entities.values()):
        if (
            entity.config_entry_id == entry.entry_id
            and entity.unique_id == old_unique_id
        ):
            registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
