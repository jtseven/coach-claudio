# Customizing the coach

Two rules keep customizing painless:

1. **Everything about you lives in `athlete/`.** Edit freely; `git pull` never
   touches it.
2. **The easiest way to change something is to tell the coach.** "Write to me
   in German", "move the morning brief to 6:45", "stop asking about my knee"
   — it updates the right file and says so.

## Where things live

| You want to change… | Edit | Notes |
|---|---|---|
| Language of every message | `athlete/config.toml` → `athlete.language` | Any language; a message *to* the coach in another language is answered in that language |
| Name, pronouns | `athlete.name`, `athlete.pronouns` | |
| Check-in times | `schedule.morning`, `schedule.evening` | Then re-render the crontab: `coach.py crontab` |
| Which day plans / closes the week | `schedule.plan_day`, `schedule.closeout_day` | No crontab change needed |
| Default session times | `schedule.weekday_run_time`, `schedule.long_run_time` | |
| Message length | `[messages]` word budgets | |
| Goal race, date, plan length | `[goal]` + `athlete/plan.md` | Or ask the coach: "new goal race" re-runs that part of the setup |
| Heart rate zones, easy ceiling | `[physiology]` | Keep them identical to your watch |
| Which sports are commutes | `activities.commute_types` | Strava sport type names |
| Integrations | `[integrations]` | Off means the coach never uses it |
| How you like to be coached | `athlete/profile.md` → "Preferences" | Plain language, dated |
| Your season, default week, gym routines | `athlete/plan.md` | |
| What a check-in does | `athlete/prompts/<mode>.md` | See below |

After editing the config, check it:

```bash
python3 skill/scripts/coach.py check && python3 skill/scripts/coach.py show
```

## Overriding a check-in prompt

The scheduled check-ins are rendered from `skill/prompts/morning.md`,
`evening.md` and `postrun.md`. To change one for yourself, copy it to
`athlete/prompts/` and edit the copy — it takes precedence:

```bash
mkdir -p athlete/prompts && cp skill/prompts/morning.md athlete/prompts/
skill/scripts/check_in.sh morning --print-prompt     # see the result
```

The template language is small:

- `{name}`, `{language}`, `{scripts}`, `{training_calendar}`,
  `{personal_calendars}`, `{plan_day}`, `{closeout_day}`, `{easy_ceiling}`,
  `{week}`, `{today}`, the `[messages]` budgets (`{morning_words}`, …), and
  for post-run `{activity_id}` and `{sport_type}`.
- `{{#if flag}} … {{/if}}` and `{{#unless flag}} … {{/unless}}`, nestable.
  Flags: every integration (`hevy`, `apple_health`, `telegram`, …), plus
  `plan_day`, `closeout_day` (true on those weekdays) and `down_week`.

After `git pull`, compare your override with the new upstream prompt.

## Changing coaching behaviour

The coaching method itself — tone, the analysis playbook, nutrition, how a
week is generated — is in `skill/SKILL.md` and `skill/references/`. Those are
shared engine files: change them if you want to change the coach for everyone
(and send a pull request), but for anything personal, prefer the profile's
Preferences section. The coach reads that on every real coaching task, and it
survives updates.

## Testing a change

```bash
skill/scripts/check_in.sh evening --print-prompt   # rendered prompt only
skill/scripts/check_in.sh evening --no-send        # real run, sends and logs nothing
tests/run.sh                                       # the script smoke tests
```

If Telegram is running on the same machine, start any interactive `claude`
session with
`--settings '{"enabledPlugins":{"telegram@claude-plugins-official":false}}'`
so it doesn't knock the bot offline (docs/deployment.md, "Two processes, one
bot token").
