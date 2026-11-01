"""Diagnostics that are safe to share: no credentials, names or task titles."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import SkylinkConfigEntry
from .schedule import PeriodSummary


def _summary(summary: PeriodSummary) -> dict[str, Any]:
    return {
        "status": summary.status.value,
        "total": summary.total,
        "completed": summary.completed,
        "deadline": summary.deadline.isoformat(),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: SkylinkConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    children = {
        f"child_{index}": {period.value: _summary(summary) for period, summary in periods.items()}
        for index, periods in enumerate(snapshot.children.values(), start=1)
    }
    return {
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "last_success": coordinator.last_success.isoformat() if coordinator.last_success else None,
        "tasks_day": coordinator.tasks_day.isoformat() if coordinator.tasks_day else None,
        "task_count": len(coordinator.tasks),
        "current_period": snapshot.current_period.value,
        "children": children,
        "household": {period.value: _summary(summary) for period, summary in snapshot.household.items()},
    }
