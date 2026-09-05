"""The watch-llm-bridge conversation agent."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import get_client

PLATFORMS = ["conversation"]


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = get_client(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok