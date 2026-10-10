# Training plan — mechanics: weekly generation, close-out, calendar, Hevy

The athlete's own season — phases, weekly targets, default week, standing gym
routines, booked races — is in `athlete/plan.md`. This file is how to run
any plan: the weekly cycle, the calendar events, the gym routines. Read both
before generating or changing a week.

Phase intent in brief (the athlete's plan says how it applies to them):

- **Base** — one job: raise the volume their legs tolerate. Almost all easy,
  strides allowed, **no tempo or interval work**. It feels unnecessarily
  gentle; that's the point.
- **Aerobic development** — one quality session a week at most (steady/tempo
  blocks, later threshold intervals). Protect the long run and that one
  session; everything else is negotiable. Treadmill and indoor bike are
  legitimate winter substitutes, not failures.
- **Specific build** — race-pace work is the priority session, the longest
  long runs, and rehearsing everything: breakfast, gels, kit, pacing. Nothing
  new in the last month. If the race is on a different weekday than their
  usual long run, move long runs to race weekday in this phase.
- **Taper** — volume drops, intensity stays. They will feel sluggish and
  doubtful; that is normal and worth saying out loud when it happens.

## Calendar mechanics

The **training calendar** is `calendar.training` in the config (id in
`calendar.training_id`). You own it. The **personal calendars**
(`calendar.personal`) are read-only in practice — read them before
scheduling. **Never write to** anything in `calendar.never_write`.

### Event types

Weekly target — all-day, Monday, transparent (`AVAILABILITY_FREE`), colour 3:
```
W3/37 · 24 km · LR 12 km · Base
```
Description carries the target and any note about that week's constraints.

Phase banner — multi-day all-day, transparent, colour 1.

Session — timed. Weekday runs at `schedule.weekday_run_time`, long runs at
`schedule.long_run_time`, unless the athlete's calendar or plan says
otherwise.

### Colour codes

```
9  long run      10 easy run     11 quality/intervals
5  gym            7 bike / other  8 rest
6  milestone/race 3 week target   1 phase banner
```

### Two mechanical gotchas

- **Do not HTML-escape the summary field.** Passing `&amp;` stores the literal
  string. Use a plain `&`, or just write "and".
- **All-day event end dates are exclusive.** A single all-day event on Monday
  10 August is `start 2026-08-10`, `end 2026-08-11`.

### Without Google Calendar

If `google_calendar` is off, the week lives in the message: generate it the
same way, send it as short lines, and keep the plan's targets in
`athlete/plan.md` as the reference. "What's tomorrow" is answered from the
last planning message in the check-in log plus the plan.

## Gym sessions — Hevy

With Hevy on, every gym session on the calendar must have a matching concrete
routine in Hevy. A colour-5 calendar block with no plan behind it is not a
gym session, it's a placeholder — always pair the two. With Hevy off, the
session's calendar description carries the exercises, sets and reps instead.

**What runner-support strength looks like** (unless the plan says otherwise):
the race is the primary training stimulus and gym exists to protect it, so
keep sessions short and moderate:

- **Lower + core**: a squat pattern, a single-leg movement (Bulgarian split
  squat or step-up — these do more for injury-proofing a runner than
  bilateral lifts), a hip hinge (RDL or similar), calf raises, and one core
  move (dead bug, side plank, or similar anti-rotation work).
- **Upper + core**: a push, a pull, and core. Upper body strength isn't the
  limiter for a runner; this session exists so it isn't lost over a season.
- **Sets/reps**: 2–3 sets, 8–12 reps for main lifts, 12–20 for core and
  calves. Loads leave reps in reserve — soreness that compromises tomorrow's
  run is a worse outcome than a slightly undertrained gym session.

**Workflow:**

1. The standing routines are named in `athlete/plan.md` — find them with
   `search-routines`, don't create new weekly copies. `create-routine` is
   only for a genuinely new session type, and check `search-routines` first;
   creates are additive with no dedupe on the Hevy side.
2. When exercises change, `update-routine` on the standing routine. Use
   `search-exercise-templates` (or the cached `hevy://exercise-templates`
   resource) for real template IDs — never invent an exercise name or ID.
3. Note in the calendar event's description which routine applies, e.g.
   "Lower + core - see Hevy: Gym A", so a glance at the calendar alone shows
   there's a real plan, not just a block.

**Progression — update the routine, don't let it go stale.** During weekly
planning (and any time they ask about the next gym session), pull their last
logged workout for that routine (`get-workouts`, or `get-exercise-history`
for a specific lift) and nudge the standing routine's targets:

- All target reps completed and it looked comfortable → next smallest
  increment (machines usually +2.5–5 kg), or +1–2 reps if the load can't
  step smaller.
- Missed reps → hold everything as it was. No drama; holding is normal.
- **Down weeks** → keep the loads, drop a set. Strength is kept by
  intensity, and the down week exists to shed fatigue, not fitness.
- Never progress load and volume in the same week, and never progress into
  an open niggle — check `coachlog.py niggles` first.

Update the routine via `update-routine` so the numbers in the app at the rack
are the current prescription, then tell them what moved ("leg press 77 →
80") — visible progression is half the reason to lift.

**Sets and notes must agree.** At the rack the athlete follows the weight in
the sets, not the note — a routine whose note says "curl 27.5" while the sets
still say 25 gets noticed mid-session, and costs trust. So every change to a
weight, rep or set count goes into the **sets themselves** (`weight_kg`,
`reps`, number of set entries); the note only explains why. Never write a new
number into a note without changing the sets in the same `update-routine`.
Afterwards, `get-routine` and check each exercise: every kg/reps figure in a
note matches the sets. Same for the calendar event description — it repeats
the sets, not a different plan.

**What Hevy is not used for here**: don't create *workouts* (Hevy's record of
a completed session) when planning — `create-workout` is for logging what
actually happened, and inventing completion data for a session that hasn't
happened is exactly what the Hevy MCP's own instructions warn against.
Planning always means routines. Only `create-workout` if they tell you
afterward what they actually did and ask you to record it.

## The weekly cycle: plan, then close out

Two different jobs on two different evenings, set in the config:
`schedule.plan_day` (default Friday) and `schedule.closeout_day` (default
Sunday). The evening check-in does both on the right day.

- **Plan day — generate next week.** Write the events so they have a couple
  of days to look at them.
- **Close-out day — close out the week that just ended.** Log it, and
  reconcile next week's events against how the weekend actually went.

The order is deliberate and the reason is the +10% rule. Planning happens
before the current week has finished — two sessions, usually including the
long run, are still outstanding. So the volume decision rests partly on a
projection, and the close-out is where that projection meets reality.

## Closing out the week

Pull the week's activities once and log the week:

```bash
python3 <skill>/scripts/analyze.py weeklog --append \
  --activities - --week 1 --week-of 2026-08-10 --phase Base \
  --target-km 18 --target-lr 9 --easy-above 4,12,2 --easy-hr 147,150,145 \
  --note "wedding Sat, long run moved to Sun"
```

`--activities` takes the `list_activities` JSON on stdin (`-`), a file path, or
inline. `--append` writes to the weekly log and refuses a week that's already
there — the log is append-only and a duplicate would quietly skew every trend
afterwards. `--week` is the plan week (`coach.py week --date <monday>`).

**The easy-run figures you supply yourself** — `list_activities` carries no
heart rate at all:

- `--easy-above` is the rating that counts: for each easy run, the percent of
  moving time above the easy ceiling, from `analyze.py ceiling` on its HR
  stream (`get_activity_streams`). A run is held at 20% or less. This is
  judged by time because averages lie — an average under the ceiling with
  half the run above it is not an easy run.
- `--easy-hr` is the averages, recorded for context. Alone it falls back to
  rating by average and the entry says `"easy_rated_by": "avg_hr"`.

Leave both out rather than guessing; a missing value records honestly as
"not rated". The `--note` is capped at 300 characters: one clause on what
shaped the week. The numbers are in the entry already; the next reader needs
the reason, not the story.

The entry also splits non-running time — `other_aerobic_h` (rides, hikes,
swims, skiing), `strength_h` / `strength_sessions` (gym), `climb_h` and
`misc_h` — which feeds the block review and the chart. Nothing to supply.

Then **reconcile next week**, which is already on the calendar:

- Long run happened as planned → leave next week alone.
- Long run missed, or the week finished materially short of target → the plan
  was built on a projection that didn't land. Recompute the +10% ceiling off
  the real number and scale next week down. Say you've done it and why;
  they'd rather know than find a quietly changed calendar.
- Week came in *above* projection → do not scale up to match. The ceiling is
  a limit, not a quota.

Most weeks this is a no-op, and a no-op is worth one clause, not a paragraph.

## The block review (close-out of a down week)

The plan moves in blocks that end in a down week (`goal.down_week_every`).
When the week being closed out is one of those (`coach.py week` says "down
week"), the message is not the usual close-out line; it's the block review.
It comes once a block, so it can be longer (`messages.review_words`), but it
still has to be read on a phone.

Gather, after the week is logged:

```bash
python3 <skill>/scripts/analyze.py block      # this block vs the last
python3 <skill>/scripts/health.py block       # with Apple Health: RHR, HRV, weight, VO2max, sleep
```

Plus, with Hevy: `get-exercise-history` for the main lift of each routine.
First and last working weight in the block is enough.

Then write it in this order, short lines, no headers:

1. **One sentence on what the block was** — its story, not its numbers
   ("A cold, a trip, and still the biggest week since the start").
2. **Running** — km per week and longest run against the previous block,
   weeks on target. The trend matters more than any single week.
3. **The rest of the athlete** — gym sessions done and what moved ("leg
   press 77 → 96"), other sports by hours. Climbing, rides and hikes get
   noticed here.
4. **Body** — only the markers that genuinely moved. Medians with few
   readings are flagged by the script — don't lean on those.
5. **One focus for the next block**, concrete: "a long run every week, even a
   short one" beats "stay consistent". Pick the thing the numbers point at.

Easy-run discipline gets a clause inside the running line, not its own
paragraph, unless it slipped badly. Don't restate the plan; they can see the
calendar.

Write the block review **instead of** the ordinary close-out message, not on
top of it — the reconciliation still happens, and if it changed next week,
that's one line at the end.

## Generating a week

The plan-day evening check-in does this itself — it has full calendar
read/write, same as a conversation. Generate the week, write the events,
then tell them the shape in the message. If they ask about next week in
conversation earlier, the same procedure applies.

**0. Account for the rest of this week.** The weekend hasn't happened yet.
Take the running already logged this week, add what is still on the calendar
for the remaining days, and use that projected total as "last week's actual"
for step 1. Say in your message that it's provisional if the weekend carries
a long run — which it usually does.

**1. Read the trend, not just last week.**

```bash
python3 <skill>/scripts/analyze.py trend --last 8
```

- Consistently under target → **hold volume flat**, don't progress. Repeating
  a week they can actually complete beats prescribing one they can't.
- Hitting target → progress as the plan says.
- **Never more than ~10% above the previous week's actual running.** Not the
  planned figure — the actual one. `trend` prints that ceiling directly. The
  injury risk is doing too much too soon, and a plan that ratchets up on
  paper while sessions are missed is how people get hurt.

The ceiling-adherence column is the other half: "you've held the ceiling on
9 of the last 11 easy runs" is worth saying out loud when it's true. Say it
when it improves, and raise it plainly when it slips two weeks running.

**2. Read the target.** "Next week" means the week starting the coming
Monday. Its weekly target event (or `athlete/plan.md`) gives volume and
long-run distance.

Check first whether that week already has session events — they sometimes
ask early in conversation, and the scheduled run would otherwise write a
second set on top. If the sessions are already there, this run is a review,
not a generation: update what their calendar or the trend has changed since,
and say so briefly instead of announcing a week they've already seen.

**3. Read their actual life.** Pull the whole of next week from the personal
calendars. Travel, weddings, work events, all-day commitments. These are
fixed; the training moves around them.

**Travel** deserves its own look, because it changes more than the time
slot. Flights, hotel stays, multi-day events with a location, a city in an
event title — any day they're away from home. For those days:

- weather comes from where they'll be: `weather.py --place <city> ...` (times
  are local there);
- the session description says where to run if you know a sensible option
  (a park loop, a riverside path, the hotel treadmill), or just "flat loop
  from the hotel" — don't invent routes;
- a gym session becomes a hotel-gym or bodyweight variant. With Hevy, check
  `search-routines` for an existing travel routine before creating one, and
  say in the event which routine applies;
- travel days themselves (early flight, long drive) are rest or a short easy
  run at most. A travel day is not where the long run goes.

**4. Check open niggles.** `coachlog.py niggles`. An open severity 2 shapes
the week (fewer downhills, no strides, gym exercise swapped); a severity 3
means the week is built around rehab and a physio visit, not training.
Mention what you adjusted and why.

**5. Check the weather.** Only worth acting on for the next ~3 days — past
that the forecast isn't reliable enough to move a session for, so set those
times normally and revisit midweek.

```bash
python3 <skill>/scripts/weather.py window --date 2026-08-11 --between 06:00-21:00
```

If a cooler slot is materially better *and* their calendar allows it, move
the session and say why. Do not move anything for a couple of degrees, and do
not drop sessions for weather — thunderstorms and ice are the only conditions
that justify that.

**6. Lay out the week** from the default week in `athlete/plan.md`, adapted
to steps 3–5.

**7. Write the events.** Every running session description states the
target pace **and** the HR ceiling for easy work, plus one line on why the
session exists — athletes engage far better with a session they understand
the purpose of. For every gym session, also create or update the matching
Hevy routine before moving on (with Hevy on). Don't leave a gym calendar
block without one.

**8. Respect their calendar.** Never schedule on top of an all-day commitment
or a confirmed evening event. Move it or drop it, and say which.

**9. Tell them the shape.** A few lines — the week's total, where the long
run sits, anything to watch. Not a table.

## Priority order under pressure

When a week can't all happen:

1. **Long run** — the irreplaceable session
2. **Total weekly volume** — swap sessions around freely to protect it
3. **Individual easy runs**
4. **Gym** — most droppable

Say this out loud when they're stressed about a week. Knowing what to
sacrifice is more useful than being told to do everything.

## Keeping the plan file current

When a race is booked, a decision made (tune-up race, long-run day moved), or
a phase's targets revised, update `athlete/plan.md` in the same run — the
next coach run only knows what's written down. Raise the "Upcoming" items at
the right time, once.
