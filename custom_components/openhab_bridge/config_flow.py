"""Config and options flows for openHAB Bridge."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import OpenHabAuthError, OpenHabClient, OpenHabError, OpenHabItem
from .const import (
    CONF_BASE_URL,
    CONF_ITEMS,
    CONF_NAME_OVERRIDE,
    CONF_PLATFORM,
    CONF_TOKEN,
    CONF_VERIFY_SSL,
    DOMAIN,
    STEP_ADD_ITEMS,
    STEP_CONNECTION,
    STEP_EDIT_ITEM,
    STEP_REMOVE_ITEMS,
    allowed_platforms_for,
    default_platform_for,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_VERIFY_SSL, default=True): BooleanSelector(),
    }
)


def _normalise_url(url: str) -> str:
    """Trailing slashes and case should not create a second entry."""
    parsed = urlparse(url.strip().rstrip("/"))
    scheme = (parsed.scheme or "http").lower()
    netloc = parsed.netloc.lower() or parsed.path.lower()
    path = parsed.path.rstrip("/") if parsed.netloc else ""
    return f"{scheme}://{netloc}{path}"


async def _async_validate(
    hass: Any, data: dict[str, Any]
) -> tuple[str | None, list[OpenHabItem]]:
    """Return (error_key, items). ``error_key`` is None on success."""
    client = OpenHabClient(
        async_get_clientsession(hass, verify_ssl=data[CONF_VERIFY_SSL]),
        data[CONF_BASE_URL],
        data[CONF_TOKEN],
        data[CONF_VERIFY_SSL],
    )
    try:
        items = await client.async_get_items()
    except OpenHabAuthError:
        return "invalid_auth", []
    except OpenHabError as err:
        _LOGGER.debug("openHAB validation failed: %s", err)
        return "cannot_connect", []
    return None, items


def _item_options(items: list[OpenHabItem]) -> list[SelectOptionDict]:
    """Selector options showing label and type, sorted by item name."""
    return [
        SelectOptionDict(
            value=item.name,
            label=f"{item.label or item.name} — {item.name} ({item.type})",
        )
        for item in sorted(items, key=lambda i: i.name.lower())
    ]


def _platform_selector(item: OpenHabItem | None) -> SelectSelector:
    """Platforms valid for this item type, defaulting to the closest match."""
    item_type = item.type if item else None
    group_type = item.group_type if item else None
    choices = allowed_platforms_for(item_type, group_type)
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=p.value, label=p.value) for p in choices],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class OpenHabConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup and reauthentication."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with no reauth entry."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the openHAB URL and API token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_BASE_URL] = _normalise_url(user_input[CONF_BASE_URL])
            await self.async_set_unique_id(user_input[CONF_BASE_URL])
            self._abort_if_unique_id_configured()

            error, _items = await _async_validate(self.hass, user_input)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=user_input[CONF_BASE_URL],
                    data=user_input,
                    options={CONF_ITEMS: {}},
                )

        return self.async_show_form(
            step_id="user", data_schema=CONNECTION_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Triggered when openHAB rejects the stored token."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a fresh API token."""
        assert self._reauth_entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {**self._reauth_entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            error, _items = await _async_validate(self.hass, data)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(self._reauth_entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
            description_placeholders={"url": self._reauth_entry.data[CONF_BASE_URL]},
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenHabOptionsFlow:
        """Return the options flow."""
        return OpenHabOptionsFlow()


class OpenHabOptionsFlow(OptionsFlow):
    """Menu-driven management of exposed items and connection settings.

    Each branch is a short, standalone operation, so adding one item later
    never means walking through the whole of initial setup again.
    """

    def __init__(self) -> None:
        """Nothing to carry across until a branch is chosen."""
        self._items: list[OpenHabItem] = []
        self._pending_names: list[str] = []
        self._editing: str | None = None

    @property
    def _configured(self) -> dict[str, dict[str, Any]]:
        return dict(self.config_entry.options.get(CONF_ITEMS, {}))

    def _save(self, items: dict[str, dict[str, Any]]) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, CONF_ITEMS: items}
        )

    async def _load_items(self) -> str | None:
        """Fetch the live item list; returns an error key on failure."""
        error, items = await _async_validate(self.hass, dict(self.config_entry.data))
        if error:
            return error
        self._items = items
        return None

    def _item(self, name: str) -> OpenHabItem | None:
        return next((item for item in self._items if item.name == name), None)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick what to do."""
        options = [STEP_ADD_ITEMS]
        if self._configured:
            options += [STEP_EDIT_ITEM, STEP_REMOVE_ITEMS]
        options.append(STEP_CONNECTION)
        return self.async_show_menu(step_id="init", menu_options=options)

    # -- add ---------------------------------------------------------------

    async def async_step_add_items(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose openHAB items that are not exposed yet."""
        if user_input is not None:
            self._pending_names = list(user_input["items"])
            if not self._pending_names:
                return self.async_abort(reason="nothing_selected")
            return await self.async_step_add_types()

        if error := await self._load_items():
            return self.async_abort(reason=error)

        exposed = set(self._configured)
        available = [item for item in self._items if item.name not in exposed]
        if not available:
            return self.async_abort(reason="no_items_available")

        return self.async_show_form(
            step_id=STEP_ADD_ITEMS,
            data_schema=vol.Schema(
                {
                    vol.Required("items", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=_item_options(available),
                            multiple=True,
                            custom_value=False,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_add_types(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the entity type for each newly selected item."""
        if user_input is not None:
            items = self._configured
            for name in self._pending_names:
                items[name] = {CONF_PLATFORM: user_input[name]}
            return self._save(items)

        schema: dict[Any, Any] = {}
        for name in self._pending_names:
            item = self._item(name)
            default = default_platform_for(
                item.type if item else None, item.group_type if item else None
            )
            schema[vol.Required(name, default=default.value)] = _platform_selector(item)

        return self.async_show_form(step_id="add_types", data_schema=vol.Schema(schema))

    # -- edit --------------------------------------------------------------

    async def async_step_edit_item(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which exposed item to edit."""
        if user_input is not None:
            self._editing = user_input["item"]
            return await self.async_step_edit_details()

        return self.async_show_form(
            step_id=STEP_EDIT_ITEM,
            data_schema=vol.Schema(
                {
                    vol.Required("item"): SelectSelector(
                        SelectSelectorConfig(
                            options=sorted(self._configured),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_edit_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change an item's platform or its friendly-name override."""
        assert self._editing is not None
        name = self._editing

        if user_input is not None:
            items = self._configured
            config = dict(items.get(name, {}))
            config[CONF_PLATFORM] = user_input[CONF_PLATFORM]
            override = (user_input.get(CONF_NAME_OVERRIDE) or "").strip()
            if override:
                config[CONF_NAME_OVERRIDE] = override
            else:
                config.pop(CONF_NAME_OVERRIDE, None)
            items[name] = config
            return self._save(items)

        if not self._items:
            await self._load_items()
        item = self._item(name)
        current = self._configured.get(name, {})
        default_platform = current.get(
            CONF_PLATFORM,
            default_platform_for(
                item.type if item else None, item.group_type if item else None
            ).value,
        )

        return self.async_show_form(
            step_id="edit_details",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PLATFORM, default=default_platform
                    ): _platform_selector(item),
                    vol.Optional(
                        CONF_NAME_OVERRIDE,
                        default=current.get(CONF_NAME_OVERRIDE, ""),
                    ): TextSelector(),
                }
            ),
            description_placeholders={
                "item": name,
                "item_type": item.type if item else "unknown",
                "label": (item.label if item and item.label else name),
            },
        )

    # -- remove ------------------------------------------------------------

    async def async_step_remove_items(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stop exposing selected items."""
        if user_input is not None:
            items = self._configured
            for name in user_input["items"]:
                items.pop(name, None)
            return self._save(items)

        return self.async_show_form(
            step_id=STEP_REMOVE_ITEMS,
            data_schema=vol.Schema(
                {
                    vol.Required("items", default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=sorted(self._configured),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # -- connection --------------------------------------------------------

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit URL, token and TLS verification without touching the items."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_BASE_URL] = _normalise_url(user_input[CONF_BASE_URL])
            error, _items = await _async_validate(self.hass, user_input)
            if error:
                errors["base"] = error
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, **user_input},
                    title=user_input[CONF_BASE_URL],
                )
                return self._save(self._configured)

        data = self.config_entry.data
        return self.async_show_form(
            step_id=STEP_CONNECTION,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BASE_URL, default=data[CONF_BASE_URL]
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                    vol.Required(CONF_TOKEN, default=data[CONF_TOKEN]): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Optional(
                        CONF_VERIFY_SSL, default=data.get(CONF_VERIFY_SSL, True)
                    ): BooleanSelector(),
                }
            ),
            errors=errors,
        )
