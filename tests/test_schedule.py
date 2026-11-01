"""Tests for task parsing and status logic, including the real redacted capture."""

import json
from datetime import date, datetime, time
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from custom_components.skylink.models import Task, parse_categories, parse_tasks
from custom_components.skylink.schedule import (
    Period,
    ScheduleConfig,
    Status,
    build_snapshot,
    counts_today,
    current_period,
    period_for_task,
)

BOSTON = ZoneInfo("America/New_York")
CAPTURE = Path(__file__).parent / "fixtures" / "discovery" / "20261031T104329"
SATURDAY = date(2026, 10, 31)
FRIDAY = date(2026, 10, 30)
KIDS = {"a": "Ann", "b": "Ben"}


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BOSTON)


def task(id_="1", child="a", start=FRIDAY, start_time=None, routine=False, status="pending", completed_on=None):
    return Task(id_, f"task {id_}", child, start, start_time, routine, status, completed_on)


def routine(id_, child="a", hour=6, status="pending"):
    return task(id_, child, start_time=time(hour), routine=True, status=status)


CONFIG = ScheduleConfig(
    weekday_deadlines={Period.MORNING: time(7, 45), Period.AFTERNOON: time(17, 0), Period.EVENING: time(20, 30)},
    weekend_deadlines={Period.MORNING: time(10, 0), Period.AFTERNOON: time(17, 0), Period.EVENING: time(21, 0)},
)


class PeriodAssignmentTest(unittest.TestCase):
    def test_skylight_routine_times_map_to_their_periods(self):
        self.assertEqual(period_for_task(routine("1", hour=6), CONFIG), Period.MORNING)
        self.assertEqual(period_for_task(routine("1", hour=14), CONFIG), Period.AFTERNOON)
        self.assertEqual(period_for_task(routine("1", hour=20), CONFIG), Period.EVENING)

    def test_routine_period_ignores_changed_period_starts(self):
        config = ScheduleConfig(starts={Period.MORNING: time(0), Period.AFTERNOON: time(15), Period.EVENING: time(18)})
        self.assertEqual(period_for_task(routine("1", hour=14), config), Period.AFTERNOON)

    def test_timed_chore_follows_configured_starts(self):
        config = ScheduleConfig(starts={Period.MORNING: time(0), Period.AFTERNOON: time(15), Period.EVENING: time(18)})
        self.assertEqual(period_for_task(task(start_time=time(14)), config), Period.MORNING)

    def test_untimed_chore_uses_the_configured_period(self):
        self.assertEqual(period_for_task(task(), CONFIG), Period.EVENING)
        self.assertEqual(period_for_task(task(), ScheduleConfig(untimed_period=Period.AFTERNOON)), Period.AFTERNOON)
        self.assertIsNone(period_for_task(task(), ScheduleConfig(untimed_period=None)))

    def test_current_period_boundaries(self):
        self.assertEqual(current_period(at(FRIDAY, 11, 59), CONFIG), Period.MORNING)
        self.assertEqual(current_period(at(FRIDAY, 12), CONFIG), Period.AFTERNOON)
        self.assertEqual(current_period(at(FRIDAY, 18), CONFIG), Period.EVENING)


class CountsTodayTest(unittest.TestCase):
    def test_dated_task_counts_on_its_date_but_not_before(self):
        self.assertTrue(counts_today(task(start=FRIDAY), FRIDAY))
        self.assertFalse(counts_today(task(start=SATURDAY), FRIDAY))

    def test_open_late_chore_counts(self):
        self.assertTrue(counts_today(task(start=FRIDAY), SATURDAY))

    def test_late_chore_finished_today_counts_but_not_one_finished_earlier(self):
        self.assertTrue(counts_today(task(start=FRIDAY, status="complete", completed_on=SATURDAY), SATURDAY))
        self.assertFalse(counts_today(task(start=FRIDAY, status="complete", completed_on=FRIDAY), SATURDAY))

    def test_missed_routine_does_not_carry_over(self):
        self.assertFalse(counts_today(routine("1"), SATURDAY))

    def test_late_record_from_an_ended_series_is_dropped(self):
        ended = Task("1", "old", "a", date(2026, 8, 30), None, False, "pending", None, date(2026, 9, 5))
        self.assertFalse(counts_today(ended, SATURDAY))

    def test_late_record_from_a_series_ending_later_counts(self):
        running = Task("1", "chore", "a", FRIDAY, None, False, "pending", None, date(2026, 12, 1))
        self.assertTrue(counts_today(running, SATURDAY))

    def test_undated_task_completed_long_ago_is_dropped(self):
        old = task(start=None, status="complete", completed_on=date(2026, 5, 12))
        self.assertFalse(counts_today(old, FRIDAY))

    def test_undated_task_counts_while_open_and_on_its_completion_day(self):
        self.assertTrue(counts_today(task(start=None), FRIDAY))
        self.assertTrue(counts_today(task(start=None, status="complete", completed_on=FRIDAY), FRIDAY))


class StatusTest(unittest.TestCase):
    def status(self, tasks, now, period=Period.MORNING, child="a"):
        return build_snapshot(tasks, KIDS, now, CONFIG).children[child][period].status

    def test_none_when_period_has_no_tasks(self):
        self.assertEqual(self.status([], at(FRIDAY, 6)), Status.NONE)

    def test_upcoming_before_the_period_starts(self):
        self.assertEqual(self.status([routine("1", hour=14)], at(FRIDAY, 11, 59), Period.AFTERNOON), Status.UPCOMING)

    def test_pending_until_the_deadline_then_overdue(self):
        tasks = [routine("1")]
        self.assertEqual(self.status(tasks, at(FRIDAY, 7, 44)), Status.PENDING)
        self.assertEqual(self.status(tasks, at(FRIDAY, 7, 45)), Status.OVERDUE)
        self.assertEqual(self.status(tasks, at(FRIDAY, 23, 59)), Status.OVERDUE)

    def test_weekends_use_their_own_deadlines(self):
        tasks = [task(start=SATURDAY, start_time=time(6), routine=True)]
        self.assertEqual(self.status(tasks, at(SATURDAY, 7, 45)), Status.PENDING)
        self.assertEqual(self.status(tasks, at(SATURDAY, 10)), Status.OVERDUE)

    def test_done_and_skipped_both_finish_a_period(self):
        tasks = [routine("1", status="complete"), routine("2", status="skipped")]
        self.assertEqual(self.status(tasks, at(FRIDAY, 9)), Status.DONE)

    def test_task_finished_before_its_period_is_done(self):
        self.assertEqual(self.status([routine("1", hour=20, status="complete")], at(FRIDAY, 14), Period.EVENING), Status.DONE)

    def test_unknown_status_counts_as_not_done(self):
        self.assertEqual(self.status([routine("1", status="mystery")], at(FRIDAY, 7)), Status.PENDING)

    def test_midnight_rollover_clears_yesterdays_overdue(self):
        tasks = [routine("1")]
        self.assertEqual(self.status(tasks, at(FRIDAY, 23, 59)), Status.OVERDUE)
        self.assertEqual(self.status(tasks, at(SATURDAY, 0, 0)), Status.NONE)

    def test_open_late_chore_goes_to_the_untimed_period_and_blocks_done(self):
        tasks = [task("1", start=FRIDAY), task("2", start=SATURDAY, status="complete", completed_on=SATURDAY)]
        summary = build_snapshot(tasks, KIDS, at(SATURDAY, 22), CONFIG).children["a"][Period.EVENING]
        self.assertEqual(summary.status, Status.OVERDUE)
        self.assertEqual((summary.total, summary.completed), (2, 1))
        self.assertEqual(summary.remaining, ("task 1",))

    def test_dads_list_from_the_live_display_gives_four_of_eight(self):
        today = date(2026, 11, 1)
        oct31, oct30 = date(2026, 10, 31), date(2026, 10, 30)
        tasks = [
            task("cut", start=oct31),
            task("tub", start=today),
            task("bag", start=today),
            task("trash", start=today),
            task("charge", start=oct30, status="complete", completed_on=today),
            task("laundry", start=oct30, status="complete", completed_on=today),
            task("kids", start=oct31, status="complete", completed_on=today),
            task("fold", start=today, status="complete", completed_on=today),
        ]
        summary = build_snapshot(tasks, KIDS, at(today, 14), CONFIG).children["a"][Period.EVENING]
        self.assertEqual((summary.completed, summary.total), (4, 8))

    def test_duplicate_ids_count_once(self):
        snapshot = build_snapshot([routine("1"), routine("1")], KIDS, at(FRIDAY, 7), CONFIG)
        self.assertEqual(snapshot.children["a"][Period.MORNING].total, 1)

    def test_untracked_child_is_ignored(self):
        snapshot = build_snapshot([routine("1", child="other")], KIDS, at(FRIDAY, 7), CONFIG)
        self.assertEqual(snapshot.household[Period.MORNING].status, Status.NONE)

    def test_deadlines_on_dst_change_days(self):
        # 02:30 doesn't exist on 2026-03-08, and 01:30 happens twice on 2026-11-01.
        config = ScheduleConfig(weekend_deadlines={Period.MORNING: time(2, 30), Period.AFTERNOON: time(17), Period.EVENING: time(21)})
        spring = date(2026, 3, 8)
        tasks = [task(start=spring, start_time=time(6), routine=True)]
        self.assertEqual(build_snapshot(tasks, KIDS, at(spring, 1, 59), config).children["a"][Period.MORNING].status, Status.PENDING)
        self.assertEqual(build_snapshot(tasks, KIDS, at(spring, 3, 0), config).children["a"][Period.MORNING].status, Status.OVERDUE)

        config = ScheduleConfig(weekend_deadlines={Period.MORNING: time(1, 30), Period.AFTERNOON: time(17), Period.EVENING: time(21)})
        autumn = date(2026, 11, 1)
        tasks = [task(start=autumn, start_time=time(6), routine=True)]
        self.assertEqual(build_snapshot(tasks, KIDS, at(autumn, 1, 0), config).children["a"][Period.MORNING].status, Status.PENDING)
        self.assertEqual(build_snapshot(tasks, KIDS, at(autumn, 2, 0), config).children["a"][Period.MORNING].status, Status.OVERDUE)


class HouseholdTest(unittest.TestCase):
    def household(self, tasks, now, period=Period.MORNING):
        return build_snapshot(tasks, KIDS, now, CONFIG).household[period]

    def test_any_overdue_child_makes_the_household_overdue(self):
        tasks = [routine("1", "a", status="complete"), routine("2", "b")]
        self.assertEqual(self.household(tasks, at(FRIDAY, 8)).status, Status.OVERDUE)

    def test_pending_beats_done(self):
        tasks = [routine("1", "a", status="complete"), routine("2", "b")]
        self.assertEqual(self.household(tasks, at(FRIDAY, 7)).status, Status.PENDING)

    def test_done_when_one_child_is_done_and_the_other_has_nothing(self):
        self.assertEqual(self.household([routine("1", "a", status="complete")], at(FRIDAY, 7)).status, Status.DONE)

    def test_totals_and_remaining_name_the_child(self):
        summary = self.household([routine("1", "a", status="complete"), routine("2", "b")], at(FRIDAY, 7))
        self.assertEqual((summary.total, summary.completed), (2, 1))
        self.assertEqual(summary.remaining, ("Ben: task 2",))

    def test_current_follows_the_clock(self):
        tasks = [routine("1", "a"), routine("2", "a", hour=14, status="complete")]
        self.assertEqual(build_snapshot(tasks, KIDS, at(FRIDAY, 7), CONFIG).current.status, Status.PENDING)
        snapshot = build_snapshot(tasks, KIDS, at(FRIDAY, 13), CONFIG)
        self.assertEqual(snapshot.current_period, Period.AFTERNOON)
        self.assertEqual(snapshot.current.status, Status.DONE)


class RealCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tasks = parse_tasks(json.loads((CAPTURE / "chores_today.json").read_text())["body"])
        categories = parse_categories(json.loads((CAPTURE / "categories.json").read_text())["body"])
        self.children = {c.id: c.label for c in categories if c.selected_for_chore_chart}

    def test_capture_parses_with_four_chart_profiles(self):
        self.assertEqual(len(self.tasks), 19)
        self.assertEqual(len(self.children), 4)

    def test_old_completed_tasks_are_excluded_and_periods_add_up(self):
        snapshot = build_snapshot(self.tasks, self.children, at(SATURDAY, 10, 43), ScheduleConfig())
        totals = {period: snapshot.household[period].total for period in Period}
        self.assertEqual(totals, {Period.MORNING: 1, Period.AFTERNOON: 4, Period.EVENING: 11})
        self.assertEqual(snapshot.household[Period.MORNING].status, Status.PENDING)
        self.assertEqual(snapshot.household[Period.AFTERNOON].status, Status.UPCOMING)
        self.assertEqual(snapshot.household[Period.EVENING].status, Status.UPCOMING)

    def test_child_with_only_old_completed_tasks_has_nothing_today(self):
        snapshot = build_snapshot(self.tasks, self.children, at(SATURDAY, 10, 43), ScheduleConfig())
        old_tasks_child = next(t.category_id for t in self.tasks if t.start is None)
        for period in Period:
            self.assertEqual(snapshot.children[old_tasks_child][period].status, Status.NONE)


class LiveDisplayMatchTest(unittest.TestCase):
    """The 2026-11-01 capture, checked against what the Skylight display showed that day."""

    def setUp(self):
        capture = CAPTURE.parent / "20261101T143445"
        self.tasks = parse_tasks(json.loads((capture / "chores_with_late.json").read_text())["body"])
        categories = parse_categories(json.loads((capture / "categories.json").read_text())["body"])
        self.children = {c.id: c.label for c in categories if c.selected_for_chore_chart}
        now = datetime(2026, 11, 1, 14, 34, tzinfo=BOSTON)
        self.snapshot = build_snapshot(self.tasks, self.children, now, ScheduleConfig())

    def done_of_total(self, child_id):
        summaries = self.snapshot.children[child_id].values()
        return sum(s.completed for s in summaries), sum(s.total for s in summaries)

    def test_each_profile_matches_the_display(self):
        self.assertEqual(self.done_of_total("id_4458a7c48c"), (0, 4))
        self.assertEqual(self.done_of_total("id_1dfca09196"), (0, 5))
        self.assertEqual(self.done_of_total("id_13ae0cb5f9"), (4, 8))
        self.assertEqual(self.done_of_total("id_7ed348c4d4"), (0, 0))

    def test_ended_series_records_were_really_in_the_response(self):
        ended = [t for t in self.tasks if t.recurring_until and t.recurring_until < date(2026, 11, 1)]
        self.assertEqual(len(ended), 4)


if __name__ == "__main__":
    unittest.main()
