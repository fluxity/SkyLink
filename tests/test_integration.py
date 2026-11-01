"""End-to-end tests of the integration inside a real Home Assistant core, with a fake client."""

from datetime import date, time, timedelta
import json
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from custom_components.skylink.api import SkylightAuthError, SkylightConnectionError, Tokens
from custom_components.skylink.const import (
    CONF_CHILDREN,
    CONF_FRAME_ID,
    CONF_TOKENS,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from custom_components.skylink.diagnostics import async_get_config_entry_diagnostics
from custom_components.skylink.models import Category, Task

FRIDAY = date(2026, 10, 30)
SATURDAY = date(2026, 10, 31)
# 07:00 in Boston, where the tests run.
FRIDAY_0700_UTC = "2026-10-30 11:00:00+00:00"
OPTIONS = {**DEFAULT_OPTIONS, "deadline_weekday_morning": "07:45:00"}
CHILDREN = {"a": "Ann", "b": "Ben"}
MORNING = "sensor.skylink_ann_morning"
HOUSEHOLD_MORNING = "sensor.skylink_household_morning"
CURRENT = "sensor.skylink_household_current"


def routine(id_, child="a", hour=6, status="pending", day=FRIDAY):
    return Task(id_, f"task {id_}", child, day, time(hour), True, status, None)


class FakeClient:
    """Stands in for SkylightClient and records what the integration asks of it."""

    instances = []
    tasks = []
    error = None
    frames = [{"id": "9", "attributes": {"name": "Home"}}]
    categories = [
        Category("a", "Ann", True, True),
        Category("b", "Ben", True, True),
        Category("c", "Parent", True, False),
        Category("d", "Pets", False, False),
    ]
    login_error = None
    valid_password = "pw"

    def __init__(self, session, email, password, *, tokens=None, on_tokens=None, **_):
        self.email, self.password, self.tokens, self.on_tokens = email, password, tokens, on_tokens
        self.task_calls = []
        FakeClient.instances.append(self)

    async def async_login(self):
        if FakeClient.login_error:
            raise FakeClient.login_error
        if self.password != FakeClient.valid_password:
            raise SkylightAuthError("bad password")
        self.tokens = Tokens("access", "refresh", 123.0)

    async def async_list_frames(self):
        return FakeClient.frames

    async def async_list_categories(self, frame_id):
        return FakeClient.categories

    async def async_list_tasks(self, frame_id, day):
        self.task_calls.append((frame_id, day))
        if FakeClient.error:
            raise FakeClient.error
        return list(FakeClient.tasks)


@pytest.fixture(autouse=True)
def fake_client():
    FakeClient.instances = []
    FakeClient.tasks = []
    FakeClient.error = None
    FakeClient.login_error = None
    FakeClient.frames = [{"id": "9", "attributes": {"name": "Home"}}]
    with (
        patch("custom_components.skylink.SkylightClient", FakeClient),
        patch("custom_components.skylink.config_flow.SkylightClient", FakeClient),
    ):
        yield FakeClient


@pytest.fixture
async def boston(hass, freezer):
    await hass.config.async_set_time_zone("America/New_York")
    freezer.move_to(FRIDAY_0700_UTC)


async def setup_entry(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="9",
        title="Home",
        data={
            CONF_EMAIL: "parent@example.com",
            CONF_PASSWORD: "pw",
            CONF_FRAME_ID: "9",
            CONF_CHILDREN: CHILDREN,
            CONF_TOKENS: {"access_token": "access", "refresh_token": "refresh", "expires_at": 123.0},
        },
        options=options or OPTIONS,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def advance(hass, freezer, **delta):
    freezer.tick(timedelta(**delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def client():
    return FakeClient.instances[-1]


@pytest.mark.usefixtures("boston")
class TestEntities:
    async def test_creates_the_planned_entities(self, hass):
        await setup_entry(hass)
        ids = set(hass.states.async_entity_ids("sensor"))
        expected = {
            f"sensor.skylink_{owner}_{period}"
            for owner in ("ann", "ben", "household")
            for period in ("morning", "afternoon", "evening")
        } | {CURRENT}
        assert ids == expected

    async def test_state_and_attributes(self, hass):
        FakeClient.tasks = [routine("1"), routine("2", status="complete")]
        await setup_entry(hass)
        state = hass.states.get(MORNING)
        assert state.state == "pending"
        assert state.attributes["total"] == 2
        assert state.attributes["completed"] == 1
        assert state.attributes["remaining"] == ["task 1"]
        assert state.attributes["deadline"] == "2026-10-30T07:45:00-04:00"
        assert state.attributes["options"] == ["none", "upcoming", "pending", "overdue", "done"]

    async def test_household_and_current(self, hass):
        FakeClient.tasks = [routine("1", "a", status="complete"), routine("2", "b")]
        await setup_entry(hass)
        assert hass.states.get(HOUSEHOLD_MORNING).state == "pending"
        assert hass.states.get(HOUSEHOLD_MORNING).attributes["remaining"] == ["Ben: task 2"]
        current = hass.states.get(CURRENT)
        assert current.state == "pending"
        assert current.attributes["period"] == "morning"


@pytest.mark.usefixtures("boston")
class TestClockAndPolling:
    async def test_deadline_applies_on_time_without_an_api_call(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass, {**OPTIONS, "poll_interval": 600})
        assert hass.states.get(MORNING).state == "pending"
        # 07:00:00 plus 45 minutes is the deadline. Polling is every 10 minutes, so
        # stop just past the last poll to prove the tick alone changes the state.
        for _ in range(4):
            await advance(hass, freezer, seconds=600)
        calls = len(client().task_calls)
        await advance(hass, freezer, minutes=5)
        assert hass.states.get(MORNING).state == "overdue"
        assert len(client().task_calls) == calls

    async def test_poll_picks_up_a_completed_task(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass)
        FakeClient.tasks = [routine("1", status="complete")]
        await advance(hass, freezer, seconds=121)
        assert hass.states.get(MORNING).state == "done"

    async def test_period_change_moves_current(self, hass, freezer):
        FakeClient.tasks = [routine("1", hour=14)]
        await setup_entry(hass)
        assert hass.states.get(CURRENT).state == "none"
        await advance(hass, freezer, hours=5)
        current = hass.states.get(CURRENT)
        assert current.attributes["period"] == "afternoon"
        assert current.state == "pending"

    async def test_short_outage_holds_then_goes_unavailable_then_recovers(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass)
        FakeClient.error = SkylightConnectionError("down")
        await advance(hass, freezer, seconds=121)
        assert hass.states.get(MORNING).state == "pending"
        await advance(hass, freezer, minutes=12)
        assert hass.states.get(MORNING).state == "pending"
        await advance(hass, freezer, minutes=2)
        assert hass.states.get(MORNING).state == STATE_UNAVAILABLE

        # The clock tick must not bring a failed sensor back on its own.
        await advance(hass, freezer, minutes=1)
        assert hass.states.get(MORNING).state == STATE_UNAVAILABLE

        FakeClient.error = None
        await advance(hass, freezer, seconds=121)
        assert hass.states.get(MORNING).state != STATE_UNAVAILABLE

    async def test_deadline_passing_during_an_outage_never_reads_as_overdue(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass)
        freezer.move_to("2026-10-30 11:40:00+00:00")
        await advance(hass, freezer, seconds=0)
        assert hass.states.get(MORNING).state == "pending"

        # The deadline is 07:45. Every state from here on must be pending or unavailable.
        FakeClient.error = SkylightConnectionError("down")
        seen = set()
        for _ in range(25):
            await advance(hass, freezer, minutes=1)
            seen.add(hass.states.get(MORNING).state)
        assert seen == {"pending", STATE_UNAVAILABLE}

    async def test_status_catches_up_when_the_outage_ends(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass)
        freezer.move_to("2026-10-30 11:40:00+00:00")
        await advance(hass, freezer, seconds=0)
        FakeClient.error = SkylightConnectionError("down")
        for _ in range(8):
            await advance(hass, freezer, minutes=1)
        assert hass.states.get(MORNING).state == "pending"
        FakeClient.error = None
        await advance(hass, freezer, minutes=2)
        assert hass.states.get(MORNING).state == "overdue"

    async def test_midnight_fetches_the_new_day_at_once(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass)
        freezer.move_to("2026-10-31 03:59:50+00:00")
        await advance(hass, freezer, seconds=1)
        FakeClient.tasks = [routine("2", day=SATURDAY)]
        freezer.move_to("2026-10-31 04:00:00+00:00")
        await advance(hass, freezer, seconds=0)
        assert client().task_calls[-1] == ("9", SATURDAY)
        state = hass.states.get(MORNING)
        assert state.state == "pending"
        assert state.attributes["remaining"] == ["task 2"]

    async def test_outage_over_midnight_discards_yesterday(self, hass, freezer):
        FakeClient.tasks = [routine("1")]
        await setup_entry(hass)
        FakeClient.error = SkylightConnectionError("down")
        freezer.move_to("2026-10-31 03:59:00+00:00")
        await advance(hass, freezer, seconds=0)
        freezer.move_to("2026-10-31 04:00:00+00:00")
        await advance(hass, freezer, seconds=0)
        assert hass.states.get(MORNING).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("boston")
class TestLifecycle:
    async def test_setup_failure_retries(self, hass):
        FakeClient.error = SkylightConnectionError("down")
        entry = await setup_entry(hass)
        assert entry.state is ConfigEntryState.SETUP_RETRY

    async def test_rejected_login_at_setup_starts_reauth(self, hass):
        FakeClient.error = SkylightAuthError("nope")
        entry = await setup_entry(hass)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]

    async def test_rejected_login_during_polling_starts_reauth(self, hass, freezer):
        await setup_entry(hass)
        FakeClient.error = SkylightAuthError("nope")
        await advance(hass, freezer, seconds=121)
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]

    async def test_rotated_tokens_are_saved_without_a_reload(self, hass):
        entry = await setup_entry(hass)
        instance = client()
        instance.on_tokens(Tokens("new-access", "new-refresh", 999.0))
        await hass.async_block_till_done()
        assert entry.data[CONF_TOKENS]["refresh_token"] == "new-refresh"
        assert client() is instance
        assert entry.state is ConfigEntryState.LOADED

    async def test_saved_tokens_are_passed_to_the_client(self, hass):
        await setup_entry(hass)
        assert client().tokens == Tokens("access", "refresh", 123.0)

    async def test_unload(self, hass):
        entry = await setup_entry(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert hass.states.get(MORNING).state == STATE_UNAVAILABLE

    async def test_diagnostics_contain_nothing_personal(self, hass):
        FakeClient.tasks = [routine("1"), routine("2", "b", status="complete")]
        entry = await setup_entry(hass)
        result = await async_get_config_entry_diagnostics(hass, entry)
        text = json.dumps(result)
        for secret in ("parent@example.com", '"pw"', "access", "refresh", "Ann", "Ben", "task 1", "task 2"):
            assert secret not in text, secret
        assert result["children"]["child_1"]["morning"]["status"] == "pending"
        assert result["household"]["morning"]["total"] == 2


@pytest.mark.usefixtures("boston")
class TestConfigFlow:
    async def start(self, hass, password="pw"):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.FORM
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "parent@example.com", CONF_PASSWORD: password}
        )

    async def test_single_calendar_skips_to_profiles_and_creates_entry(self, hass):
        result = await self.start(hass)
        assert result["step_id"] == "children"
        schema = result["data_schema"]
        field = next(iter(schema.schema))
        assert field.default() == ["a", "b"]
        offered = [o["value"] for o in schema.schema[field].config["options"]]
        assert offered == ["a", "b", "c"]

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CHILDREN: ["a"]})
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Home"
        assert result["data"][CONF_CHILDREN] == {"a": "Ann"}
        assert result["data"][CONF_TOKENS]["refresh_token"] == "refresh"
        assert result["data"][CONF_FRAME_ID] == "9"
        assert result["options"] == DEFAULT_OPTIONS
        assert hass.states.get(MORNING) is not None

    async def test_several_calendars_asks_which(self, hass):
        FakeClient.frames = [{"id": "9", "attributes": {"name": "Home"}}, {"id": "10", "attributes": {"name": "Cabin"}}]
        result = await self.start(hass)
        assert result["step_id"] == "frame"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_FRAME_ID: "10"})
        assert result["step_id"] == "children"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CHILDREN: ["b"]})
        await hass.async_block_till_done()
        assert result["title"] == "Cabin"

    async def test_wrong_password_shows_error_and_can_retry(self, hass):
        result = await self.start(hass, password="wrong")
        assert result["errors"] == {"base": "invalid_auth"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "parent@example.com", CONF_PASSWORD: "pw"}
        )
        assert result["step_id"] == "children"

    async def test_network_failure_shows_error(self, hass):
        FakeClient.login_error = SkylightConnectionError("down")
        result = await self.start(hass)
        assert result["errors"] == {"base": "cannot_connect"}

    async def test_no_calendars_aborts(self, hass):
        FakeClient.frames = []
        result = await self.start(hass)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "no_calendars"

    async def test_empty_selection_is_rejected(self, hass):
        result = await self.start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_CHILDREN: []})
        assert result["errors"] == {"base": "no_children"}

    async def test_same_calendar_twice_aborts(self, hass):
        await setup_entry(hass)
        result = await self.start(hass)
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"


@pytest.mark.usefixtures("boston")
class TestReauthFlow:
    async def test_new_password_is_saved_and_reloads(self, hass):
        entry = await setup_entry(hass)
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        assert result["description_placeholders"][CONF_EMAIL] == "parent@example.com"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
        assert result["errors"] == {"base": "invalid_auth"}

        FakeClient.valid_password = "new-pw"
        try:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new-pw"})
            await hass.async_block_till_done()
        finally:
            FakeClient.valid_password = "pw"
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_PASSWORD] == "new-pw"
        assert client().password == "new-pw"
        assert entry.state is ConfigEntryState.LOADED


@pytest.mark.usefixtures("boston")
class TestOptionsFlow:
    async def open(self, hass, entry):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        return result

    async def test_new_deadline_takes_effect_after_reload(self, hass):
        FakeClient.tasks = [routine("1")]
        entry = await setup_entry(hass)
        assert hass.states.get(MORNING).state == "pending"
        result = await self.open(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**OPTIONS, "deadline_weekday_morning": "06:30:00"}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options["deadline_weekday_morning"] == "06:30:00"
        assert hass.states.get(MORNING).state == "overdue"

    async def test_deadline_before_period_start_is_rejected(self, hass):
        entry = await setup_entry(hass)
        result = await self.open(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**OPTIONS, "deadline_weekend_afternoon": "11:00:00"}
        )
        assert result["errors"] == {"deadline_weekend_afternoon": "deadline_before_start"}

    async def test_period_starts_must_be_in_order(self, hass):
        entry = await setup_entry(hass)
        result = await self.open(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**OPTIONS, "start_evening": "11:00:00"}
        )
        assert result["errors"]["start_evening"] == "starts_out_of_order"

    async def test_ignoring_untimed_chores_removes_them(self, hass):
        FakeClient.tasks = [Task("1", "chore", "a", FRIDAY, None, False, "pending", None)]
        entry = await setup_entry(hass)
        assert hass.states.get("sensor.skylink_ann_evening").state == "upcoming"
        result = await self.open(hass, entry)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**OPTIONS, "untimed_period": "ignore"}
        )
        await hass.async_block_till_done()
        assert hass.states.get("sensor.skylink_ann_evening").state == "none"

    async def test_poll_interval_is_stored_as_a_whole_number(self, hass):
        entry = await setup_entry(hass)
        result = await self.open(hass, entry)
        await hass.config_entries.options.async_configure(result["flow_id"], {**OPTIONS, "poll_interval": 300})
        await hass.async_block_till_done()
        assert entry.options["poll_interval"] == 300
        assert isinstance(entry.options["poll_interval"], int)


async def test_every_form_field_has_a_translation(hass):
    """Catch a missing or misnamed label, which the UI would show as a raw key."""
    with open("custom_components/skylink/translations/en.json", encoding="utf-8") as handle:
        strings = json.load(handle)
    labels = strings["options"]["step"]["init"]["data"]
    assert set(labels) == set(DEFAULT_OPTIONS)
    for key in ("morning", "afternoon", "evening", "current"):
        assert set(strings["entity"]["sensor"][key]["state"]) == {"none", "upcoming", "pending", "overdue", "done"}
