# SkyLink

SkyLink is a Home Assistant integration that shows whether your children have finished their Skylight morning, afternoon and evening tasks. Use its sensors to send phone notifications or change a light's color.

## Before you install

- **Skylight has no official API.** SkyLink uses a reverse-engineered one that can change or break without notice.
- **It needs a Skylight password.** Accounts that sign in only through Google or Apple won't work.
- **Your password is stored unencrypted** in Home Assistant's `.storage` folder, as with most integrations. The access token has full account scope, but after login SkyLink only reads data.

Tested on Home Assistant 2026.10.4.

## Install

With [HACS](https://hacs.xyz), open the menu, choose **Custom repositories**, and add `https://github.com/fluxity/SkyLink` as type **Integration**. Install SkyLink, then restart Home Assistant. New releases then appear as updates in Home Assistant.

To install by hand, copy `custom_components/skylink` into your `config/custom_components/` folder and restart.

Then go to **Settings > Devices & services > Add integration > SkyLink**. Sign in, choose a calendar if you have several, and pick who to track. Profiles on your Skylight chore chart are preselected. To change who is tracked later, remove and re-add the integration, which also resets its options.

## Set your deadlines

Open the integration's **Configure** screen. Deadlines default to the end of each period, so nothing shows as overdue until late unless you change them.

| Option | Default | Notes |
|---|---|---|
| Period starts | 00:00, 12:00, 18:00 | Routines stay in the period they were created for in Skylight, even if you change these. |
| Weekday and weekend deadlines | 12:00, 18:00, 23:59 | Set separately for each period. |
| Period for chores with no time | Evening | Can also be set to Ignore. |
| Poll interval | 120 seconds | 60 to 600 seconds. |

## Sensors

- `sensor.skylink_<child>_morning`, `_afternoon` and `_evening`
- `sensor.skylink_household_morning`, `_afternoon` and `_evening`
- `sensor.skylink_household_current`, for whichever period it is now

| State | Meaning |
|---|---|
| `none` | No tasks in this period |
| `upcoming` | The period hasn't started and tasks remain |
| `pending` | The period has started, the deadline hasn't passed, and tasks remain |
| `overdue` | The deadline passed and tasks remain, until they are done or the day ends |
| `done` | All tasks are complete or skipped |
| `unavailable` | Skylight has been unreachable for 15 minutes or more |

Late chores count toward today, in their usual period, until they are done, as they do on the Skylight display. Untimed ones go to the period you chose for chores with no time, so one forgotten chore keeps that period from reaching `done` and shows as `overdue` each day after the deadline until it is finished or removed. Missed Routines don't carry over to the next day.

Attributes are `total`, `completed`, `remaining` and `deadline`. The current sensor also has `period`. A household sensor uses the first of these that applies to any child: `overdue`, `pending`, `upcoming`, `done`, `none`.

### When Skylight is unreachable

Statuses freeze while polls are failing, then become `unavailable` after 15 minutes, so an outage doesn't read as missed tasks. A deadline that passes during an outage won't send a notification. If the deadline passes just before the first failed poll, the sensor can still turn `overdue` based on the last good data.

## Automations

HACS doesn't install blueprints. In Home Assistant, go to **Settings > Automations & scenes > Blueprints > Import Blueprint** and paste each link:

- `https://github.com/fluxity/SkyLink/blob/main/blueprints/automation/skylink/notify_overdue.yaml`
- `https://github.com/fluxity/SkyLink/blob/main/blueprints/automation/skylink/status_light.yaml`

Those links need the repository to be public. Otherwise, copy `blueprints/automation/skylink` into your `config/blueprints/automation/` folder.

- **SkyLink task notification:** Notifies one or more phones when a sensor becomes overdue, listing the remaining tasks. Reminders and a "done" notification are optional. Enter actions such as `notify.mobile_app_my_phone`. A phone that can't be reached doesn't affect the others, but a misspelled action stops the notifications to any phones listed after it. Create one automation per sensor.
- **SkyLink status light:** Sets a light's color for each status, and reapplies it when Home Assistant starts. The defaults are red for overdue, amber for pending, blue for upcoming and green for done, and the light turns off when there are no tasks. If Skylight is unreachable, the light keeps its last color.

## Troubleshooting

- **A sign-in prompt appears in Home Assistant:** Skylight rejected the saved login. Enter your current password.
- **Sensors are `unavailable`:** Skylight can't be reached, or its API changed. Check the Home Assistant log for SkyLink errors.
- **Sharing a bug report:** Download diagnostics from the integration's menu. They leave out credentials, names and task titles.

## Development

Tests need Python 3.14:

```bash
python3.14 -m venv .venv-ha
```

```bash
.venv-ha/bin/pip install pytest-homeassistant-custom-component
```

```bash
.venv-ha/bin/python -m pytest
```

`tools/discover.py` captures redacted API responses for checking changes in Skylight's API. It needs only the Python standard library. [docs/api-findings.md](docs/api-findings.md) records what those captures showed.
