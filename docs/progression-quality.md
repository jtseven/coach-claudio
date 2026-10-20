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

The baseline is the **median of at least three** positive, complete, explicitly
tolerated normal weeks within the preceding eight weeks. It is not the peak or
the previous down week. Duplicate dates, partial/current weeks, negative or
non-finite history are rejected. Gaps in the recent weekly log or an unconfirmed
latest week's coverage prevent a numerical allowance. A last week more than 14
days old is stale. Missing weeks mean unknown coverage, never a manufactured zero.

| Mode | Default policy allowance (not a target) |
| --- | --- |
| `build` | With an eligible baseline and a complete/tolerated latest normal week: the smaller of last week × 1.10 and baseline × 1.10. With sparse but complete history: hold observed volume, no automatic increase. |
| `planned-down` | Latest kind must be `planned-down`, nonzero; return at most to the eligible normal baseline. |
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
- `streams` reports observed/missing HR seconds and contextual limitations.
  Sensor ratios and decoupling categories are observations, not diagnoses of
  aerobic capacity, illness or injury risk.
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
assertion, not machine-verified symptom clearance. The validator checks weekly
running km only, not session intensity, daily load distribution, or a medical
return protocol. Reducing/holding volume can always be appropriate even when a
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
