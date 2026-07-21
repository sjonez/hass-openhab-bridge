"""Thin async REST client for the openHAB API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class OpenHabError(Exception):
    """Base error for openHAB API failures."""


class OpenHabConnectionError(OpenHabError):
    """openHAB could not be reached."""


class OpenHabAuthError(OpenHabError):
    """The API token was rejected."""


class OpenHabNotFoundError(OpenHabError):
    """The requested item does not exist."""


@dataclass(slots=True)
class OpenHabItem:
    """An openHAB item as far as this integration cares about it."""

    name: str
    type: str
    label: str | None = None
    state: str | None = None
    group_type: str | None = None
    autoupdate: bool = True
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OpenHabItem:
        """Build from a ``/rest/items`` payload entry."""
        return cls(
            name=data["name"],
            type=data.get("type", "String"),
            label=_clean_label(data.get("label")),
            state=data.get("state"),
            group_type=data.get("groupType") or None,
            autoupdate=_parse_autoupdate(data.get("metadata")),
            tags=list(data.get("tags") or []),
        )


def _clean_label(label: str | None) -> str | None:
    """Drop openHAB's state presentation pattern from a label.

    openHAB label syntax is ``Label [pattern]``, e.g.
    ``Outdoor Temperature [%.1f °C]``. The pattern is a formatting
    instruction for openHAB's own UIs, not part of the name, and would
    otherwise end up in the Home Assistant friendly name.
    """
    if not label:
        return None
    cleaned = re.sub(r"\s*\[[^\]]*\]\s*$", "", label).strip()
    return cleaned or label.strip()


def _parse_autoupdate(metadata: dict[str, Any] | None) -> bool:
    """Read the ``autoupdate`` metadata namespace.

    Absent metadata means openHAB's default prediction is active, i.e. True.
    Only an explicit "false" turns it off.
    """
    if not metadata:
        return True
    entry = metadata.get("autoupdate")
    if not isinstance(entry, dict):
        return True
    return str(entry.get("value", "true")).strip().lower() != "false"


class OpenHabClient:
    """Talks to the openHAB REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
    ) -> None:
        """Initialise the client. ``session`` is Home Assistant's shared session."""
        self._session = session
        self._base = URL(base_url.rstrip("/"))
        self._token = token
        self._verify_ssl = verify_ssl

    @property
    def base_url(self) -> str:
        """The configured openHAB base URL."""
        return str(self._base)

    def websocket_url(self) -> URL:
        """The events WebSocket URL, with the access token attached."""
        scheme = "wss" if self._base.scheme == "https" else "ws"
        return (
            self._base.with_scheme(scheme)
            .joinpath("ws/events")
            .with_query(accessToken=self._token)
        )

    @property
    def verify_ssl(self) -> bool:
        """Whether TLS certificates are verified."""
        return self._verify_ssl

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: str | None = None,
        content_type: str | None = None,
        accept: str = "application/json",
    ) -> aiohttp.ClientResponse:
        url = self._base.joinpath(path.lstrip("/"))
        headers = self._headers(accept)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = await self._session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                ssl=self._verify_ssl,
                timeout=REQUEST_TIMEOUT,
            )
        except TimeoutError as err:
            raise OpenHabConnectionError(f"Timeout contacting {url}") from err
        except aiohttp.ClientError as err:
            raise OpenHabConnectionError(f"Cannot reach {url}: {err}") from err

        if response.status in (401, 403):
            response.close()
            raise OpenHabAuthError("openHAB rejected the API token")
        if response.status == 404:
            response.close()
            raise OpenHabNotFoundError(f"{url} not found")
        if response.status >= 400:
            text = await response.text()
            raise OpenHabError(f"openHAB returned {response.status} for {url}: {text}")
        return response

    async def async_get_items(self) -> list[OpenHabItem]:
        """List every item, including its autoupdate metadata.

        If openHAB refuses the metadata query (older versions, or a token
        without metadata rights) we retry without it. Items then default to
        autoupdate=False, which is the cautious assumption: we wait for real
        confirmation rather than expecting an immediate state change.
        """
        try:
            response = await self._request(
                "GET", "/rest/items", params={"metadata": "autoupdate"}
            )
            payload = await response.json()
            return [OpenHabItem.from_json(entry) for entry in payload]
        except OpenHabAuthError:
            raise
        except OpenHabError:
            _LOGGER.debug(
                "openHAB did not return autoupdate metadata; assuming autoupdate=false "
                "for all items so commands are treated as unconfirmed until echoed"
            )

        response = await self._request("GET", "/rest/items")
        payload = await response.json()
        items = [OpenHabItem.from_json(entry) for entry in payload]
        for item in items:
            item.autoupdate = False
        return items

    async def async_get_item(self, name: str) -> OpenHabItem:
        """Fetch a single item."""
        response = await self._request(
            "GET", f"/rest/items/{name}", params={"metadata": "autoupdate"}
        )
        return OpenHabItem.from_json(await response.json())

    async def async_get_state(self, name: str) -> str:
        """Fetch the raw string state of a single item.

        This endpoint returns plain text, and openHAB rejects the request with
        a 400 if we ask for JSON.
        """
        response = await self._request(
            "GET", f"/rest/items/{name}/state", accept="text/plain"
        )
        return (await response.text()).strip()

    async def async_post_update(self, name: str, state: str) -> None:
        """postUpdate: write the item state directly."""
        response = await self._request(
            "PUT",
            f"/rest/items/{name}/state",
            data=state,
            content_type="text/plain",
        )
        response.close()

    async def async_send_command(self, name: str, command: str) -> None:
        """sendCommand: ask the bound thing to act.

        Note this does not necessarily change the item state -- see the
        autoupdate handling in the coordinator.
        """
        response = await self._request(
            "POST",
            f"/rest/items/{name}",
            data=command,
            content_type="text/plain",
        )
        response.close()

    async def async_validate(self) -> None:
        """Raise if the URL or token is unusable. Used by the config flow."""
        response = await self._request("GET", "/rest/items", params={"limit": "1"})
        response.close()
