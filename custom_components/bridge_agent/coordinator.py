"""Thin aiohttp wrapper around the bridge's bearer API."""
from __future__ import annotations

import json

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

VERIFY_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
COMMAND_TIMEOUT = aiohttp.ClientTimeout(total=14, connect=5)


class CannotConnect(Exception):
    """The bridge answered nothing useful — wrong URL, down, or timeouts."""


class InvalidAuth(Exception):
    """The bridge returned 401 for this token."""


class BridgeUnreachable(Exception):
    pass


class BridgeRejected(Exception):
    pass


class BridgeClient:
    """POSTs to /command with the user's bearer token; also the two GETs the
    config flow verifies against (/health unauthenticated, /whoami with the
    token — /boards would 409 for a user with no Trello connected)."""

    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self.hass = hass
        self.url = url.rstrip("/")
        self.token = token

    @staticmethod
    def from_entry(hass: HomeAssistant, entry) -> "BridgeClient":
        return BridgeClient(hass, entry.data[CONF_URL], entry.data[CONF_TOKEN])

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _status(self, method: str, path: str, timeout, headers: dict) -> int:
        session = async_get_clientsession(self.hass)
        try:
            async with session.request(method, self.url + path,
                                       headers=headers, timeout=timeout) as resp:
                await resp.read()
                return resp.status
        except aiohttp.ClientError as e:
            raise CannotConnect() from e

    async def verify(self) -> None:
        """Map the two probes onto config-flow errors."""
        if await self._status("GET", "/health", VERIFY_TIMEOUT, {}) != 200:
            raise CannotConnect()
        status = await self._status("GET", "/whoami", VERIFY_TIMEOUT,
                                    self._headers())
        if status == 401:
            raise InvalidAuth()
        if status != 200:
            raise CannotConnect()

    async def command(self, text: str) -> str:
        """Returns the bridge's reply text; raises BridgeUnreachable or
        BridgeRejected."""
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                self.url + "/command",
                headers={**self._headers(), "Content-Type": "application/json"},
                data=json.dumps({"text": text, "source": "home_assistant"}),
                timeout=COMMAND_TIMEOUT,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(body, dict) \
                        and isinstance(body.get("reply"), str):
                    return body["reply"]
                if resp.status in (401, 403):
                    raise BridgeRejected()
                raise BridgeUnreachable()
        except BridgeRejected:
            raise
        except aiohttp.ClientError as e:
            raise BridgeUnreachable() from e
        except json.JSONDecodeError as e:
            raise BridgeUnreachable() from e


def get_client(hass: HomeAssistant, entry) -> BridgeClient:
    """One client per entry, rebuilt on reload so a new token is picked up."""
    return BridgeClient.from_entry(hass, entry)