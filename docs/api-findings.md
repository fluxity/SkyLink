# Skylight API findings

Observed from a real account on 2026-10-31, in `tests/fixtures/discovery/20261031T104329`. The API is unofficial and can change.

## Gate questions

| Question | Answer |
|---|---|
| How is a Routine's time of day represented? | `routine: true` plus `start_time`: `06:00` morning, `14:00` afternoon, `20:00` evening. `recurrence_set` carries the same hour as `BYHOUR`. |
| Is a Routine in two periods one task or two? | Two. Each period is its own record with its own id. |
| Are all of a day's Routines returned all day? | Yes, as far as observed. At 10:43 the response held that day's later Routines, and yesterday's morning Routines were still returned the next day. |
| Do completed tasks stay in the response? | Yes, with `status: complete` and `completed_on`. |
| Do skipped tasks stay in the response? | Not observed. It doesn't change the logic: a `skipped` status counts as done, and a task that disappears no longer counts as remaining. |
| How is a task for several children represented? | One record per child, with the same `summary`. |
| Does login work? | Yes, with the PKCE flow and no device fingerprint. |

## Behavior the code must handle

- **Old completed tasks:** Undated tasks (`start: null`) come back in every query after completion, including ones finished months earlier. A task with no date only counts today if it is still open or `completed_on` is today.
- **Late chores:** With `include_late=true`, late chores come back with one record per series and `completed_on` set when finished, including on the day they are finished. The display lists open late chores and those finished today. No late Routines appeared in the display or the response on 2026-11-01.
- **Ended series:** `include_late=true` also returns open records from recurring series whose `recurring_until` has passed, some months old. The display hides them, so SkyLink drops them. On 2026-11-01 this rule matched all 8 late records and every displayed count, but that is one day's evidence.
- **`/routines`:** Returns 404. Routines are read from `/chores`, so this endpoint isn't used.
- **Tracked children:** The account has 16 categories, and only 4 have `selected_for_chore_chart: true`. That flag is the default selection during setup.
- **Tokens:** The access token lasts 86400 seconds. Refresh works and rotates the refresh token.
- **Early completion:** A task can be completed before its period starts, so an evening task can already be `complete` in the afternoon.
- **Date range:** `after` and `before` are both inclusive.
- **Ids:** Recurring chores use `<id>-YYYY-MM-DD`. Recurring Routines had plain ids in this capture.
