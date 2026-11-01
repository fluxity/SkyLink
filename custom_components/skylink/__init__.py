"""SkyLink: Skylight task status in Home Assistant."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SkylightClient, Tokens
from .const import CONF_TOKENS, tokens_from_dict, tokens_to_dict
from .coordinator import SkylinkConfigEntry, SkylinkCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SkylinkConfigEntry) -> bool:
    @callback
    def save_tokens(tokens: Tokens) -> None:
        # Refresh tokens rotate, so each one has to be saved as soon as it is issued.
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKENS: tokens_to_dict(tokens)})

    client = SkylightClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        tokens=tokens_from_dict(entry.data.get(CONF_TOKENS)),
        on_tokens=save_tokens,
    )
    coordinator = SkylinkCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_clock()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SkylinkConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
