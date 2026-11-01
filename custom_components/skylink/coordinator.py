"""Polls Skylight and recalculates statuses as the clock moves."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import SkylightAuthError, SkylightClient, SkylightConnectionError
from .const import (
    CONF_CHILDREN,
    CONF_FRAME_ID,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    STALE_AFTER,
    schedule_config,
)
from .models import Task
from .schedule import Snapshot, build_snapshot

_LOGGER = logging.getLogger(__name__)

type SkylinkConfigEntry = ConfigEntry[SkylinkCoordinator]


class SkylinkCoordinator(DataUpdateCoordinator[Snapshot]):
    config_entry: SkylinkConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SkylinkConfigEntry, client: SkylightClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)),
        )
        self.client = client
        self.frame_id: str = entry.data[CONF_FRAME_ID]
        self.children: dict[str, str] = entry.data[CONF_CHILDREN]
        self.schedule = schedule_config(entry.options)
        self.tasks: list[Task] = []
        self.tasks_day: date | None = None
        self.last_success: datetime | None = None
        # While polls are failing, statuses stay frozen. A task may have been finished
        # since the last good poll, so a deadline passing now proves nothing.
        self.holding = False

    def _snapshot(self) -> Snapshot:
        return build_snapshot(self.tasks, self.children, dt_util.now(), self.schedule)

    async def _async_update_data(self) -> Snapshot:
        today = dt_util.now().date()
        try:
            self.tasks = await self.client.async_list_tasks(self.frame_id, today)
        except SkylightAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SkylightConnectionError as err:
            # A brief failure shouldn't flip a light, but data from another day is
            # wrong however recent it is.
            recent = self.last_success is not None and dt_util.utcnow() - self.last_success < STALE_AFTER
            if recent and self.tasks_day == today and self.data is not None:
                _LOGGER.debug("Keeping recent data after a failed poll: %s", err)
                self.holding = True
                return self.data
            raise UpdateFailed(str(err)) from err
        self.holding = False
        self.tasks_day = today
        self.last_success = dt_util.utcnow()
        return self._snapshot()

    def async_start_clock(self) -> None:
        """Recalculate every minute so deadlines apply on time, without an API call."""
        self.config_entry.async_on_unload(async_track_time_change(self.hass, self._async_tick, second=0))

    @callback
    def _async_tick(self, _now: datetime) -> None:
        if self.tasks_day != dt_util.now().date():
            self.config_entry.async_create_background_task(
                self.hass, self.async_refresh(), f"{DOMAIN}_day_rollover"
            )
            return
        if self.holding or not self.last_update_success:
            return
        self.data = self._snapshot()
        self.async_update_listeners()
