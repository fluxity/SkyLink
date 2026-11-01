"""Constants and option helpers for SkyLink."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .api import Tokens
from .schedule import PERIOD_ORDER, Period, ScheduleConfig

DOMAIN = "skylink"

CONF_FRAME_ID = "frame_id"
CONF_CHILDREN = "children"
CONF_TOKENS = "tokens"
CONF_POLL_INTERVAL = "poll_interval"
CONF_UNTIMED_PERIOD = "untimed_period"
UNTIMED_IGNORE = "ignore"

DEFAULT_POLL_INTERVAL = 120
MIN_POLL_INTERVAL = 60
MAX_POLL_INTERVAL = 600
STALE_AFTER = timedelta(minutes=15)

DEFAULT_OPTIONS: dict[str, Any] = {
    "start_morning": "00:00:00",
    "start_afternoon": "12:00:00",
    "start_evening": "18:00:00",
    "deadline_weekday_morning": "12:00:00",
    "deadline_weekday_afternoon": "18:00:00",
    "deadline_weekday_evening": "23:59:00",
    "deadline_weekend_morning": "12:00:00",
    "deadline_weekend_afternoon": "18:00:00",
    "deadline_weekend_evening": "23:59:00",
    CONF_UNTIMED_PERIOD: Period.EVENING.value,
    CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
}


def _time_option(options: Mapping[str, Any], key: str) -> time:
    parsed = dt_util.parse_time(str(options.get(key, DEFAULT_OPTIONS[key])))
    return parsed if parsed is not None else dt_util.parse_time(DEFAULT_OPTIONS[key])


def schedule_config(options: Mapping[str, Any]) -> ScheduleConfig:
    untimed = options.get(CONF_UNTIMED_PERIOD, DEFAULT_OPTIONS[CONF_UNTIMED_PERIOD])
    return ScheduleConfig(
        starts={p: _time_option(options, f"start_{p.value}") for p in PERIOD_ORDER},
        weekday_deadlines={p: _time_option(options, f"deadline_weekday_{p.value}") for p in PERIOD_ORDER},
        weekend_deadlines={p: _time_option(options, f"deadline_weekend_{p.value}") for p in PERIOD_ORDER},
        untimed_period=None if untimed == UNTIMED_IGNORE else Period(untimed),
    )


def validate_options(options: Mapping[str, Any]) -> dict[str, str]:
    """Return form errors keyed by field, in the shape a config flow expects."""
    config = schedule_config(options)
    errors: dict[str, str] = {}
    for earlier, later in zip(PERIOD_ORDER, PERIOD_ORDER[1:]):
        if config.starts[later] <= config.starts[earlier]:
            errors[f"start_{later.value}"] = "starts_out_of_order"
    for period in PERIOD_ORDER:
        for day_type, deadlines in (("weekday", config.weekday_deadlines), ("weekend", config.weekend_deadlines)):
            if deadlines[period] <= config.starts[period]:
                errors[f"deadline_{day_type}_{period.value}"] = "deadline_before_start"
    return errors


def tokens_to_dict(tokens: Tokens) -> dict[str, Any]:
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_at": tokens.expires_at,
    }


def tokens_from_dict(data: Mapping[str, Any] | None) -> Tokens | None:
    if not data or not data.get("access_token"):
        return None
    return Tokens(data["access_token"], data.get("refresh_token"), data.get("expires_at"))
