"""Turns tasks, configuration and the current time into period statuses."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum

from .models import Task


class Period(str, Enum):
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"


class Status(str, Enum):
    NONE = "none"
    UPCOMING = "upcoming"
    PENDING = "pending"
    OVERDUE = "overdue"
    DONE = "done"


PERIOD_ORDER = (Period.MORNING, Period.AFTERNOON, Period.EVENING)
AGGREGATE_PRIORITY = (Status.OVERDUE, Status.PENDING, Status.UPCOMING, Status.DONE, Status.NONE)

# Skylight's own boundaries. A Routine belongs to the period it was created for, even
# if the household changes when its periods start.
SKYLIGHT_AFTERNOON = time(12, 0)
SKYLIGHT_EVENING = time(18, 0)


def _default_starts() -> dict[Period, time]:
    return {Period.MORNING: time(0, 0), Period.AFTERNOON: time(12, 0), Period.EVENING: time(18, 0)}


def _default_deadlines() -> dict[Period, time]:
    return {Period.MORNING: time(12, 0), Period.AFTERNOON: time(18, 0), Period.EVENING: time(23, 59)}


@dataclass(frozen=True)
class ScheduleConfig:
    starts: Mapping[Period, time] = field(default_factory=_default_starts)
    weekday_deadlines: Mapping[Period, time] = field(default_factory=_default_deadlines)
    weekend_deadlines: Mapping[Period, time] = field(default_factory=_default_deadlines)
    # None means tasks with no time of day are ignored.
    untimed_period: Period | None = Period.EVENING


@dataclass(frozen=True)
class PeriodSummary:
    status: Status
    total: int
    completed: int
    remaining: tuple[str, ...]
    deadline: datetime


@dataclass(frozen=True)
class Snapshot:
    children: Mapping[str, Mapping[Period, PeriodSummary]]
    household: Mapping[Period, PeriodSummary]
    current_period: Period

    @property
    def current(self) -> PeriodSummary:
        return self.household[self.current_period]


def counts_today(task: Task, today: date) -> bool:
    """Decide whether a task returned by the API belongs in today's totals.

    Late chores count until they are done, matching the Skylight display, and on the
    day they are finished. A missed Routine doesn't carry over. The API also returns
    open records from recurring series that have since ended, which the display hides.
    Undated tasks keep coming back after completion, so old ones are dropped too.
    """
    if task.start == today:
        return True
    if task.start is not None and (task.start > today or task.routine):
        return False
    if task.recurring_until is not None and task.recurring_until < today:
        return False
    return not task.is_done or task.completed_on == today


def _period_for_time(value: time, config: ScheduleConfig) -> Period:
    found = Period.MORNING
    for period in PERIOD_ORDER:
        if value >= config.starts[period]:
            found = period
    return found


def period_for_task(task: Task, config: ScheduleConfig) -> Period | None:
    if task.start_time is None:
        return config.untimed_period
    if task.routine:
        if task.start_time < SKYLIGHT_AFTERNOON:
            return Period.MORNING
        return Period.AFTERNOON if task.start_time < SKYLIGHT_EVENING else Period.EVENING
    return _period_for_time(task.start_time, config)


def current_period(now: datetime, config: ScheduleConfig) -> Period:
    return _period_for_time(now.time(), config)


def deadline_for(period: Period, now: datetime, config: ScheduleConfig) -> datetime:
    deadlines = config.weekend_deadlines if now.weekday() >= 5 else config.weekday_deadlines
    return datetime.combine(now.date(), deadlines[period], tzinfo=now.tzinfo)


def summarize_period(tasks: Iterable[Task], period: Period, now: datetime, config: ScheduleConfig) -> PeriodSummary:
    tasks = list(tasks)
    remaining = tuple(task.summary for task in tasks if not task.is_done)
    deadline = deadline_for(period, now, config)
    if not tasks:
        status = Status.NONE
    elif not remaining:
        status = Status.DONE
    elif now.time() < config.starts[period]:
        status = Status.UPCOMING
    elif now >= deadline:
        status = Status.OVERDUE
    else:
        status = Status.PENDING
    return PeriodSummary(status, len(tasks), len(tasks) - len(remaining), remaining, deadline)


def aggregate(summaries: Mapping[str, PeriodSummary], deadline: datetime) -> PeriodSummary:
    """Combine children's summaries. `summaries` is keyed by the child's display label."""
    statuses = {summary.status for summary in summaries.values()}
    status = next((s for s in AGGREGATE_PRIORITY if s in statuses), Status.NONE)
    remaining = tuple(f"{label}: {task}" for label, summary in summaries.items() for task in summary.remaining)
    total = sum(summary.total for summary in summaries.values())
    completed = sum(summary.completed for summary in summaries.values())
    return PeriodSummary(status, total, completed, remaining, deadline)


def build_snapshot(tasks: Iterable[Task], children: Mapping[str, str], now: datetime, config: ScheduleConfig) -> Snapshot:
    """`children` maps a tracked category id to its display label."""
    today = now.date()
    buckets: dict[tuple[str, Period], dict[str, Task]] = {}
    for task in tasks:
        if task.category_id not in children or not counts_today(task, today):
            continue
        period = period_for_task(task, config)
        if period is not None:
            buckets.setdefault((task.category_id, period), {})[task.id] = task

    child_summaries = {
        child: {
            period: summarize_period(buckets.get((child, period), {}).values(), period, now, config)
            for period in PERIOD_ORDER
        }
        for child in children
    }
    household = {
        period: aggregate(
            {label: child_summaries[child][period] for child, label in children.items()},
            deadline_for(period, now, config),
        )
        for period in PERIOD_ORDER
    }
    return Snapshot(child_summaries, household, current_period(now, config))
