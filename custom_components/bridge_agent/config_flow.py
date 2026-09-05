"""Config flow: URL + bearer token, verified against /health and /whoami."""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
import voluptuous as vol

from .const import DEFAULT_URL, DOMAIN
from .coordinator import BridgeClient, CannotConnect, InvalidAuth

_TOKEN = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL, default=DEFAULT_URL): str,
        vol.Required(CONF_TOKEN): _TOKEN,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): _TOKEN,
    }
)


class BridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for bridge_agent."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = cv.url(user_input[CONF_URL])
            client = BridgeClient(self.hass, url, user_input[CONF_TOKEN])
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            self._abort_if_unique_id_mismatch()
            try:
                await client.verify()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title="Bridge Agent",
                    data={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data) -> FlowResult:
        """Token rotation: keep the URL, ask for a new token."""
        self.entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> FlowResult:
        entry = self.entry
        errors: dict[str, str] = {}
        if user_input is not None:
            client = BridgeClient(self.hass, entry.data[CONF_URL],
                                  user_input[CONF_TOKEN])
            try:
                await client.verify()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={CONF_URL: entry.data[CONF_URL],
                          CONF_TOKEN: user_input[CONF_TOKEN]},
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )