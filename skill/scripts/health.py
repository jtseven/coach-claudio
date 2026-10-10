#!/usr/bin/env python3
"""Reads back the Apple Health data stored by health_server.py.

  summary   Recovery snapshot: sleep, HRV, resting HR, illness signals
  run       Running form metrics for a day, with derived cadence
  trend     Any metric over time (VO2max, weight, resting HR, ...)
  day       Everything recorded on one day - the catch-all
  block     Slow recovery markers, last 4 weeks vs the 4 before

Standard library only. Every subcommand takes --json.

ONE THING TO UNDERSTAND BEFORE TRUSTING ANY NUMBER HERE: Health Auto Export
can aggregate by **hour** (the recommended setup), so a metric normally holds
many datapoints per calendar day - or by day, one datapoint each, and an
archive can hold both shapes at once. Never sum or average a metric by
hand - go through `value()`, which knows whether the
metric accumulates over the day (steps, energy, distance: SUM_METRICS) or is
a rate to be averaged. Averaging what should be summed understated one day's
active energy as 80 kJ instead of 1754.

Hourly buckets are noisy where a bucket rests on few samples: single hours of
HRV at 138 and 147 ms show up against a baseline of 60. Use medians over a
window rather than trusting one hour, which is what `overnight_value()` does.
Which markers that basis is good enough for is decided per metric by bucket
count - see OVERNIGHT_BASIS_METRICS.

There are still no per-workout records, so the running form metrics (`run`)
describe *all the running done that day*, not a specific activity. On most
days that is one run, so in practice they read as that run - but on a day
with two runs, or a run plus a long walk that Apple counted as running, the
numbers blend. Strava remains the source of truth for what a given activity
was; these add the biomechanics Strava doesn't record.
"""

import argparse
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta

import coachconf as cc

DAILY_DIR = str(cc.HEALTH_DIR)

KJ_PER_KCAL = 4.184
TS_FMT = "%Y-%m-%d %H:%M:%S %z"

# --------------------------------------------------------------------------
# metric registry
# --------------------------------------------------------------------------
# label, unit shown, decimals, and which direction is *bad* for a flag:
#   "up"   = a rise above baseline is the warning (resting HR, wrist temp)
#   "down" = a fall below baseline is the warning (HRV, VO2max)
#   None   = descriptive only, never flagged
METRICS = {
    # recovery
    "heart_rate_variability": ("HRV", "ms", 0, "down"),
    "resting_heart_rate": ("Resting HR", "bpm", 0, "up"),
    "respiratory_rate": ("Respiratory rate", "/min", 1, "up"),
    "apple_sleeping_wrist_temperature": ("Wrist temp", "degC", 2, "up"),
    "blood_oxygen_saturation": ("Blood oxygen", "%", 1, "down"),
    "breathing_disturbances": ("Breathing disturbances", "", 1, "up"),
    # fitness markers
    "vo2_max": ("VO2max", "ml/kg/min", 1, "down"),
    "cardio_recovery": ("1-min HR recovery", "bpm", 0, "down"),
    "walking_heart_rate_average": ("Walking HR", "bpm", 0, "up"),
    # running form
    "running_power": ("Running power", "W", 0, None),
    "running_speed": ("Running speed", "km/h", 2, None),
    "running_stride_length": ("Stride length", "m", 2, None),
    "running_ground_contact_time": ("Ground contact", "ms", 0, "up"),
    "running_vertical_oscillation": ("Vertical oscillation", "cm", 1, "up"),
    # body
    "weight_body_mass": ("Weight", "kg", 1, None),
    "body_fat_percentage": ("Body fat", "%", 1, None),
    "lean_body_mass": ("Lean mass", "kg", 1, None),
    "body_mass_index": ("BMI", "", 1, None),
    # daily load
    "active_energy": ("Active energy", "kJ", 0, None),
    "basal_energy_burned": ("Basal energy", "kJ", 0, None),
    "apple_exercise_time": ("Exercise time", "min", 0, None),
    "physical_effort": ("Physical effort", "kcal/hr/kg", 1, None),
    "step_count": ("Steps", "", 0, None),
    "walking_running_distance": ("Walk+run distance", "km", 2, None),
    "cycling_distance": ("Cycling distance", "km", 2, None),
    "cycling_power": ("Cycling power", "W", 0, None),
    "cycling_cadence": ("Cycling cadence", "rpm", 0, None),
    "swimming_distance": ("Swim distance", "m", 0, None),
    "flights_climbed": ("Flights climbed", "", 0, None),
    "time_in_daylight": ("Daylight", "min", 0, None),
    "apple_stand_hour": ("Stand hours", "", 0, None),
    "apple_stand_time": ("Stand time", "min", 0, None),
    # gait, from the phone in a pocket rather than the watch - descriptive
    # background, not training signal
    "walking_speed": ("Walking speed", "km/h", 2, None),
    "walking_step_length": ("Walking step length", "cm", 0, None),
    "walking_asymmetry_percentage": ("Walking asymmetry", "%", 1, "up"),
    "walking_double_support_percentage": ("Double support", "%", 1, None),
    "stair_speed_up": ("Stair speed up", "m/s", 2, None),
    "stair_speed_down": ("Stair speed down", "m/s", 2, None),
    # logged elsewhere and synced in: food-tracking apps, smart scales
    "dietary_energy": ("Dietary energy", "kJ", 0, None),
    "protein": ("Protein", "g", 0, None),
    "height": ("Height", "m", 2, None),
    "mindful_minutes": ("Mindful minutes", "min", 0, None),
}

RUN_METRICS = [
    "running_power",
    "running_speed",
    "running_stride_length",
    "running_ground_contact_time",
    "running_vertical_oscillation",
]

# Deviations worth interrupting the athlete for. Direction is taken from METRICS, so
# a value that moves the *good* way never fires.
FLAG_RULES = {
    "resting_heart_rate": ("abs", 5),        # bpm over baseline
    "heart_rate_variability": ("ratio", 0.80),  # <= 80% of baseline
    "respiratory_rate": ("abs", 2),          # /min over baseline - illness
    "apple_sleeping_wrist_temperature": ("abs", 0.4),  # degC over baseline
    "blood_oxygen_saturation": ("abs", 2),   # % under baseline
}
SLEEP_FLAG_H = 6.0


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


# Health Auto Export writes whatever unit the iPhone is set to, per datapoint.
# Change the phone's region or units and it flips, e.g. to imperial: weight in
# lb, wrist temperature in degF, energy in kcal, distance in mi. Read naively,
# 161.6 lb once became a 161.6 kg weight and 97.07 degF a wrist temperature 61 degrees over baseline.
# Days around the switch even mix both systems across hourly buckets, so the
# conversion is per datapoint, on read, and the raw files stay untouched.
UNIT_CONVERSIONS = {
    "lb": ("kg", lambda x: x * 0.45359237),
    "degF": ("degC", lambda x: (x - 32) * 5 / 9),
    "kcal": ("kJ", lambda x: x * 4.184),
    "mi": ("km", lambda x: x * 1.609344),
    "mi/hr": ("km/hr", lambda x: x * 1.609344),
    "ft/s": ("m/s", lambda x: x * 0.3048),
    "ft": ("m", lambda x: x * 0.3048),
    "yd": ("m", lambda x: x * 0.9144),
    "in": ("cm", lambda x: x * 2.54),
}


def normalize_units(day_json):
    """Convert every imperial datapoint in a parsed day file to metric, in place."""
    for dps in (day_json.get("metrics") or {}).values():
        for dp in dps or []:
            conv = isinstance(dp, dict) and UNIT_CONVERSIONS.get(dp.get("units"))
            if not conv:
                continue
            unit, fn = conv
            for key in ("qty", "Min", "Max", "Avg"):
                if isinstance(dp.get(key), (int, float)):
                    dp[key] = fn(dp[key])
            dp["units"] = unit
    return day_json


def read_day(d):
    """One day's export, unit-normalised. Raises like open()/json.load()."""
    with open(os.path.join(DAILY_DIR, f"{d}.json")) as fh:
        return normalize_units(json.load(fh))


def load_days(n, end=None):
    """Newest-first [(date_str, parsed)] for the n calendar days up to `end`.

    Missing days are skipped rather than filled - a day with no export and a
    day without the watch on are the same thing here, and neither should
    become a zero.
    """
    out = []
    last = date.fromisoformat(end) if end else date.today()
    for i in range(n):
        d = (last - timedelta(days=i)).isoformat()
        try:
            out.append((d, read_day(d)))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return out


# With HOURLY buckets (the recommended export setting), several datapoints
# for one metric mean several hours of the same day, not several sources - so how they combine depends on what the metric is. Counts,
# distances and durations accumulate over the day; rates and levels average.
# Getting this wrong is not subtle: averaging 22 hourly buckets of active
# energy reported 80 kJ for a day that actually held 1754.
#
# Files exported with daily aggregation carry one datapoint per day, where sum
# and mean are identical, so both branches stay correct for any history.
SUM_METRICS = {
    "step_count",
    "active_energy",
    "basal_energy_burned",
    "dietary_energy",
    "protein",
    "walking_running_distance",
    "cycling_distance",
    "swimming_distance",
    "flights_climbed",
    "apple_exercise_time",
    "apple_stand_time",
    "apple_stand_hour",
    "time_in_daylight",
    "mindful_minutes",
}

# Markers Apple measures during sleep, so a sleep-window figure is in
# principle truer than a 24-hour mean. Whether it actually is depends on how
# many hourly buckets a night gives you, and a month of hourly history split
# them cleanly in two:
#
#   respiratory rate   7.2 buckets/night  -> range 13.0-17.3, tracks the day
#   blood oxygen       ~7  buckets/night  -> range 94.5-97.5, tracks the day
#   HRV                3.5 buckets/night  -> range 37-139 against a 61 mean
#
# So respiratory rate and blood oxygen move onto the sleep-window basis for
# value, baseline and flag. HRV does NOT: three or four buckets can't absorb
# single-hour spikes (139, 114 and 110 ms against a ~60 baseline), and
# Apple's own daily figure is the steadier number. It stays day-basis for the
# flag and the sleep-window value is shown next to it as context.
#
# Wrist temperature is already a sleep-only daily value with no hourly
# buckets at all, and resting HR is daily by nature - both stay day-basis.
OVERNIGHT_BASIS_METRICS = {
    "respiratory_rate",
    "blood_oxygen_saturation",
}
OVERNIGHT_CONTEXT_METRICS = {
    "heart_rate_variability",
}
# Nights needed on the sleep-window basis before its baseline is trusted.
OVERNIGHT_BASELINE_MIN_DAYS = 10


def hour_of(dp):
    """Datapoint timestamp as a naive datetime, or None."""
    try:
        return datetime.strptime(str(dp.get("date")).strip()[:19],
                                 "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def value(day_json, name, dps=None):
    """One number for the day, aggregated according to the metric's kind."""
    if dps is None:
        dps = day_json.get("metrics", {}).get(name) or []
    vals = [dp["qty"] for dp in dps if isinstance(dp.get("qty"), (int, float))]
    if not vals:
        return None
    return sum(vals) if name in SUM_METRICS else sum(vals) / len(vals)


def overnight_value(days, day, name):
    """Mean of the hourly buckets that fall inside the night's sleep window.

Apple measures HRV, respiratory rate, wrist temperature and blood oxygen
    during sleep, so a 24-hour mean dilutes them with waking hours. The
    window spans two files, because a night that starts before midnight has
    its early hours filed under the previous day.

    Whether the result is trustworthy depends on bucket count and is decided
    per metric in OVERNIGHT_BASIS_METRICS - this function just computes it.

    Uses the MEDIAN, not the mean: hourly HRV is spiky - single hours of
    138 ms (17 Aug) and 147 ms (15 Aug) against a baseline of 60 - because an
    hour may rest on very few samples. One such hour moves a 4-bucket mean by
    20 ms, which would manufacture a recovery story that isn't there.

    Returns (value, n_buckets), or (None, 0) when the window can't be built -
    no sleep record, or hourly data not available for that day.
    """
    by_day = dict(days)
    j = by_day.get(day)
    if not j:
        return None, 0
    sleep = sleep_detail(j)
    if not sleep or not sleep.get("start") or not sleep.get("end"):
        return None, 0
    start, end = hour_of({"date": sleep["start"]}), hour_of({"date": sleep["end"]})
    if not start or not end:
        return None, 0

    prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    picked = []
    for d in (prev, day):
        for dp in (by_day.get(d, {}).get("metrics", {}).get(name) or []):
            ts, qty = hour_of(dp), dp.get("qty")
            if ts and start <= ts <= end and isinstance(qty, (int, float)):
                picked.append(qty)
    if not picked:
        return None, 0
    return statistics.median(picked), len(picked)


def overnight_baseline(days, name, skip_date=None, min_days=None):
    """Baseline built on the same sleep-window basis as the value it judges.

    Mixing bases is how you invent a delta: a sleep-window blood oxygen of
    96.5 against a whole-day baseline of 97.2 looks like a 0.7-point drop and
    is nothing but the two numbers meaning different things. Returns
    (mean, n_nights) or (None, n) when too few nights carry hourly data.
    """
    if min_days is None:
        min_days = OVERNIGHT_BASELINE_MIN_DAYS
    vals = []
    for d, _ in days:
        if d == skip_date:
            continue
        v, n = overnight_value(days, d, name)
        if v is not None and n >= 3:
            vals.append(v)
    if len(vals) < min_days:
        return None, len(vals)
    return sum(vals) / len(vals), len(vals)


# Wrist temperature is one reading per night, dated at sleep *onset*. A night
# that starts at 22:16 is therefore filed under the evening before, while the
# summary reports on the day the athlete woke up - so the morning brief kept
# saying "no reading" for a value that was sitting one file back (during an
# illness, when it mattered most). Look it up by the night's end.
NIGHT_FILED_METRICS = {"apple_sleeping_wrist_temperature"}


def night_filed_value(days, day, name):
    """The reading for the night that ended on `day`, wherever it's filed."""
    by_day = dict(days)
    prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    vals = []
    for d in (prev, day):
        for dp in (by_day.get(d, {}).get("metrics", {}).get(name) or []):
            end = str(dp.get("end") or dp.get("date") or "")[:10]
            if end == day and isinstance(dp.get("qty"), (int, float)):
                vals.append(dp["qty"])
    return sum(vals) / len(vals) if vals else None


def heart_rate_range(day_json):
    """Daily heart rate is exported as Min/Max/Avg rather than a `qty`, so
    it needs its own reader. Max is the useful one: it says whether the day
    contained a hard effort, independent of what got uploaded to Strava."""
    dps = day_json.get("metrics", {}).get("heart_rate") or []
    got = {}
    for key in ("Min", "Max", "Avg"):
        vals = [dp[key] for dp in dps if isinstance(dp.get(key), (int, float))]
        if vals:
            got[key.lower()] = (min(vals) if key == "Min"
                                else max(vals) if key == "Max"
                                else sum(vals) / len(vals))
    return got or None


def latest(days, name):
    """(date, value) of the most recent day carrying this metric."""
    for d, j in days:
        v = value(j, name)
        if v is not None:
            return d, v
    return None, None


def baseline(days, name, skip_date=None):
    """Mean of every other day that has the metric. Deliberately not a
    rolling window on calendar days: the watch isn't worn every night,
    and a gap should widen the window, not thin the baseline."""
    vals = [
        v for d, j in days
        if d != skip_date and (v := value(j, name)) is not None
    ]
    return sum(vals) / len(vals) if vals else None


def fmt(name, v):
    label, unit, dec, _ = METRICS.get(name, (name, "", 1, None))
    txt = f"{v:.{dec}f}"
    return f"{txt} {unit}".strip()


def sleep_detail(day_json):
    """Hours asleep, in bed, and the stage split. Real exports send
    asleep: 0 / inBed: 0 with the truth in totalSleep and the timestamps,
    so read those first and only then fall back to summing stages."""
    dps = day_json.get("metrics", {}).get("sleep_analysis") or []
    out = {"asleep": 0.0, "inBed": 0.0, "core": 0.0, "deep": 0.0,
           "rem": 0.0, "awake": 0.0, "start": None, "end": None}
    found = False
    for dp in dps:
        for stage in ("core", "deep", "rem", "awake"):
            if isinstance(dp.get(stage), (int, float)):
                out[stage] += dp[stage]
        total = dp.get("totalSleep")
        if not isinstance(total, (int, float)) or total == 0:
            total = dp.get("asleep")
        if not isinstance(total, (int, float)) or total == 0:
            stages = [dp.get(k) for k in ("core", "deep", "rem")]
            nums = [s for s in stages if isinstance(s, (int, float))]
            total = sum(nums) if nums else 0
        out["asleep"] += total or 0

        bed = dp.get("inBed") or dp.get("inBedTime")
        if not isinstance(bed, (int, float)) or bed == 0:
            try:
                bed = (
                    datetime.strptime(dp["inBedEnd"], TS_FMT)
                    - datetime.strptime(dp["inBedStart"], TS_FMT)
                ).total_seconds() / 3600
            except (KeyError, ValueError):
                bed = 0
        out["inBed"] += bed or 0
        out["start"] = out["start"] or dp.get("sleepStart") or dp.get("inBedStart")
        out["end"] = dp.get("sleepEnd") or dp.get("inBedEnd") or out["end"]
        found = True
    return out if found and out["asleep"] else None


def emit(payload, lines, as_json):
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(lines))


# --------------------------------------------------------------------------
# summary - the morning recovery read
# --------------------------------------------------------------------------


def cmd_summary(args):
    days = load_days(args.days)
    if not days:
        print("no health data yet")
        return

    lines, flags, missing = [], [], []
    payload = {"flags": [], "missing": []}

    # Sleep: today's file, else yesterday's. Sleep is filed under the day the athlete
    # woke up, so an early check-in before the phone has exported still finds
    # last night under yesterday's date.
    night = next(((d, j) for d, j in days[:2] if sleep_detail(j)), None)
    if night:
        d, j = night
        s = sleep_detail(j)
        label = "last night" if d == date.today().isoformat() else f"night to {d}"
        line = f"Sleep {label}: {s['asleep']:.1f} h asleep"
        if s["inBed"]:
            line += f", {s['inBed']:.1f} h in bed"
        lines.append(line)
        if s["deep"] or s["rem"]:
            lines.append(
                f"  stages: {s['deep']:.1f} h deep, {s['rem']:.1f} h REM, "
                f"{s['core']:.1f} h core"
            )
        payload["sleep_h"] = round(s["asleep"], 2)
        payload["sleep_deep_h"] = round(s["deep"], 2)
        payload["sleep_rem_h"] = round(s["rem"], 2)
        payload["sleep_in_bed_h"] = round(s["inBed"], 2)
        if s["asleep"] < SLEEP_FLAG_H:
            flags.append(f"short sleep ({s['asleep']:.1f} h)")

    # The night we are reporting on. Every marker below has to belong to that
    # same day, or it is not evidence about it.
    ref_day = night[0] if night else date.today().isoformat()

    # Recovery and illness markers against their own baselines.
    for name in ("heart_rate_variability", "resting_heart_rate",
                 "respiratory_rate", "apple_sleeping_wrist_temperature",
                 "blood_oxygen_saturation"):
        label, unit, dec, bad_dir = METRICS[name]
        # Ask for ref_day itself. Going via latest() here was a bug: it returns
        # the newest day carrying the metric, which on a morning when today has
        # already posted a reading is *later* than the night being reported -
        # so a day that genuinely held 7 blood-oxygen buckets got announced as
        # "no reading" with a NOTABLE flag behind it.
        d = ref_day
        if name in NIGHT_FILED_METRICS:
            v = night_filed_value(days, ref_day, name)
        else:
            v = value(dict(days).get(ref_day) or {}, name)
        # Health Auto Export omits individual metrics from a day's payload
        # fairly often. Carrying a neighbouring day's reading forward silently
        # is how a two-day-old resting HR became a NOTABLE flag; say it is
        # missing and never flag on it instead.
        if v is None:
            near_d, near_v = latest(days, name)
            if near_v is None:
                lines.append(f"{label}: no data")
            else:
                when = "last was" if near_d < ref_day else "only"
                lines.append(
                    f"{label}: no reading for {ref_day} "
                    f"({when} {fmt(name, near_v)} on {near_d})"
                )
                payload[name + "_stale_value"] = round(near_v, dec + 1)
                payload[name + "_stale_date"] = near_d
            payload[name + "_missing"] = True
            # A gap is an export problem, not a finding about the body - it
            # goes on its own line so it can never read as a recovery flag.
            gap = ((date.fromisoformat(ref_day) - date.fromisoformat(near_d)).days
                   if near_d else None)
            missing.append(f"{label.lower()} ({gap} day{'s' if gap != 1 else ''})" if gap else label.lower())
            continue
        # Value and baseline must share a basis. For the markers with enough
        # hourly buckets per night, both move to the sleep window; otherwise
        # both stay on the whole day.
        basis = "day"
        base = baseline(days, name, d)
        if name in OVERNIGHT_BASIS_METRICS:
            night_v, n_buckets = overnight_value(days, d, name)
            night_base, n_nights = overnight_baseline(days, name, d)
            if night_v is not None and n_buckets >= 3 and night_base is not None:
                v, base, basis = night_v, night_base, "overnight"
                payload[name + "_overnight_hours"] = n_buckets
                payload[name + "_baseline_nights"] = n_nights
        payload[name + "_basis"] = basis

        key = name
        payload[key] = round(v, dec + 1)
        if base is None:
            lines.append(f"{label}: {fmt(name, v)} (no baseline yet)")
            continue
        payload[key + "_baseline"] = round(base, dec + 1)
        delta = v - base
        line = f"{label}: {fmt(name, v)} (baseline {base:.{dec}f}, {delta:+.{dec}f})"
        if basis == "overnight":
            line += "  [overnight basis]"
        # HRV keeps the steadier whole-day figure for its flag, but the
        # sleep-window number is worth seeing next to it - it reacts harder to
        # a bad night (16 Aug: 37 overnight against 45 across the day).
        if name in OVERNIGHT_CONTEXT_METRICS:
            night_v, n = overnight_value(days, d, name)
            if night_v is not None and n >= 3:
                line += f"  ·  {fmt(name, night_v)} overnight median ({n} h asleep)"
                payload[key + "_overnight"] = round(night_v, dec + 1)
                payload[key + "_overnight_hours"] = n
        lines.append(line)
        rule = FLAG_RULES.get(name)
        if not rule:
            continue
        kind, thr = rule
        worse = delta if bad_dir == "up" else -delta
        if kind == "abs" and worse >= thr:
            flags.append(f"{label.lower()} {delta:+.{dec}f} vs baseline")
        elif kind == "ratio" and v <= base * thr:
            flags.append(f"{label} suppressed ({v:.0f} vs baseline {base:.0f})")

    if not lines:
        print("no health data yet")
        return

    payload["flags"] = flags
    payload["missing"] = missing
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print("\n".join(lines))
    if flags:
        print("NOTABLE: " + "; ".join(flags))
    else:
        print("nothing notable - recovery looks normal")
    if missing:
        # Not for the message. Worth one line to the athlete only when a marker has
        # been gone 3+ days (export settings on the phone), and only once.
        print("MISSING (export gap, not a finding): " + "; ".join(missing))


# --------------------------------------------------------------------------
# run - biomechanics for a running day
# --------------------------------------------------------------------------


def cadence_spm(speed_kmh, stride_m):
    """Steps per minute, derived. Apple exports speed and stride length but
    not cadence, and cadence is the one running-form number with a clear
    coaching action attached, so compute it: steps/min = (m/s / m) * 60."""
    if not speed_kmh or not stride_m:
        return None
    return (speed_kmh / 3.6) / stride_m * 60


def cmd_run(args):
    days = load_days(args.days, end=args.date)

    # The day being analysed: the one asked for, else the most recent day
    # with any running data at all.
    if args.date:
        target = next((pair for pair in days if pair[0] == args.date), None)
    else:
        target = next(
            (pair for pair in days
             if any(value(pair[1], m) is not None for m in RUN_METRICS)),
            None,
        )
    if not target:
        print("no running metrics for that day")
        return
    tdate, tjson = target

    rest = [pair for pair in days if pair[0] != tdate]
    payload = {"date": tdate, "metrics": {}}
    lines = [f"Running form, {tdate}"]

    for name in RUN_METRICS:
        v = value(tjson, name)
        if v is None:
            continue
        label, unit, dec, _ = METRICS[name]
        base = baseline(rest, name)
        entry = {"value": round(v, dec + 1), "unit": unit}
        line = f"  {label:<22} {fmt(name, v)}"
        if base:
            entry["baseline"] = round(base, dec + 1)
            entry["delta"] = round(v - base, dec + 1)
            line += f"   (avg {base:.{dec}f}, {v-base:+.{dec}f})"
        payload["metrics"][name] = entry
        lines.append(line)

    if not payload["metrics"]:
        print(f"no running metrics on {tdate}")
        return

    # Derived: cadence now and on the comparison days.
    cad = cadence_spm(value(tjson, "running_speed"),
                      value(tjson, "running_stride_length"))
    if cad:
        cads = [
            c for _, j in rest
            if (c := cadence_spm(value(j, "running_speed"),
                                 value(j, "running_stride_length")))
        ]
        base = sum(cads) / len(cads) if cads else None
        payload["cadence_spm"] = round(cad)
        line = f"  {'Cadence (derived)':<22} {cad:.0f} spm"
        if base:
            payload["cadence_spm_baseline"] = round(base)
            line += f"   (avg {base:.0f}, {cad-base:+.0f})"
        lines.append(line)

    # Cardio recovery is logged on the day of the effort and is the single
    # best "how fit / how recovered" signal in this dataset.
    cr = value(tjson, "cardio_recovery")
    if cr is not None:
        base = baseline(rest, "cardio_recovery")
        payload["cardio_recovery"] = round(cr, 1)
        line = f"  {'1-min HR recovery':<22} {cr:.0f} bpm"
        if base:
            payload["cardio_recovery_baseline"] = round(base, 1)
            line += f"   (avg {base:.0f}, {cr-base:+.0f})"
        lines.append(line)

    lines += [
        "",
        "Daily aggregates: these describe all running on this date, not one",
        "activity. Read them next to the Strava activity, not instead of it.",
    ]
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# trend - any metric over time
# --------------------------------------------------------------------------


BLOCK_METRICS = [  # (metric, good direction) - the slow-moving markers
    ("resting_heart_rate", "down"),
    ("heart_rate_variability", "up"),
    ("weight_body_mass", None),
    ("vo2_max", "up"),
]


def cmd_block(args):
    """Last n days against the n before, by median - the monthly review's
    recovery half. Medians because a single hourly HRV spike or a bad scale
    reading (a 161.6 that was pounds) must not move a four-week figure."""
    n = args.days
    days = load_days(2 * n)
    cut = (date.today() - timedelta(days=n)).isoformat()
    cur = [(d, j) for d, j in days if d > cut]
    prev = [(d, j) for d, j in days if d <= cut]
    payload, lines = {"days": n}, [f"Recovery markers, last {n} days vs the {n} before (medians)"]

    def med(rows, name):
        vals = [v for _, j in rows if (v := value(j, name)) is not None]
        return (statistics.median(vals), len(vals)) if vals else (None, 0)

    for name, good in BLOCK_METRICS:
        label, unit, dec, _ = METRICS[name]
        (a, na), (b, nb) = med(cur, name), med(prev, name)
        payload[name] = {"current": a, "previous": b, "n_current": na, "n_previous": nb}
        if a is None:
            lines.append(f"  {label:<20} no readings")
            continue
        txt = f"  {label:<20}{a:>7.{dec}f} {unit}"
        if b is not None:
            d = a - b
            better = (good == "down" and d < 0) or (good == "up" and d > 0)
            tag = "" if good is None or abs(d) < 10 ** -dec else (" better" if better else " worse")
            txt += f"   (prev {b:.{dec}f}, {d:+.{dec}f}{tag})"
        if na < n / 3:
            txt += f"   [only {na} readings]"
        lines.append(txt)
    sl_cur = [x["asleep"] for _, j in cur if (x := sleep_detail(j)) and x.get("asleep")]
    sl_prev = [x["asleep"] for _, j in prev if (x := sleep_detail(j)) and x.get("asleep")]
    if sl_cur:
        txt = f"  {'Sleep':<20}{statistics.median(sl_cur):>7.1f} h"
        if sl_prev:
            txt += f"   (prev {statistics.median(sl_prev):.1f})"
        lines.append(txt)
        payload["sleep_h"] = {"current": statistics.median(sl_cur),
                              "previous": statistics.median(sl_prev) if sl_prev else None}
    emit(payload, lines, args.json)


def cmd_trend(args):
    name = args.metric
    if name not in METRICS:
        sys.exit(
            f"unknown metric '{name}'. Known: " + ", ".join(sorted(METRICS))
        )
    days = load_days(args.days)
    series = [(d, v) for d, j in days if (v := value(j, name)) is not None]
    if not series:
        print(f"no data for {name} in the last {args.days} days")
        return
    series.reverse()  # oldest first

    label, unit, dec, _ = METRICS[name]
    payload = {
        "metric": name,
        "unit": unit,
        "points": [{"date": d, "value": round(v, dec + 1)} for d, v in series],
    }
    lines = [f"{label} ({unit}), last {len(series)} readings"]
    for d, v in series[-args.show:]:
        lines.append(f"  {d}  {v:.{dec}f}")

    first, last = series[0][1], series[-1][1]
    payload["change"] = round(last - first, dec + 1)
    payload["first"] = round(first, dec + 1)
    payload["latest"] = round(last, dec + 1)
    lines += [
        "",
        f"{series[0][0]} -> {series[-1][0]}: {first:.{dec}f} -> {last:.{dec}f} "
        f"({last-first:+.{dec}f})",
    ]
    if len(series) >= 6:
        half = len(series) // 2
        early = sum(v for _, v in series[:half]) / half
        late = sum(v for _, v in series[half:]) / (len(series) - half)
        payload["first_half_avg"] = round(early, dec + 1)
        payload["second_half_avg"] = round(late, dec + 1)
        lines.append(
            f"first half avg {early:.{dec}f} -> second half avg {late:.{dec}f} "
            f"({late-early:+.{dec}f})"
        )
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# day - everything recorded, so the coach never parses the JSON by hand
# --------------------------------------------------------------------------


def cmd_day(args):
    d = args.date or date.today().isoformat()
    try:
        j = read_day(d)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"no health data for {d}")
        return

    payload = {"date": d, "metrics": {}}
    lines = [f"Apple Health, {d}"]
    for name in sorted(j.get("metrics", {})):
        v = value(j, name)
        if v is None:
            continue
        if name in METRICS:
            label, unit, dec, _ = METRICS[name]
            shown = f"{v:.{dec}f} {unit}".strip()
        else:
            label, shown = name, f"{v:.2f}"
        payload["metrics"][name] = round(v, 3)
        lines.append(f"  {label:<24} {shown}")
        if name in ("active_energy", "basal_energy_burned"):
            lines[-1] += f"  ({v/KJ_PER_KCAL:.0f} kcal)"

    hr = heart_rate_range(j)
    if hr:
        payload["heart_rate"] = {k: round(v) for k, v in hr.items()}
        lines.append(
            f"  {'Heart rate':<24} "
            + ", ".join(f"{k} {v:.0f}" for k, v in hr.items())
            + " bpm"
        )

    s = sleep_detail(j)
    if s:
        payload["sleep"] = {k: round(v, 2) for k, v in s.items()
                            if isinstance(v, (int, float))}
        lines.append(
            f"  {'Sleep':<24} {s['asleep']:.1f} h asleep "
            f"({s['deep']:.1f} deep / {s['rem']:.1f} REM), "
            f"{s['inBed']:.1f} h in bed"
        )
    if len(lines) == 1:
        print(f"no health data for {d}")
        return
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("block", help="slow markers: last n days vs the n before")
    s.add_argument("--days", type=int, default=28)
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("summary", help="recovery snapshot vs baseline")
    s.add_argument("--days", type=int, default=28, help="baseline window")
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("run", help="running form metrics for a day")
    s.add_argument("--date", help="YYYY-MM-DD; default = latest running day")
    s.add_argument("--days", type=int, default=60, help="comparison window")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("trend", help="one metric over time")
    s.add_argument("--metric", required=True, help="e.g. vo2_max, weight_body_mass")
    s.add_argument("--days", type=int, default=180, help="how far back to look")
    s.add_argument("--show", type=int, default=12, help="readings to print")
    s.set_defaults(func=cmd_trend)

    s = sub.add_parser("day", help="everything recorded on one day")
    s.add_argument("--date", help="YYYY-MM-DD; default = today")
    s.set_defaults(func=cmd_day)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
