"""Tests for the bridge_agent custom conversation integration.

A stub bridge runs on 127.0.0.1:8111 with configurable behaviour; the config
flow and the conversation entity both talk to it over real aiohttp.
"""
from __future__ import annotations

from aiohttp import web
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.components import conversation
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import Context
import pytest

from custom_components.bridge_agent.const import DEFAULT_URL, DOMAIN

URL = "http://127.0.0.1:8111"
TOKEN = "test-token"


@pytest.fixture
async def stub_bridge(hass):
    """A stub bridge; tests flip `command_status` or set `drop_command` to
    simulate a rejected token / an unreachable bridge."""
    state = {"command_status": 200, "drop_command": False,
             "valid_tokens": {TOKEN}}

    async def health(request):
        return web.json_response({"ok": True})

    async def whoami(request):
        if request.headers.get("Authorization") != \
                f"Bearer {next(iter(state['valid_tokens']))}":
            return web.json_response({"detail": "bad token"}, status=401)
        return web.json_response({"username": "hatest"})

    async def command(request):
        if state["drop_command"]:
            request.transport.close()
            return  # unreachable
        body = await request.json()
        assert body["source"] == "home_assistant"
        return web.json_response(
            {"reply": f"echo: {body['text']}"}, status=state["command_status"]
        )

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/whoami", whoami)
    app.router.add_post("/command", command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8111)
    await site.start()
    try:
        yield state
    finally:
        await runner.cleanup()


async def _submit_flow(hass, url: str, token: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_TOKEN: token}
    )


async def test_config_flow_success_creates_entry(hass, stub_bridge):
    result = await _submit_flow(hass, URL, TOKEN)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bridge Agent"
    entry = result["result"]
    assert entry.data == {CONF_URL: URL, CONF_TOKEN: TOKEN}
    assert entry.unique_id == URL


async def test_config_flow_rejects_a_bad_token(hass, stub_bridge):
    result = await _submit_flow(hass, URL, "wrong")
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_config_flow_rejects_an_unreachable_url(hass, stub_bridge):
    result = await _submit_flow(hass, "http://127.0.0.1:9", TOKEN)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_config_flow_rejects_a_second_entry_for_the_same_url(hass, stub_bridge):
    first = await _submit_flow(hass, URL, TOKEN)
    assert first["type"] == FlowResultType.CREATE_ENTRY
    # single_config_entry aborts the second flow at init, before any form
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def _setup_entry(hass, stub_bridge, url=URL, token=TOKEN):
    result = await _submit_flow(hass, url, token)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    return entry


async def test_entity_loads_and_answers_echo(hass, stub_bridge):
    entry = await _setup_entry(hass, stub_bridge)
    assert entry.state is ConfigEntryState.LOADED
    agent = conversation.async_get_agent(hass, "conversation.bridge_agent")
    assert agent is not None
    result = await conversation.async_converse(
        hass, "hello", "conv-1", Context(), "en", "conversation.bridge_agent"
    )
    assert result.response.speech["plain"]["speech"] == "echo: hello"
    assert result.conversation_id == "conv-1"
    assert result.continue_conversation is False


async def test_a_rejected_token_gets_the_friendly_speech(hass, stub_bridge):
    await _setup_entry(hass, stub_bridge)
    stub_bridge["command_status"] = 401
    result = await conversation.async_converse(
        hass, "hello", None, Context(), "en", "conversation.bridge_agent"
    )
    speech = result.response.speech["plain"]["speech"]
    assert "rejected" in speech and "token" in speech


async def test_an_unreachable_bridge_gets_the_friendly_speech(hass, stub_bridge):
    await _setup_entry(hass, stub_bridge)
    stub_bridge["drop_command"] = True
    result = await conversation.async_converse(
        hass, "hello", None, Context(), "en", "conversation.bridge_agent"
    )
    assert result.response.speech["plain"]["speech"] == (
        "The bridge is unreachable right now."
    )


async def test_reauth_swaps_the_token_without_touching_the_url(hass, stub_bridge):
    entry = await _setup_entry(hass, stub_bridge)
    stub_bridge["valid_tokens"] = {"rotated"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
            "unique_id": entry.unique_id,
        },
        data=entry.data,
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TOKEN: "rotated"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    fresh = hass.config_entries.async_get_entry(entry.entry_id)
    assert fresh.data == {CONF_URL: URL, CONF_TOKEN: "rotated"}
    assert fresh.state is ConfigEntryState.LOADED
    result = await conversation.async_converse(
        hass, "hello again", None, Context(), "en", "conversation.bridge_agent"
    )
    assert result.response.speech["plain"]["speech"] == "echo: hello again"


async def test_unload_unregisters_the_agent(hass, stub_bridge):
    entry = await _setup_entry(hass, stub_bridge)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert conversation.async_get_agent(hass, "conversation.bridge_agent") is None