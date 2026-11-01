"""Runs the shipped blueprints as real automations inside Home Assistant."""

import asyncio
from datetime import timedelta
from pathlib import Path
import shutil

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / "skylink"
SENSOR = "sensor.skylink_ann_morning"
LIGHT = "light.kitchen"
PHONES = ["notify.mobile_app_one", "notify.mobile_app_two"]


def set_status(hass, state, remaining=("Teeth", "Bed")):
    hass.states.async_set(SENSOR, state, {"friendly_name": "SkyLink Ann Morning", "remaining": list(remaining)})


@pytest.fixture(autouse=True)
def blueprint_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    shutil.copytree(BLUEPRINTS, tmp_path / "blueprints" / "automation" / "skylink")


async def use_blueprint(hass, name, **inputs):
    assert await async_setup_component(
        hass,
        "automation",
        {"automation": {"use_blueprint": {"path": f"skylink/{name}.yaml", "input": {"status_sensor": SENSOR, **inputs}}}},
    )
    await hass.async_block_till_done()
    automations = hass.states.async_entity_ids("automation")
    assert len(automations) == 1, "the blueprint failed validation"


async def settle():
    # async_block_till_done would wait out a reminder delay, which never ends under a frozen clock.
    for _ in range(50):
        await asyncio.sleep(0)


async def change(hass, state, **kwargs):
    set_status(hass, state, **kwargs)
    await settle()


async def advance(hass, freezer, minutes):
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await settle()


class TestNotification:
    @pytest.fixture
    def one(self, hass):
        return async_mock_service(hass, "notify", "mobile_app_one")

    @pytest.fixture
    def two(self, hass):
        return async_mock_service(hass, "notify", "mobile_app_two")

    async def test_overdue_notifies_every_phone_with_remaining_tasks(self, hass, one, two):
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES)
        await change(hass, "overdue")
        assert len(one) == len(two) == 1
        assert one[0].data["title"] == "SkyLink Ann Morning is overdue"
        assert one[0].data["message"] == "Still to do: Teeth, Bed"
        assert one[0].data["data"]["tag"] == SENSOR

    async def test_restart_or_outage_recovery_does_not_notify(self, hass, one):
        set_status(hass, "unavailable")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1])
        await change(hass, "overdue")
        assert one == []

    async def test_no_reminders_by_default(self, hass, freezer, one):
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1])
        await change(hass, "overdue")
        await advance(hass, freezer, 500)
        assert len(one) == 1

    async def test_reminders_stop_at_the_limit_and_list_current_tasks(self, hass, freezer, one):
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1], repeat_minutes=10, max_reminders=2)
        await change(hass, "overdue")
        await change(hass, "overdue", remaining=("Bed",))
        for _ in range(5):
            await advance(hass, freezer, 11)
        assert len(one) == 3
        assert one[-1].data["message"] == "Still to do: Bed"

    async def test_finishing_stops_reminders(self, hass, freezer, one):
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1], repeat_minutes=10, max_reminders=5)
        await change(hass, "overdue")
        await change(hass, "done")
        for _ in range(3):
            await advance(hass, freezer, 11)
        assert len(one) == 1

    async def test_going_unavailable_stops_reminders(self, hass, freezer, one):
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1], repeat_minutes=10, max_reminders=5)
        await change(hass, "overdue")
        await change(hass, "unavailable")
        for _ in range(3):
            await advance(hass, freezer, 11)
        assert len(one) == 1

    async def test_done_notification_is_opt_in(self, hass, one):
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1])
        await change(hass, "done")
        assert one == []

    async def test_done_notification_when_enabled(self, hass, one):
        set_status(hass, "overdue")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES[:1], notify_when_done=True)
        await change(hass, "done")
        assert one[0].data["title"] == "SkyLink Ann Morning is done"

    async def test_a_failing_phone_does_not_block_the_next(self, hass, two):
        async def fail(call):
            raise HomeAssistantError("phone unreachable")

        hass.services.async_register("notify", "mobile_app_one", fail)
        set_status(hass, "pending")
        await use_blueprint(hass, "notify_overdue", notify_services=PHONES)
        await change(hass, "overdue")
        assert len(two) == 1


class TestLight:
    @pytest.fixture
    def turn_on(self, hass):
        return async_mock_service(hass, "light", "turn_on")

    @pytest.fixture
    def turn_off(self, hass):
        return async_mock_service(hass, "light", "turn_off")

    async def setup(self, hass, **inputs):
        set_status(hass, "upcoming")
        await use_blueprint(hass, "status_light", light_target={"entity_id": LIGHT}, **inputs)

    @pytest.mark.parametrize(
        ("state", "color"),
        [("overdue", [255, 0, 0]), ("pending", [255, 160, 0]), ("done", [0, 255, 0])],
    )
    async def test_each_status_sets_its_color(self, hass, turn_on, state, color):
        await self.setup(hass)
        await change(hass, state)
        assert turn_on[-1].data["rgb_color"] == color
        assert turn_on[-1].data["brightness_pct"] == 60
        assert turn_on[-1].data["entity_id"] == [LIGHT]

    async def test_upcoming_color(self, hass, turn_on):
        set_status(hass, "pending")
        await use_blueprint(hass, "status_light", light_target={"entity_id": LIGHT})
        await change(hass, "upcoming")
        assert turn_on[-1].data["rgb_color"] == [0, 80, 255]

    async def test_custom_color_and_brightness(self, hass, turn_on):
        await self.setup(hass, overdue_color=[1, 2, 3], brightness=10)
        await change(hass, "overdue")
        assert turn_on[-1].data["rgb_color"] == [1, 2, 3]
        assert turn_on[-1].data["brightness_pct"] == 10

    async def test_turns_off_when_no_tasks(self, hass, turn_on, turn_off):
        await self.setup(hass)
        await change(hass, "none")
        assert len(turn_off) == 1
        assert turn_on == []

    async def test_can_leave_the_light_alone_when_no_tasks(self, hass, turn_on, turn_off):
        await self.setup(hass, off_when_no_tasks=False)
        await change(hass, "none")
        assert turn_off == []
        assert turn_on == []

    async def test_unavailable_keeps_the_last_color(self, hass, turn_on, turn_off):
        await self.setup(hass)
        await change(hass, "unavailable")
        assert turn_on == []
        assert turn_off == []

    async def test_recovery_from_unavailable_sets_the_color(self, hass, turn_on):
        set_status(hass, "unavailable")
        await use_blueprint(hass, "status_light", light_target={"entity_id": LIGHT})
        await change(hass, "done")
        assert turn_on[-1].data["rgb_color"] == [0, 255, 0]

    async def test_attribute_changes_do_not_resend(self, hass, turn_on):
        set_status(hass, "pending", remaining=("Teeth", "Bed"))
        await use_blueprint(hass, "status_light", light_target={"entity_id": LIGHT})
        await change(hass, "pending", remaining=("Bed",))
        assert turn_on == []

    async def test_reapplies_when_home_assistant_starts(self, hass, turn_on):
        hass.set_state(CoreState.not_running)
        set_status(hass, "overdue")
        await use_blueprint(hass, "status_light", light_target={"entity_id": LIGHT})
        assert turn_on == []
        await hass.async_start()
        await hass.async_block_till_done()
        assert turn_on[-1].data["rgb_color"] == [255, 0, 0]
