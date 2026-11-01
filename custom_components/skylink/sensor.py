"""Status sensors for each child and for the household."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SkylinkConfigEntry, SkylinkCoordinator
from .schedule import PERIOD_ORDER, Period, PeriodSummary, Status

HOUSEHOLD = "household"
CURRENT = "current"
STATUS_OPTIONS = [status.value for status in Status]


async def async_setup_entry(
    hass: HomeAssistant, entry: SkylinkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities: list[SkylinkSensor] = [
        SkylinkSensor(coordinator, child, label, period.value)
        for child, label in coordinator.children.items()
        for period in PERIOD_ORDER
    ]
    entities += [SkylinkSensor(coordinator, HOUSEHOLD, "Household", period.value) for period in PERIOD_ORDER]
    entities.append(SkylinkSensor(coordinator, HOUSEHOLD, "Household", CURRENT))
    async_add_entities(entities)


class SkylinkSensor(CoordinatorEntity[SkylinkCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(self, coordinator: SkylinkCoordinator, owner: str, label: str, key: str) -> None:
        super().__init__(coordinator)
        self._owner = owner
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.frame_id}_{owner}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.frame_id}_{owner}")},
            name=f"SkyLink {label}",
            manufacturer="Skylight",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _summary(self) -> PeriodSummary:
        snapshot = self.coordinator.data
        if self._key == CURRENT:
            return snapshot.current
        period = Period(self._key)
        return snapshot.household[period] if self._owner == HOUSEHOLD else snapshot.children[self._owner][period]

    @property
    def native_value(self) -> str:
        return self._summary.status.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self._summary
        attributes: dict[str, Any] = {
            "total": summary.total,
            "completed": summary.completed,
            "remaining": list(summary.remaining),
            "deadline": summary.deadline.isoformat(),
        }
        if self._key == CURRENT:
            attributes["period"] = self.coordinator.data.current_period.value
        return attributes
