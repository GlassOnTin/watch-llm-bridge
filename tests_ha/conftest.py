"""Shared setup for the bridge_agent HA tests.

These tests import homeassistant, so they run in .venv-ha
(pytest-homeassistant-custom-component), not the bridge's .venv:

    cd tests_ha && ../.venv-ha/bin/pytest

hass_config_dir is overridden to ./config so the loader picks up
custom_components/bridge_agent (a relative symlink into the repo).
"""
import pathlib
import sys

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

TEST_DIR = pathlib.Path(__file__).parent
sys.path.insert(0, str(TEST_DIR / "config"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def open_sockets(socket_enabled):
    """The stub bridge listens on localhost; the harness blocks sockets."""
    yield


@pytest.fixture(autouse=True)
async def setup_homeassistant_component(hass):
    """Entity exposure (async_should_expose) needs the homeassistant domain."""
    from homeassistant.setup import async_setup_component

    await async_setup_component(hass, "homeassistant", {})
    await hass.async_block_till_done()
    yield


@pytest.fixture
def hass_config_dir() -> str:
    return str(TEST_DIR / "config")