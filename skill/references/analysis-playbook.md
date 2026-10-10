# Analysis playbook

How to interrogate a run properly. Summary statistics — distance, pace, average
HR — answer "what happened". These techniques answer "what does it mean", which
is the part the athlete can't get from the Strava app themselves.

Read this before analysing any specific activity.
`<skill>/scripts/analyze.py` implements most of the arithmetic — call it by
full path, since a check-in doesn't run from the skill folder. Personal
numbers (HRmax, easy ceiling, VDOT, power band) come from the athlete config,
so the commands below mostly need no flags for them.

## Contents

1. Start here: intent versus execution
2. Ceiling adherence
3. HR/power decoupling
4. Matched-segment comparison
5. Race prediction and zone recalibration
6. Confounders — read before concluding anything
7. Weekly rollups
8. Recovery data — what their body did overnight
9. Running form — cadence, ground contact, vertical oscillation
10. Training load — fitness, fatigue, form
11. The aerobic trend — efficiency and intensity distribution
12. Charts

---

## 1. Start here: intent versus execution

Before any clever analysis, answer the simple question: **what was this session
supposed to do, and did it?**

Look up what the calendar planned. An easy run's job is to add aerobic volume at
low cost. A long run's job is time on feet. If an "easy 5 km" was run at 165 bpm,
it didn't do its job regardless of how good the pace looks — it bought fatigue
instead of fitness.

This is usually the headline of your reply. The rest is supporting detail.

## 2. Ceiling adherence

For any run marked easy, compute **time above the easy ceiling** as a fraction
of total, not just the average. An average can sit under the ceiling while a
third of the run was above it.

```
python3 <skill>/scripts/analyze.py ceiling --hr <stream> --time <stream>
```

`analyze.py streams` (section 3) prints the same figure along with
everything else a run's streams can say, so when you're pulling streams
anyway, use that.

Then ask *where* the excursions were. Almost always they cluster on climbs. That
distinction matters, because the coaching response differs:

- **Excursions on hills** — terrain, not indiscipline. The fix is to slow further
  or walk the steepest section, not to abandon hilly routes. Hills build strength
  they'll want later in the season.
- **Excursions spread evenly, or in the first kilometre** — they set off too fast.
  That's the habit to correct.
- **Excursions rising through the run at constant pace** — cardiac drift, which
  points at heat, dehydration, or fatigue rather than pacing.

## 3. HR/power decoupling

When Strava records running power (`average_watts`, and a `watts` stream —
from a watch or footpod that measures it), this is the single most useful
signal for detecting fatigue, heat stress, or a bad night, because it separates
*mechanical work done* from *cardiovascular cost*. No power data → skip this
section; matched segments (section 4) do a similar job with pace.

The metric is **watts per beat**: `average_watts / average_heartrate`. Higher is
more efficient.

**Build the athlete's band first.** Take five or more of their easy runs at
similar power and compute the ratio for each — for one runner that looked like
this:

```
195 W  141 bpm   1.39
200 W  147 bpm   1.36
204 W  144 bpm   1.42
210 W  141 bpm   1.49
211 W  142 bpm   1.48
```

So roughly 1.36–1.49 was that runner's normal easy-run band. Write the
athlete's observed range into `physiology.wpb_band` in the config and the
profile; `decouple` compares against it. The band is their *usual* range, not
their best: hilly loops can sit well above it. Before calling any reading
"the best this year", check the actual activities — a claim built from the
band alone is how a coach ends up correcting itself the next morning. A reading materially below the band —
more heartbeats bought per watt of work — means the same work is costing them
more, which is what fatigue, heat, alcohol, illness or dehydration all look
like.

Worked example: 198 W at 148 bpm gives 1.33 — below that band. Suggestive of a
short night before. But see confounders below; if that run was also their only
midday run in months, heat explains it just as well.

```
python3 <skill>/scripts/analyze.py decouple --watts 198 --hr 148.4
python3 <skill>/scripts/analyze.py decouple --watts 198 --hr 148.4 --compare-watts 210.9 --compare-hr 142.4
```

**Caveat worth stating out loud:** watts-per-beat is not linear across
intensities. Heart rate has a resting offset, so a very slow run naturally shows
a lower ratio than a fast one. Only compare runs at *similar power*. Comparing an
easy jog against a tempo run using this metric is meaningless.

### Decoupling within one run

The comparison above is between runs. Within a single run, the question is
whether the cost of the work stayed constant: **aerobic decoupling**
compares output per heartbeat in the first half of the steady part with the
second half.

Pull `get_activity_streams` with `time`, `heart_rate`, `velocity_smooth`,
`moving`, `distance`, `altitude` (and `watts` when the run has power) at
`resolution` 300 — a sample every 10–30 s is plenty, and full resolution is
thousands of points you'd have to write out. Save the JSON to a file:

```
python3 <skill>/scripts/analyze.py streams --streams <file>
python3 <skill>/scripts/analyze.py streams --streams <file> --append --id <id> --date <start_local>
```

It leaves the first 10 minutes out as warm-up (`--skip-warmup`), uses power
when there is any (hills don't fool it) and pace otherwise, and also prints
efficiency, time above the ceiling and time per zone. `--append` records the
run in the run log for the trends in section 11; a run already there is
skipped.

- **Under 5%** — coupled: aerobically solid at this effort for this long.
  On a long run that is the result to name.
- **5–10%** — the effort is at the edge of their aerobic base for that
  duration — or heat, dehydration, no fuel. Check section 6 first.
- **Over 10%** — the effort outran the base. On a long run the fix is a
  slower start, fuel, or a shorter run, not more willpower.
- **Negative** — HR fell against output: an easier or downhill second half,
  or a sensor artifact.

Only meaningful for steady runs of 40+ minutes; intervals and fartlek
decouple by design. With pace as the basis, the script warns when the halves
differ in climbing — believe the warning. A heat-driven 8% says nothing
about fitness, so `weather.py history` before you interpret it.

## 4. Matched-segment comparison

The most rigorous tool available, and the one that beats every confounder except
weather.

Strava segments are fixed stretches of road. When they run the same route twice,
`get_activity_performance` returns the same `segment_id` in both activities, with
time, average HR and average watts for each effort. Comparing the *same segment*
holds terrain, gradient and distance perfectly constant.

Procedure:
1. Pull `get_activity_performance` for today's run.
2. Note the `segment_id` values.
3. Search recent activities for the same route and pull theirs.
4. Compare time, watts and HR on shared segments.

Worked example — a flat 357 m segment on a regular loop:

```
three weeks ago   136 s   192 W   HR 143
today             149 s   179 W   HR 148
```

Thirteen seconds slower, thirteen watts less work, five beats *higher*. Same
tarmac, so terrain is fully controlled. That is a genuine decoupling signal.

Worth noting in the profile: the athlete's frequently repeated segments, once
you've seen them come up a few times.

## 5. Race prediction and zone recalibration

Two independent methods, worth running both and reporting the range.

**Riegel:** `T2 = T1 × (D2/D1)^1.06`. Simple, tends to be optimistic for the
marathon specifically because it assumes marathon-adequate training — which for
most amateurs is precisely the thing that's missing. Treat a Riegel marathon
prediction as a ceiling on their *speed potential*, not a realistic finish time
until the volume is there.

**VDOT (Daniels):** derives training paces as well as race equivalents, which is
what makes it more useful for prescription.

```
python3 <skill>/scripts/analyze.py vdot --distance 9762 --time 2867
python3 <skill>/scripts/analyze.py paces            # VDOT from the config
python3 <skill>/scripts/analyze.py riegel --distance 5484 --time 1518 --target 42195
```

**Critical speed** is the third view, from their best efforts rather than a
single race: distance against time is close to a straight line between
about 2 and 25 minutes of maximal running, and its slope is the fastest pace
they can hold for a long while (near threshold, ~30–40 min race pace). The
intercept, D', is the distance they can run above it before they're done.

```
python3 <skill>/scripts/analyze.py cs --effort 1000:228 --effort 3000:760 --effort 5000:1330
python3 <skill>/scripts/analyze.py cs --performance <get_activity_performance json, or a list>
```

It wants maximal efforts at 3+ distances spanning at least 2.5x in duration
— a parkrun, a hard 3 km, a 1 km rep set all-out. **Splits inside one race
are not maximal efforts** (the fastest kilometre of a 5k was paced), and the
script warns when every effort comes from one activity. A low R² or a D'
outside ~100–400 m means one of the efforts wasn't all-out. Use CS to check
threshold pace against VDOT, and for "how hard can I go in this 4 km
tempo?": anything faster than CS burns D' and cannot be held.

**Recalibrate when:** they race, they hit a notable parkrun, or roughly every
8–12 weeks — the profile says when the current numbers date from and when the
next review is due. When you recalibrate, update `athlete/config.toml` and
`athlete/profile.md`, and tell them what changed and what to re-enter on their
watch and in Strava (custom zones are set there by hand — you can't write them,
the Strava MCP is read-only).

## 6. Confounders — read before concluding anything

Elevated HR at a given effort has many causes and you will be wrong regularly if
you jump to the interesting one. Before attributing anything, check:

- **Time of day and heat.** Most athletes run at a habitual time of day. A
  midday summer run is not comparable with their usual evening ones; heat alone
  moves HR 5–10 bpm at fixed effort. Check `start_local` — and then stop
  guessing, because you can look it up:

  ```bash
  python3 <skill>/scripts/weather.py history --at 2026-08-09T10:51
  ```

  Pass the activity's `start_local`. This is the single biggest upgrade
  available to this playbook: the honest-but-unsatisfying "fatigue and heat
  explain this equally well" becomes "it was 25° and 68% humidity at the
  start, against their usual 17° evening runs — heat is the simpler
  explanation." Check it *before* attributing a poor watts-per-beat reading
  to fatigue, not after.
- **Terrain.** 15 m/km of climbing on a hilly loop versus 4 m/km on a flat path
  is a large difference. Check `elevation_gain` per km, and note the athlete's
  regular hilly and flat routes in the profile.
- **Treadmill.** Treadmill runs (`VirtualRun`, or a title that says so) have no
  wind cooling and often suspect distance calibration.
- **Cardiac drift.** In runs over ~75 minutes, HR rises at constant effort. A
  2-hour run averaging 142 is a *better* performance than a 30-minute run
  averaging 142.
- **Sensor artifacts.** Optical HR drops out. The signature is a flat line —
  identical values for minutes at a stretch, then a jump of 30+ bpm. If you see
  it, exclude that window and say the average is unreliable. It happens on race
  files too, which matters when a race sets HRmax.
- **Moving versus elapsed time.** Strava best-efforts use elapsed; pace summaries
  use moving. They will disagree and that's not a finding.

**When two explanations fit equally well, say so.** "This is consistent with a
short night, but midday heat explains it just as well and I can't separate them"
is more useful than a confident guess.

But check first, because tiebreakers are often available: weather settles
heat, and with Apple Health on, overnight resting HR, HRV and sleep settle
fatigue (section 8). Between them, most "I can't separate these" situations
are separable — say the honest uncertain thing only after you've looked, not
instead of looking.

## 7. Weekly rollups

For "how's my week going", aggregate running distance Monday to Sunday, exclude
cycling and hiking from the running total, and compare against the week's target
event on the calendar.

```
python3 <skill>/scripts/analyze.py week --activities <json-from-list_activities>
```

Report actual versus target, what's left, and — if they're behind — which
sessions to prioritise rather than implying they should cram. Priority order: long run,
total volume, easy runs, gym.

## 8. Recovery data — what their body did overnight

Only with `apple_health` on: the watch exports to the coach machine through
Health Auto Export (setup in `docs/deployment.md`). This is the evidence for
everything the coach would otherwise have to ask about or guess.

```bash
python3 <skill>/scripts/health.py summary        # the morning read
python3 <skill>/scripts/health.py day --date 2026-08-13   # everything
```

`summary` prints sleep (with stage split), HRV, resting HR, respiratory rate,
wrist temperature and blood oxygen, each against its own 28-day baseline, and
ends with a `NOTABLE:` line when something crosses a threshold. **The flags are
the point** — read them, and stay quiet about the rest on a normal day.

What each marker is actually good for:

- **HRV and resting HR** — the fatigue pair. HRV suppressed to ≤80% of baseline,
  or resting HR ≥5 bpm above it, means autonomic load: hard training, poor
  sleep, alcohol, or oncoming illness. Either one alone is a nudge to keep the
  day easy; both together on a quality day is a real reason to move the session.
- **Sleep** — under 6 h is flagged. Deep and REM matter as well as the total; a
  7-hour night with 20 minutes of deep sleep is not a recovered night. Do not
  moralise about it, just factor it into what you prescribe.
- **Respiratory rate and wrist temperature** — the illness pair, and the reason
  they're worth watching: both rise a day or two *before* they feel ill. Together
  with a resting HR jump, that's the pattern that justifies saying "this looks
  like something coming on, keep today genuinely easy" before they notice.
- **Blood oxygen** — background; only worth mentioning if it drops materially.
- **Cardio recovery** (1-minute HR drop after effort) — logged on training days,
  and one of the better fitness trends available. A rising number over months is
  real aerobic improvement. Very sensitive to how the run ended (a sprint finish
  versus a jog-down), so read the trend, never one value.
- **VO2max** — Apple's estimate. Directionally useful over months, noisy week to
  week, and **it drifts upward when they run faster rather than fitter** —
  Apple's model rewards pace. A rise of several points in a few months is partly
  real only if resting HR and HRV corroborate it. Use it as encouragement on a
  long trend, never as a fitness verdict, and never convert it into a race
  prediction — VDOT from an actual race result is the number for that.
- **Resting HR down and HRV up together, over months,** is the clearest evidence
  the aerobic work is working — better evidence than any single run. Worth saying
  when it's true.

```bash
python3 <skill>/scripts/health.py trend --metric vo2_max
python3 <skill>/scripts/health.py trend --metric resting_heart_rate
```

**The relationship to Strava is one-way:** Strava says what they did, Apple
Health says what it cost them. When those disagree — a run that looked fine but cost a
5-bpm resting HR rise the next morning — the cost is the more interesting half
and usually the thing worth telling them.

## 9. Running form — cadence, ground contact, vertical oscillation

The watch records biomechanics Strava never shows. Read them for a run day:

```bash
python3 <skill>/scripts/health.py run                 # latest run day
python3 <skill>/scripts/health.py run --date 2026-08-13
```

**Read the caveat in the output, and mean it.** `health.py run` averages the
export's buckets across the whole date — so these are figures for all running
that day, not one activity. Usually that's a single run, so they read as that
run — but on a day with two runs, or a run plus a walk Apple counted as running,
they blend. Always pair them with the actual Strava activity rather than
presenting them as it. Sanity-check the speed too: a day showing 118 spm at
7 km/h is a walk being counted, not a form collapse.

What's worth acting on, in order:

- **Cadence** (derived: speed ÷ stride length) — the one form metric with a clear
  action attached. Around 160 spm or below is on the low side for most runners;
  low cadence with long stride is the overstriding pattern that loads the knee
  and shin, which matters most for an athlete whose plan hinges on adding
  volume without injury. The fix is boring and effective:
  same pace, slightly shorter steps, aiming for ~5% higher over weeks. Raise it
  when it's relevant — a niggle in the knee or shin, or a volume step-up — not as
  a recurring lecture.
- **Ground contact time** — falls as running economy improves.
  Slow-moving; a monthly trend, not a daily talking point.
- **Vertical oscillation** — bounce. Lower is generally more economical,
  but chasing it directly is a bad idea; it improves as a by-product of cadence.
- **Stride length and running speed** — mostly inputs to cadence. Note that speed
  here is a whole-day running average, so it drops on a day with warm-up walking
  mixed in.
- **Running power** — Apple's estimate, and **not interchangeable with Strava's
  `average_watts`**. The watts-per-beat band in section 3 is calibrated on
  Strava's numbers; feeding Apple's power into that comparison would produce a
  confident wrong answer. Keep the two sources in separate arguments.

Form metrics move slowly and are noisy day to day. The honest use is a trend
across weeks, or as supporting evidence for something else — "the knee complaint
lines up with cadence dropping to 158 on the longer runs" is real coaching; "your
vertical oscillation was 0.2 cm higher today" is noise dressed as insight.

## 10. Training load — fitness, fatigue, form

```bash
python3 <skill>/scripts/analyze.py load                                  # from the saved history
python3 <skill>/scripts/analyze.py load --activities <json> --save       # add activities first
```

Each day's load is Strava's `relative_effort` (HR-based, in
`list_activities`, every sport but commutes). **Fitness** is its 42-day
average, **fatigue** its 7-day average, **form** the gap — expressed as a
percentage of fitness so the bands work on Strava's scale:

- **below −30%** — high risk: fatigue well beyond what fitness absorbs.
  Combined with a niggle or poor sleep, that's the week to cut.
- **−30 to −10%** — productive: a training block carrying load. Normal.
- **−10 to +5%** — neutral.
- **+5 to +25%** — fresh: a down week, or race-ready after a taper.
- **above +25%** — fitness starting to decay; fine after illness or a
  holiday, a problem if it lasts.

Use it as evidence, not a verdict:

- **"Should I train today?" / "legs dead"** — form below −30% backs up
  backing off; form near neutral with dead legs points at sleep, food or
  illness rather than load.
- **Taper and race week** — form should climb into the fresh band by race
  day. If it's still negative a week out, the taper started too late.
- **Ramp** — fitness gained per week. A ramp far above the weeks before is
  the load-side version of the +10% rule, and it counts the bike and the
  gym that running km don't.

It reads only what was saved: if the latest activity in the history is
older than the athlete's last session, add the recent activities first.
With under 42 days of history, form isn't judged — the script says so.
Relative effort needs HR; activities without it count as zero.

## 11. The aerobic trend — efficiency and intensity distribution

The run log (section 3, `--append`) holds every analysed run. Two readings
come out of it:

```bash
python3 <skill>/scripts/analyze.py efficiency     # easy-run efficiency by week, long-run decoupling
python3 <skill>/scripts/analyze.py zonedist       # weekly time low / mid / high
```

**Efficiency** (EF) is speed per heartbeat — metres per minute per beat — on
easy runs only (20+ min, held under the ceiling). Its weekly median rising is
the clearest sign in their own runs that the base is building, and it
translates into the sentence athletes understand: "≈ pace at the ceiling",
the pace their easy heart rate buys. Rules:

- Changes under ~2% are noise.
- **Terrain moves it.** The m/km column is there for a reason: a week of flat
  routes raises EF without any fitness. Compare like with like.
- **Heat lowers it** by several percent in summer. A dip in a heatwave isn't
  lost fitness.
- With power, watts per beat is the better number (section 3's band).

**Intensity distribution** splits running time into low (at or under the
easy ceiling), high (at or over threshold HR) and the middle. Most of an
amateur's time belongs in low — roughly three quarters or more. A large
middle is the classic pattern: easy runs drifting up, hard runs not hard,
everything moderately tiring. That's the time to say so, with the number;
when the distribution is fine, it doesn't need a mention. Recoveries inside
intervals count as low, so a session-heavy week reads a little easier than
it was.

## 12. Charts

`charts.py` renders PNGs from the same data. It needs matplotlib, which uv
provides; run it through `uv run --script`:

```bash
uv run --script <skill>/scripts/charts.py run --streams <file> --title "Sunday long run"
uv run --script <skill>/scripts/charts.py load
uv run --script <skill>/scripts/charts.py aerobic
uv run --script <skill>/scripts/charts.py weekly
```

Each prints the PNG path. In a Telegram conversation, attach it with the
reply tool's `files`; in the terminal, give the path. Send one when a picture
answers the question better than a number — "show me my long run", "am I
getting fitter?" — not by default. The weekly close-out sends the weekly,
load and aerobic charts on its own (with `weekly_chart` on).
