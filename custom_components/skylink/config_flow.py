"""Config, reauth and options flows for SkyLink."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .api import SkylightAuthError, SkylightClient, SkylightConnectionError
from .const import (
    CONF_CHILDREN,
    CONF_FRAME_ID,
    CONF_POLL_INTERVAL,
    CONF_TOKENS,
    CONF_UNTIMED_PERIOD,
    DEFAULT_OPTIONS,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    UNTIMED_IGNORE,
    tokens_to_dict,
    validate_options,
)
from .models import Category
from .schedule import PERIOD_ORDER

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }
)
PASSWORD_SCHEMA = vol.Schema(
    {vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
)


def _frame_label(frame: Mapping[str, Any]) -> str:
    attributes = frame.get("attributes") or {}
    return str(attributes.get("name") or attributes.get("label") or frame.get("id"))


class SkylinkConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._email = ""
        self._password = ""
        self._client: SkylightClient | None = None
        self._frames: dict[str, str] = {}
        self._frame_id = ""
        self._categories: list[Category] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SkylinkOptionsFlow:
        return SkylinkOptionsFlow()

    def _new_client(self, email: str, password: str) -> SkylightClient:
        return SkylightClient(async_get_clientsession(self.hass), email, password)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email, self._password = user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            self._client = self._new_client(self._email, self._password)
            try:
                await self._client.async_login()
                frames = await self._client.async_list_frames()
            except SkylightAuthError:
                errors["base"] = "invalid_auth"
            except SkylightConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self._frames = {str(frame.get("id")): _frame_label(frame) for frame in frames}
                if not self._frames:
                    return self.async_abort(reason="no_calendars")
                if len(self._frames) == 1:
                    return await self._async_select_frame(next(iter(self._frames)))
                return await self.async_step_frame()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CREDENTIALS_SCHEMA, {CONF_EMAIL: self._email}),
            errors=errors,
        )

    async def async_step_frame(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_select_frame(user_input[CONF_FRAME_ID])
        options = [SelectOptionDict(value=frame_id, label=label) for frame_id, label in self._frames.items()]
        return self.async_show_form(
            step_id="frame",
            data_schema=vol.Schema(
                {vol.Required(CONF_FRAME_ID): SelectSelector(SelectSelectorConfig(options=options))}
            ),
        )

    async def _async_select_frame(self, frame_id: str) -> ConfigFlowResult:
        await self.async_set_unique_id(frame_id)
        self._abort_if_unique_id_configured()
        self._frame_id = frame_id
        assert self._client is not None
        try:
            self._categories = await self._client.async_list_categories(frame_id)
        except (SkylightAuthError, SkylightConnectionError):
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_children()

    async def async_step_children(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profiles = [c for c in self._categories if c.linked_to_profile or c.selected_for_chore_chart]
        if user_input is not None:
            selected = user_input[CONF_CHILDREN]
            if selected:
                assert self._client is not None and self._client.tokens is not None
                return self.async_create_entry(
                    title=self._frames[self._frame_id],
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_FRAME_ID: self._frame_id,
                        CONF_CHILDREN: {c.id: c.label for c in profiles if c.id in selected},
                        CONF_TOKENS: tokens_to_dict(self._client.tokens),
                    },
                    options=DEFAULT_OPTIONS,
                )
            errors["base"] = "no_children"
        defaults = [c.id for c in profiles if c.selected_for_chore_chart]
        options = [SelectOptionDict(value=c.id, label=c.label) for c in profiles]
        return self.async_show_form(
            step_id="children",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CHILDREN, default=defaults): SelectSelector(
                        SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._new_client(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            try:
                await client.async_login()
            except SkylightAuthError:
                errors["base"] = "invalid_auth"
            except SkylightConnectionError:
                errors["base"] = "cannot_connect"
            else:
                assert client.tokens is not None
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_TOKENS: tokens_to_dict(client.tokens),
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=PASSWORD_SCHEMA,
            errors=errors,
            description_placeholders={CONF_EMAIL: entry.data[CONF_EMAIL]},
        )


def _options_schema() -> vol.Schema:
    fields: dict[Any, Any] = {}
    for period in PERIOD_ORDER:
        fields[vol.Required(f"start_{period.value}")] = TimeSelector()
    for day_type in ("weekday", "weekend"):
        for period in PERIOD_ORDER:
            fields[vol.Required(f"deadline_{day_type}_{period.value}")] = TimeSelector()
    fields[vol.Required(CONF_UNTIMED_PERIOD)] = SelectSelector(
        SelectSelectorConfig(
            options=[*(p.value for p in PERIOD_ORDER), UNTIMED_IGNORE],
            translation_key=CONF_UNTIMED_PERIOD,
        )
    )
    fields[vol.Required(CONF_POLL_INTERVAL)] = vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=MIN_POLL_INTERVAL,
                max=MAX_POLL_INTERVAL,
                step=10,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Coerce(int),
    )
    return vol.Schema(fields)


class SkylinkOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            errors = validate_options(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)
            current = user_input
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(_options_schema(), current),
            errors=errors,
        )
