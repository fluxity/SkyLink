"""Typed views of the Skylight API's JSON:API resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

DONE_STATUSES = {"complete", "skipped"}


@dataclass(frozen=True)
class Task:
    id: str
    summary: str
    category_id: str | None
    start: date | None
    start_time: time | None
    routine: bool
    status: str
    completed_on: date | None
    recurring_until: date | None = None

    @property
    def is_done(self) -> bool:
        return self.status in DONE_STATUSES


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    linked_to_profile: bool
    selected_for_chore_chart: bool


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_time(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def _resources(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _category_id(item: dict[str, Any]) -> str | None:
    relationship = (item.get("relationships") or {}).get("category") or {}
    data = relationship.get("data") or {}
    return str(data["id"]) if data.get("id") is not None else None


def parse_tasks(payload: Any) -> list[Task]:
    tasks = []
    for item in _resources(payload):
        attrs = item.get("attributes") or {}
        tasks.append(
            Task(
                id=str(item.get("id")),
                summary=str(attrs.get("summary") or ""),
                category_id=_category_id(item),
                start=_parse_date(attrs.get("start")),
                start_time=_parse_time(attrs.get("start_time")),
                routine=attrs.get("routine") is True,
                status=str(attrs.get("status") or "pending"),
                completed_on=_parse_date(attrs.get("completed_on")),
                recurring_until=_parse_date(attrs.get("recurring_until")),
            )
        )
    return tasks


def parse_categories(payload: Any) -> list[Category]:
    categories = []
    for item in _resources(payload):
        attrs = item.get("attributes") or {}
        categories.append(
            Category(
                id=str(item.get("id")),
                label=str(attrs.get("label") or item.get("id")),
                linked_to_profile=attrs.get("linked_to_profile") is True,
                selected_for_chore_chart=attrs.get("selected_for_chore_chart") is True,
            )
        )
    return categories
