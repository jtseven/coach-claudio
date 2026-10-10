# Setup — onboarding a new athlete

Read this when `coach.py show` says there is no config, when `coach.py
check` reports gaps, or when the athlete asks to set up or reconfigure the
coach.

The goal of the setup is three files and a working connection to their data:

- `athlete/config.toml` — structured settings every script reads
- `athlete/profile.md` — who they are as an athlete, in prose
- `athlete/plan.md` — the season: phases, targets, the default week

...plus, depending on what they want, a training calendar with the first week
on it, Hevy routines, and the scheduled check-ins on their phone.

## How to run it

- **It's a conversation, not a form.** Ask in small rounds — three or four
  questions at a time, with a sensible default offered for each — and fill
  in what you can from their data instead of asking. In a terminal, the
  AskUserQuestion tool is good for multiple-choice rounds.
- **Their data first, questions second.** Strava already knows their weekly
  volume, longest run, recent races and heart-rate range. Show them what you
  found and ask them to confirm or correct, rather than asking them to
  remember.
- **Write as you go.** Fill `athlete/config.toml` after each round, so a
  setup interrupted halfway resumes from what's on disk. On resuming, read
  the files and pick up at the first step that isn't done.
- **Say where you are.** Start with a one-paragraph overview (about 20–30
  minutes; steps below) and keep a light sense of progress ("step 3 of 7:
  your numbers").
- **Be honest about the goal.** If the timeline or target doesn't fit their
  history, say so plainly with the numbers, and offer an alternative. A coach
  who agrees to an unsafe plan is not doing the job.

## Step 0 — scaffolding

```bash
python3 <skill>/scripts/coach.py init      # athlete/ from templates, state files, .env
python3 <skill>/scripts/doctor.py          # what's installed, what's missing
```

`init` never overwrites. Ignore doctor's failures for integrations not yet
chosen — that's what the next steps are for. Fix anything in the basics
(Python version, the skill symlink) first.

## Step 1 — can you see Strava?

Strava is the one hard requirement. Call the Strava `list_activities` tool
for the last few activities.

- **Works** → go on.
- **No Strava tools** → it needs an MCP server. The recommended one, and the
  one the post-run watcher needs by name:

  ```bash
  claude mcp add --scope user --transport http strava-mcp https://mcp.strava.com/mcp
  ```

  Then in a new `claude` session, `/mcp` → `strava-mcp` → authenticate (on a
  machine without a browser, see docs/deployment.md "OAuth without a
  browser"). Claude Code only picks up new MCP servers at startup, so they
  need to restart `claude` in the coach folder and say "continue the setup".
  Everything done so far is on disk.

## Step 2 — basics

Ask, in one round:

- name, and pronouns (default they/them);
- which language the coach should write in (default English);
- where they live (city) — use it for `home.place`, look up `lat`/`lon` with
  `weather.py --place <city> forecast --at <now>` (the output names the place
  it found; ask if it's ambiguous), and infer the IANA timezone, then
  confirm it;
- the goal: race name, date, distance, and a target time if they have one.
  No race yet? Help them pick something 12–30 weeks out that fits their
  history, or set a non-race goal ("first half marathon", "run 30 km a week
  without getting hurt") with a target date.

Write `[athlete]`, `[home]`, `[goal]` (race, date, distance, target).

## Step 3 — what their data says

Pull about six months of activities (paginate `list_activities`). Then:

```bash
python3 <skill>/scripts/analyze.py week --activities <json>     # weekly running km
```

Work out, and show them as short lines:

- **Volume**: mean weekly running km over the last 8 weeks and the last six
  months, peak week, weeks without running.
- **Long run**: longest run in six months and ever (if visible), how often
  anything over ~15 km happens.
- **Other sport**: hours of cycling, gym, climbing etc. per week — and which
  of it is commuting (`EBikeRide` etc., or rides they tell you are commutes;
  add those types to `activities.commute_types`).
- **Races and hard efforts**: anything tagged as a race, or best efforts. For
  each, `analyze.py vdot --distance <m> --time <s>`. Take the VDOT from the
  most reliable recent race, rounded down if they tend to overcook easy runs.
- **Heart rate**: their configured zones from Strava (`get_athlete_zones`),
  and the highest credible max HR — from the end of a hard race, not a spike.
  A sensor artifact looks like an HR pinned flat for minutes then jumping
  30+ bpm; ignore those. Average HR of a maximal 45–60 minute effort is a
  decent threshold proxy.
- **How easy are the easy runs?** Pull the HR of a few typical short runs
  (`get_activity_performance` or streams). Compare short-run HR with long-run
  HR — runners who race their short runs are the most common pattern there
  is, and the most useful thing to find.

While those six months are in hand, seed the training load — fitness takes
six weeks of history to mean anything, and this is the only time the coach
pulls that much:

```bash
python3 <skill>/scripts/analyze.py load --activities <json> --save
```

Then propose, and let them confirm or correct:

- `hrmax` — measured if possible. If nothing credible exists, say so and use
  their watch's value as a placeholder, flagged for a test (a parkrun's last
  kilometre all-out, or a hill repeat set).
- `threshold_hr`.
- `easy_ceiling` — the top of their easy zone. If their watch zones look
  sensible, the top of Z2; if not (Strava's defaults are often too generous),
  roughly 75–77% of HRmax, or about 18 bpm under threshold. Say how you got it.
- `zones` — exactly as configured on their watch/Strava, so the coach never
  contradicts what they see. If they change their zones because of this
  setup, use the new ones.
- `vdot`.

Write `[physiology]`. Then name **the central problem** — the one thing their
data says this season has to fix — and check it with them. It goes at the
heart of the profile.

## Step 4 — their life and preferences

One or two rounds:

- **Week shape**: which days can they run, and when (morning/evening)? Which
  day suits the long run? Days that are always off-limits?
- **Strength**: do they lift? Where (gym, home)? Do they use Hevy (needs Hevy
  Pro for the API)?
- **Other sports** they want to keep doing, and roughly how often.
- **Injury history** and anything current. Current pain → record it as a
  niggle now (`coachlog.py niggle`).
- **Food**: diet and restrictions; do they track food (if not, the coach
  never talks in grams); is weight a goal at all (default: no — a training
  block is fuelled, not dieted through).
- **How they want to be coached**: how direct, how long messages should be,
  whether they want to be asked about niggles or will report them.
- **Calendars**: which Google calendars are their personal ones (to read for
  travel and events)? Any to never touch?

Write `[schedule]` (check-in times, plan/close-out days, session times),
`[calendar]` personal and never_write. Everything else goes into the profile.

## Step 5 — write the profile and the plan

**`athlete/profile.md`** — fill the template's sections from steps 2–4, with
numbers and where they came from, and the central problem written out with
its evidence. Delete the template comments as you go. Note the baseline date
and when to recheck (the end of the first phase).

**`athlete/plan.md`** — draft the season, show it to them in short lines, and
adjust before writing. Principles:

- **Start from what they actually run**, not what they'd like to: W1 is about
  their recent 8-week average (not their peak), and nothing grows more than
  ~10% a week on the *actual* previous week. Down week every 3rd or 4th week
  at ~70–75% of the week before.
- **`plan_start`** is the coming Monday (or this Monday if it's early in the
  week and they're keen). `plan_weeks` runs to race week inclusive.
- **Phases**, roughly: base (a third of the plan — easy volume only, strides,
  no intervals), aerobic development (one quality session a week appears),
  specific build (race-pace work in long runs, the longest runs, rehearsing
  fuelling and kit), taper (marathon 2–3 weeks, half 1–2, 10K about one).
- **Long run** about half the weekly volume early on; for a marathon, build
  toward two or three runs of 30–34 km in the specific phase if the timeline
  allows; for a half, 18–22 km.
- **Peak volume** follows from their history and time, not from a template:
  a first marathon off 15 km a week is a different plan from a fifth off 50.
- **Not enough time?** If +10% a week can't reach a sensible peak before the
  taper, say so with the numbers and offer the options: a later race, a
  different distance, or a "finish healthy" goal instead of a time.
- **Gym**: two short sessions a week if they lift (lower + core, upper +
  core), fewer in the taper.

Fill in the arc table, phase intents, weekly targets for at least the first
phase, the default week, gym routines, and any booked races. Then:

```bash
python3 <skill>/scripts/coach.py check && python3 <skill>/scripts/coach.py show
```

## Step 6 — integrations

Ask which they want, explaining each in a line. Do the chosen ones now.

**Google Calendar (recommended).** The plan lives there, and the check-ins
read it. The connector can't create calendars, so ask them to create one in
Google Calendar (e.g. "Berlin Marathon training"), then find it with
`list_calendars` and write its name and id to `[calendar]`. If no calendar
tools are available: on claude.ai, Settings → Connectors → Google Calendar,
then restart `claude`. Then write:

- a **phase banner** per phase (all-day, multi-day, transparent);
- a **weekly target** event per week (all-day Monday, transparent, e.g.
  `W3/18 · 24 km · LR 12 km · Base`), at least through the first phase;
- the **first week's sessions**, following "Generating a week" in
  `references/training-plan.md`.

**Hevy.** Needs Hevy Pro and an API key (Hevy app → Settings → Developer).

```bash
claude mcp add --scope user hevy -e HEVY_API_KEY=<key> -- npx -y hevy-mcp
```

Needs Node.js, and a `claude` restart. Then create the standing routines
named in the plan (search exercise templates first; check `search-routines`
so you don't duplicate) and set `integrations.hevy = true`.

**Telegram — chat with the coach and get check-ins on the phone.** Needs an
always-on machine (anything that runs Linux or macOS and stays on: a
Raspberry Pi, a NUC, a home server, a small VPS) and tmux. The steps are in
`docs/deployment.md` "Telegram"; walk them through it, doing what you can
from here:

1. They create a bot with @BotFather and install the plugin themselves —
   `/plugin install telegram@claude-plugins-official`, then
   `/telegram:configure <token>` and `/telegram:access` to pair. Slash
   commands are theirs to run, not yours.
2. Chat id into `.env` (`TELEGRAM_CHAT_ID`); `chmod 600 .env`.
3. `integrations.telegram = true`, then render and install the crontab:

   ```bash
   python3 <skill>/scripts/coach.py crontab
   ```

   Show it and ask before installing — `coach.py crontab | crontab -`
   replaces any crontab they already have; if they have one, merge by hand.
4. Optional typing indicator: merge `coach.py hooks` into
   `~/.claude/settings.json` (merge, never overwrite that file).
5. **Warn them once, clearly**: with the plugin enabled, *any* `claude`
   session started on that machine connects to Telegram and kills the bot's
   connection until the watchdog repairs it (up to 10 minutes). For other
   work on the machine, start claude with
   `claude --settings '{"enabledPlugins":{"telegram@claude-plugins-official":false}}'`.

**Apple Health (needs iPhone + Apple Watch).** The paid Health Auto Export
app posts to a small receiver on the coach machine. `docs/deployment.md`
"Apple Health" has the app settings and the ways to reach the machine from
the phone (home Wi-Fi only, Tailscale, or a reverse proxy). Generate
`HEALTH_API_KEY` (`openssl rand -hex 16`) into `.env`, set
`integrations.apple_health = true`, add the watchdog to cron (it's in the
rendered crontab), and have them press "Export now". `health.py summary`
should then show data.

**Voice notes** and the **weekly charts** are optional extras, also in
`docs/deployment.md`. The charts need `uv` on the machine;
`doctor.py` says whether it's there.

## Step 7 — verify

```bash
python3 <skill>/scripts/doctor.py
<skill>/scripts/check_in.sh morning --print-prompt   # the rendered prompt
```

Fix every FAIL. If they're running scheduled check-ins, suggest they run one
dry from their own terminal — it runs the real coach but sends nothing and
logs nothing:

```bash
<skill>/scripts/check_in.sh morning --no-send
```

Then a real end-to-end test of Telegram is them messaging the bot and getting
an answer. Tool availability and message delivery are different things;
only the second is the feature.

## Step 8 — wrap up

Tell them, briefly:

- what happens next: the first week is on the calendar; the first morning
  brief arrives at <time> (with Telegram); otherwise "message me after your
  next run";
- that everything about them is in `athlete/` — config, profile, plan — and
  they can edit it, or just tell the coach what to change;
- `docs/customizing.md` for tone, prompts and timing;
- backup: `athlete/` is the irreplaceable part. `docs/deployment.md`
  "Backup" makes it its own private git repo, pushed nightly.

Log the setup: `coachlog.py add --mode conversation --note "setup complete:
<goal>, W1 starts <date>, <integrations>"`.

## Changing things later

Do these when asked, and confirm in one line.

- **Language, check-in times, message length, plan/close-out day** — edit
  `athlete/config.toml`. Changed times also need the crontab re-rendered and
  reinstalled (`coach.py crontab`); say so, and offer to do it.
- **New goal race** — steps 2 (goal), 5 (plan) and the calendar part of 6.
  Don't delete past events; end the old phase banners and write new ones.
  Keep the weekly log; it's their history.
- **Zone recalibration** — after a race, or at the end of a phase: steps 3's
  heart-rate and VDOT parts, update config and profile, and tell them what to
  re-enter on their watch/Strava (the Strava connector is read-only).
- **Turning an integration on or off** — step 6 for that integration, and
  the config flag. Off means the coach stops using it entirely.
- **Preferences** ("stop asking about X") — the profile's Preferences section,
  dated.
