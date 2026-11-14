# Progression and data quality

These are deterministic coaching guardrails, not a training engine, clinical clearance,
or an injury predictor. The familiar 10% rule is a configurable heuristic: it does
**not** scientifically guarantee prevention. An accepted proposal only passes the
recorded-volume policy. Symptoms, intensity, long-run size, terrain, life stress and
professional advice can still make it inappropriate.

## Weekly history and validation

`analyze.py progression` reads the usual `athlete/state/weekly_log.jsonl` (or
`--log PATH`). It never writes history or calls an API. Existing commands/flags
remain available; new JSON quality/progression fields are additive. Legacy
`max_next_week_km` is now nullable rather than an unconditional last-week × 1.1.

Each completed week needs a Monday `week_of` and nonnegative finite `actual_km`.
For eligible baseline weeks it also needs:

```json
{"week_of":"2026-09-07","actual_km":30,"week_kind":"normal","tolerated":true,"coverage_complete":true}
```

These are generic synthetic values, not a prescription. `weeklog` can record the
metadata with `--week-kind normal --tolerated --coverage-complete`. Do not infer
these flags from distance, a target hit, an easy HR or a phase label. Confirm the
activity list is complete and ask whether the work was tolerated. Other kinds:
`planned-down`, `travel`, `illness`, `pain`, `illness-pain-return`, `unknown`.
Default flags are unconfirmed/false. Existing logs remain readable, but old rows
without evidence do not silently become eligible normal weeks. No backfill or
retrospective tolerance is invented.

At least three positive, complete, explicitly tolerated normal weeks within
the preceding eight weeks establish eligible history. `baseline_km` retains
the median as descriptive context; `reference_km` is the **most recent** eligible
normal week. Builds use the latest completed normal week, not the median; a
planned-down return uses the normal week immediately before the down block.
Intervening illness/travel/unknown weeks cannot be bridged by calling a later
week planned-down. This removes the median rollback without using a historical
maximum. Duplicate dates, partial/current weeks, negative or
non-finite history are rejected. Gaps in the recent weekly log or an unconfirmed
latest week's coverage prevent a numerical allowance. A last week more than 14
days old is stale. Missing weeks mean unknown coverage, never a manufactured zero.

| Mode | Default policy allowance (not a target) |
| --- | --- |
| `build` | With an eligible baseline and a complete/tolerated latest normal week: last week × 1.10; the multiweek median does not cap the build. With sparse but complete history: hold observed volume, no automatic increase. |
| `planned-down` | Latest kind must be `planned-down`, nonzero; return at most to the most recent eligible normal week before the down block. |
| `travel-return` | Latest kind must be `travel`; at most 75% of the eligible normal baseline. Cross-training hours never become running-km credit. Can explicitly review a zero-running travel week. |
| `illness-pain-return` | No automatic return allowance. Require a separately reviewed `--return-cap-km`; cap it further at 50% of the eligible normal baseline. This is not medical clearance. |

A zero week cannot be treated as an ordinary build or planned down week. Without
an eligible baseline, no numerical return is inferred; get context and review
conservatively rather than jumping to a historical peak. A latest illness/pain
week cannot be bypassed with build/down/travel labels. Repeated illness returns
still require an explicit reviewed cap and evidence each time, not a ratchet.

All returns require `--reason` and `--evidence`; **every validation proposal**,
including builds, requires both. Text is recorded as supplied, not independently
verified by the tool. Never pass an empty placeholder as purported evidence.

```bash
python3 skill/scripts/analyze.py --json progression --log synthetic-weeks.jsonl \
  --as-of 2026-10-05 --mode planned-down --proposed-km 31 \
  --reason "scheduled recovery week" \
  --evidence "recent complete normal weeks tolerated; current symptoms reviewed"
```

For synthetic complete/tolerated normal weeks of 30, 32 and 31 km followed by a
planned 22 km down week, the baseline/return allowance is 31 km, not the old
24.2 km ceiling. This resolves the down-week trap without making every large
week-on-week increase acceptable. The same history with a zero travel week
uses the discounted baseline, not +10% of zero and not a full automatic rebound.
Illness/pain requires its own reviewed cap regardless of past normal volume.

Exit codes: 0 for a review or an accepted proposal; 2 for a rejected proposal;
nonzero with an explanatory error for malformed history/policy. JSON includes
`accepted`, `max_next_week_km`, `baseline_km`, `baseline_weeks`, `reasons`, the
selected policy, and limitations. `accepted: null` means no proposal was checked.
`trend`, `block` and `week` expose the same policy and support its mode/reason/
evidence/date/configuration options. Raw activity rollups cannot establish
coverage or tolerance, so `week` normally gives no progression allowance.
Plan projections are never logged or validated as completed actual history.
On Sunday after all sessions finish, `--closed-week YYYY-MM-DD` (that week's
Monday) explicitly closes the reporting period. It requires reason/evidence
and complete coverage; it is rejected on other weekdays or for other weeks.
Use the real review date; do not spoof a future Monday with `--as-of`.
`trend`, `block` and `week` support the same close-out flag.

Optional configuration (CLI flags override config):

```toml
[progression]
build_pct = 10         # range 0..10; --build-pct
travel_fraction = 0.75 # range (0,1]; --travel-fraction
illness_fraction = 0.5 # range (0,0.5]; --illness-fraction
```

These are conservative administrative defaults, not evidence-based return-to-sport
protocols. Lower them when context warrants; they do not override current symptoms.
Allowances are rounded down to 0.1 km before validation, so an accepted proposal
cannot exceed its displayed cap. Repeated missed targets (at least two targeted
recent weeks, none reaching 95%) hold builds at observed volume rather than
printing hold advice alongside a higher allowance.

## Quality, coverage and uncertainty

- `runlog --missing ACTIVITIES` shows expected/logged/missing counts and date span
  **within the supplied list**. Duplicate activity IDs are counted once. It cannot
  establish that an API export is complete or that an unrecorded session did not
  occur. `coverage_complete` here only describes matching that supplied list.
- `efficiency` and `zonedist` show logged and usable run counts, excluded/missing
  counts, date span, staleness and low/limited confidence. Full activity coverage
  remains unknown. Fewer than six usable runs or usable history older than 14 days
  gives low confidence. This sample threshold is an operational warning, not a
  statistical significance test. A recent run without usable metrics does not
  make old efficiency evidence fresh. No usable data still returns quality
  information rather than inventing a trend. EF change comparisons are suppressed
  when usable history is stale. The `--weeks` view selects logged week buckets,
  not proof of a continuous, complete calendar window.
- Zone percentages describe observed classified HR time only. Missing runs and
  missing HR can bias them. `unclassified_moving_s` shows moving time not assigned
  to low/mid/high among the inspected rows. It does not estimate unlogged time.
- `streams` reports and persists observed/missing HR and paired speed/HR and
  power/HR seconds. EF uses speed and HR from the same samples. Efficiency
  trends require at least 90% HR coverage and 90% coverage for the respective
  paired metric. This is an operational filter, not a validated significance
  threshold. Legacy rows without coverage are unknown and excluded from these
  comparisons; do not backfill complete coverage from averages. Raw zone totals
  still describe observed time and retain planned tempo/MP work.
- `streams` reports observed/missing HR seconds and contextual limitations.
  Sensor ratios and decoupling categories are observations, not diagnoses of
  aerobic capacity, illness or injury risk.
- Load updates lock the read/merge/write transaction and atomically replace
  a unique temporary snapshot; overlapping scheduled/conversational writers
  cannot silently overwrite another writer's activities. A malformed saved
  store fails a write rather than being replaced as empty.
- `load` retains `load: null` for an activity without relative effort. Known zero
  effort is distinct. CTL/ATL/TSB compute only the observed component; an absent
  record is an arithmetic empty day, not verified rest. Quality includes known/
  missing counts, dates and days since the latest activity. Fewer than 42 days,
  any missing effort, or a latest record older than seven days suppresses form
  bands. Otherwise confidence remains limited because upstream completeness is
  unknown. `weeklog` effort totals are likewise observed sums with explicit
  known/missing counts. Future records are excluded from an as-of load review.
- Charts say observed load/trends and use descriptive model-balance labels rather
  than “high risk”. They are not a replacement for the CLI quality report.
- Cadence is speed/terrain/body/device dependent; there is no universal cadence
  threshold diagnosing overstriding. Watch sleep-stage estimates cannot diagnose
  poor recovery. Use perceived symptoms, comparable conditions and professional
  assessment where appropriate, not a number alone.

No live-data backfill is part of this change. Evidence/tolerance text is a human
assertion, not machine-verified symptom clearance. Weekly validation still checks running km only, not session intensity,
daily distribution or a medical return protocol. The separate `session` command
adds advisory exposure comparisons; its warnings do not change acceptance of
a weekly proposal or imply that an unflagged session is safe. Reducing/holding volume can always be appropriate even when a
proposal passes.

## Offline verification

```bash
python3 -m unittest discover -s tests -p test_progression_quality.py -v
bash tests/run.sh
```

Tests use temporary `COACH_HOME` directories and synthetic logs only. They cover
the down-week return, zero/travel cross-training, explicit illness caps, missing
reason/evidence, malformed/partial/stale/sparse history, consistent rollups,
missing effort, incomplete run-log coverage and insufficient trend evidence.

## Session review and feasibility

```bash
python3 skill/scripts/analyze.py --json session --date 2026-10-11 \
  --proposed-km 18 --proposed-minutes 115 --weekly-km 40
python3 skill/scripts/analyze.py --json progression --peak-km 50 --weeks-to-peak 12
```

`session` compares the proposal with logged runs in the preceding 30 days,
excluding the session date and future entries. It reports distance ratio,
longest duration and weekly share. A distance ratio over 1.10, a new duration,
about three hours, or half the week in one run prompts review; none is a
universal prohibition or clearance. The >10% distance observation comes from
[Frandsen 2025](https://bjsm.bmj.com/content/59/17/1203), a cohort rather than a
causal safety trial. Other flags are coaching review conventions. Missing logs
can bias every comparison. Intensity, terrain and recovery still need review.

The optional progression projection starts with the current reviewed allowance
(including a planned-down return), then assumes every subsequent build week
grows at `build_pct`, ignoring later down weeks, travel, illness and taper. It is only an
optimistic upper bound: exceeding it shows the target needs revision, while
being below it does not establish feasibility. No allowance produces an unknown
projection. Actual block planning must include the real calendar.
