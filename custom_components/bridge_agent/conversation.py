"""The conversation agent entity: proxies dictated text to the bridge."""
from __future__ import annotations

import logging

from homeassistant.components import conversation
from homeassistant.components.conversation.chat_log import AssistantContent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.intent import IntentResponse

from .const import DOMAIN
from .coordinator import BridgeRejected, BridgeUnreachable

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([BridgeAgent(hass, entry)])


class BridgeAgent(conversation.ConversationEntity):
    """One entry = one bridge user's token, like one watch."""

    # Explicit name: with _attr_name=None the entity registry falls back to
    # the unique_id for the object id (2026.9), which would make the entity
    # conversation.bridge_agent_<entry_id>.
    _attr_name = "Bridge Agent"
    _attr_has_entity_name = True
    _attr_supported_features = 0
    _attr_supports_streaming = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.client = hass.data[DOMAIN][entry.entry_id]
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | str:
        # MATCH_ALL: HA offers the agent for every language. The bridge's own
        # prompts and aliases are English-only; the docs note this.
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self.entry)

    async def _async_handle_message(self, user_input, chat_log):
        """POST the dictated text to the bridge and speak its reply."""
        intent_response = IntentResponse(language=user_input.language)
        try:
            reply = await self.client.command(user_input.text)
        except BridgeRejected:
            intent_response.async_set_speech(
                "The bridge rejected this token. Open the bridge dashboard "
                "to get a fresh one."
            )
        except BridgeUnreachable:
            intent_response.async_set_speech(
                "The bridge is unreachable right now."
            )
        else:
            chat_log.async_add_assistant_content_without_tools(
                AssistantContent(agent_id=user_input.agent_id, content=reply)
            )
            intent_response.async_set_speech(reply)
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
            continue_conversation=False,
        )