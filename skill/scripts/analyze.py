#!/usr/bin/env python3
"""Running-coach arithmetic. Standard library only, so it runs anywhere.

Subcommands:
  vdot      VDOT from a race result
  paces     Training paces from a VDOT
  riegel    Race-time prediction between distances
  zones     HR and pace zone boundaries
  decouple  Watts-per-beat, optionally against a reference run
  ceiling   Time above/below an easy-run HR ceiling from streams
  week      Weekly running volume rollup from list_activities JSON
  weeklog   Build one weekly_log.jsonl entry: planned vs actual, ceiling
  trend     Read the weekly log back as a progression
  block     Last training block against the previous one (monthly review)
  chart     Render the weekly log as a PNG (volume, hours by sport, easy runs)

Everything is standard library except `chart`, which needs matplotlib and
must run under the project venv:  <coach home>/venv/bin/python3

Personal numbers - HRmax, zones, easy ceiling, VDOT, power band, commute
types - come from athlete/config.toml (see coachconf.py), never from here.

Every subcommand takes --json for machine-readable output.
"""

import argparse
import json
import math
import os
import sys

import coachconf as cc

# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------


def fmt_pace(sec_per_km):
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}"


def fmt_time(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def emit(payload, lines, as_json):
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print("\n".join(lines))


# --------------------------------------------------------------------------
# Daniels VDOT
# --------------------------------------------------------------------------


def vdot_from_race(distance_m, time_s):
    """VDOT from a maximal race effort."""
    minutes = time_s / 60.0
    velocity = distance_m / minutes  # m/min
    vo2 = -4.60 + 0.182258 * velocity + 0.000104 * velocity ** 2
    pct = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * minutes)
        + 0.2989558 * math.exp(-0.1932605 * minutes)
    )
    return vo2 / pct


def velocity_at_vo2(vo2):
    """Invert the VO2 polynomial to get m/min."""
    a, b, c = 0.000104, 0.182258, -4.60 - vo2
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def pace_at_intensity(vdot, fraction):
    return 1000.0 / velocity_at_vo2(vdot * fraction) * 60.0


def race_time_at_vdot(vdot, distance_m):
    """Bisect for the time that yields this VDOT at this distance."""
    lo, hi = 60.0, 40000.0
    for _ in range(90):
        mid = (lo + hi) / 2
        if vdot_from_race(distance_m, mid) > vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# Intensity fractions of vVO2max used for prescription. The two easy values
# deliberately reuse the Z2 band fractions below so the "paces" output can't
# contradict the "zones" output — an inconsistency here would show up as the
# coach prescribing two different easy paces in the same conversation.
INTENSITIES = [
    ("easy (slow end)", 0.60),
    ("easy (fast end)", 0.735),
    ("marathon", 0.79),
    ("threshold", 0.86),
    ("interval", 0.975),
]

ZONE_BANDS = [
    ("Z1 recovery", None, 0.60),
    ("Z2 endurance", 0.60, 0.735),
    ("Z3 steady", 0.735, 0.815),
    ("Z4 threshold", 0.815, 0.895),
    ("Z5 VO2max", 0.895, None),
]


def cmd_vdot(args):
    v = vdot_from_race(args.distance, args.time)
    pace = args.time / (args.distance / 1000.0)
    payload = {
        "distance_m": args.distance,
        "time_s": args.time,
        "pace_per_km": fmt_pace(pace),
        "vdot": round(v, 1),
    }
    lines = [
        f"{args.distance/1000:.2f} km in {fmt_time(args.time)} ({fmt_pace(pace)}/km)",
        f"VDOT {v:.1f}",
        "",
        "Race equivalents:",
    ]
    for label, dist in [("5 km", 5000), ("10 km", 10000), ("Half", 21097), ("Marathon", 42195)]:
        t = race_time_at_vdot(v, dist)
        payload.setdefault("equivalents", {})[label] = fmt_time(t)
        lines.append(f"  {label:<9} {fmt_time(t):>9}  ({fmt_pace(t/(dist/1000.0))}/km)")
    emit(payload, lines, args.json)


def cmd_paces(args):
    payload = {"vdot": args.vdot, "paces": {}, "zones": {}}
    lines = [f"Training paces at VDOT {args.vdot:g}", ""]
    for label, frac in INTENSITIES:
        p = pace_at_intensity(args.vdot, frac)
        payload["paces"][label] = fmt_pace(p)
        lines.append(f"  {label:<18} {fmt_pace(p)}/km")
    lines += ["", "Pace zones:"]
    for label, lo, hi in ZONE_BANDS:
        if lo is None:
            band = f"slower than {fmt_pace(pace_at_intensity(args.vdot, hi))}/km"
        elif hi is None:
            band = f"faster than {fmt_pace(pace_at_intensity(args.vdot, lo))}/km"
        else:
            band = (
                f"{fmt_pace(pace_at_intensity(args.vdot, lo))} - "
                f"{fmt_pace(pace_at_intensity(args.vdot, hi))} /km"
            )
        payload["zones"][label] = band
        lines.append(f"  {label:<16} {band}")
    emit(payload, lines, args.json)


def cmd_riegel(args):
    t2 = args.time * (args.target / args.distance) ** 1.06
    payload = {
        "from": {"distance_m": args.distance, "time_s": args.time},
        "target_m": args.target,
        "predicted_s": round(t2),
        "predicted": fmt_time(t2),
        "predicted_pace_per_km": fmt_pace(t2 / (args.target / 1000.0)),
    }
    lines = [
        f"{args.distance/1000:.2f} km in {fmt_time(args.time)} "
        f"({fmt_pace(args.time/(args.distance/1000.0))}/km)",
        f"-> {args.target/1000:.2f} km in {fmt_time(t2)} "
        f"({fmt_pace(t2/(args.target/1000.0))}/km)",
        "",
        "Riegel assumes training is adequate for the target distance. For a",
        "marathon prediction from a 5-10 km result, treat it as a ceiling on",
        "speed potential rather than a realistic finish time.",
    ]
    emit(payload, lines, args.json)


# Zone boundaries as fractions of HRmax, used when the athlete has no zones of
# their own in athlete/config.toml. A coach that rounds hand-set zones off
# generic percentages produces boundaries a beat or two different from what
# their watch shows - exactly the quiet contradiction that makes a coach look
# unreliable - so configured zones always win.
PCT_ZONES = [
    ("Z1 recovery", None, 0.65),
    ("Z2 easy", 0.655, 0.76),
    ("Z3 steady", 0.765, 0.84),
    ("Z4 threshold", 0.845, 0.91),
    ("Z5 VO2max", 0.915, None),
]


def configured_zones():
    """[(label, lo, hi)] from athlete/config.toml, or [] if none set."""
    out = []
    for z in cc.get("physiology.zones", []) or []:
        if isinstance(z, dict):
            out.append((z.get("name", "?"), z.get("min"), z.get("max")))
        elif isinstance(z, (list, tuple)) and len(z) == 3:
            out.append((z[0], z[1] or None, z[2] or None))
    return out


def cmd_zones(args):
    hrmax = args.hrmax or cc.get("physiology.hrmax")
    if not hrmax:
        sys.exit("no HRmax - pass --hrmax or set physiology.hrmax in athlete/config.toml")
    hrmax = float(hrmax)
    payload = {"hrmax": hrmax, "zones": {}}
    own = configured_zones()
    own_hrmax = float(cc.get("physiology.hrmax") or 0)

    def band_str(lo, hi):
        if lo is None:
            return f"<= {hi}", f"<= {hi/hrmax*100:.0f}%"
        if hi is None:
            return f"{lo}+", f"{lo/hrmax*100:.0f}%+"
        return f"{lo}-{hi}", f"{lo/hrmax*100:.0f}-{hi/hrmax*100:.0f}%"

    if own and abs(hrmax - own_hrmax) < 0.5:
        lines = [f"HR zones for HRmax {hrmax:g} (the athlete's configured zones)", ""]
        rows = own
        payload["source"] = "configured"
    else:
        # No zones of their own, or a different HRmax means a remeasurement:
        # derive from percentages and say so, so nobody mistakes a derived
        # table for the configured one.
        rows = [(label, round(lo * hrmax) if lo else None, round(hi * hrmax) if hi else None)
                for label, lo, hi in PCT_ZONES]
        lines = [f"HR zones DERIVED for HRmax {hrmax:g} (% of max)", ""]
        payload["source"] = "derived"
    for label, lo, hi in rows:
        band, pct = band_str(lo, hi)
        payload["zones"][label] = band
        lines.append(f"  {label:<16} {band:>10} bpm   {pct}")
    ceiling = cc.easy_ceiling()
    if ceiling:
        lines += ["", f"  Easy ceiling {ceiling:g} is the number that matters day to day."]
    if payload["source"] == "derived" and own:
        lines += ["", "  Derived, not the configured zones. Update the watch/Strava",
                  "  and athlete/config.toml deliberately if adopting these."]

    threshold = args.threshold or cc.get("physiology.threshold_hr")
    if threshold:
        threshold = float(threshold)
        payload["threshold_hr"] = threshold
        payload["threshold_pct_max"] = round(threshold / hrmax * 100, 1)
        lines += ["", f"Threshold HR {threshold:g} = {threshold/hrmax*100:.0f}% of max"]
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# decoupling
# --------------------------------------------------------------------------

# The athlete's observed easy-run watts-per-beat band (physiology.wpb_band),
# filled in once a handful of easy runs at similar power exist.
WPB_BAND = tuple(cc.get("physiology.wpb_band", []) or ()) or None


def cmd_decouple(args):
    wpb = args.watts / args.hr
    payload = {"watts": args.watts, "hr": args.hr, "watts_per_beat": round(wpb, 3)}
    lines = [f"{args.watts:.1f} W at {args.hr:.1f} bpm  ->  {wpb:.2f} watts/beat"]

    if args.compare_watts and args.compare_hr:
        ref = args.compare_watts / args.compare_hr
        payload["reference"] = {
            "watts": args.compare_watts,
            "hr": args.compare_hr,
            "watts_per_beat": round(ref, 3),
        }
        payload["delta"] = round(wpb - ref, 3)
        payload["pct_change"] = round((wpb - ref) / ref * 100, 1)
        lines += [
            f"reference: {args.compare_watts:.1f} W at {args.compare_hr:.1f} bpm "
            f"-> {ref:.2f}",
            f"delta {wpb-ref:+.2f} ({(wpb-ref)/ref*100:+.1f}%)",
        ]
        if abs(args.watts - args.compare_watts) > 25:
            lines.append(
                "WARNING: power differs by more than 25 W. Watts-per-beat is not "
                "linear across intensities; this comparison is unreliable."
            )

    if not WPB_BAND:
        lines.append(
            "No personal easy-run band yet (physiology.wpb_band). Collect 5+ easy "
            "runs at similar power, then set the observed range."
        )
        emit(payload, lines, args.json)
        return
    lo, hi = WPB_BAND
    payload["baseline_band"] = [lo, hi]
    if wpb < lo:
        payload["verdict"] = "below band"
        lines.append(
            f"Below the usual {lo:.2f}-{hi:.2f} easy-run band: more heartbeats per watt "
            "than usual. Consistent with fatigue, heat, poor sleep, alcohol, "
            "dehydration or illness. Check confounders before attributing."
        )
    elif wpb > hi:
        payload["verdict"] = "above band"
        lines.append(f"Above the usual {lo:.2f}-{hi:.2f} band: efficient day, or fitness moving.")
    else:
        payload["verdict"] = "within band"
        lines.append(f"Within the usual {lo:.2f}-{hi:.2f} easy-run band.")
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# ceiling adherence
# --------------------------------------------------------------------------


def cmd_ceiling(args):
    hr = json.loads(args.hr) if isinstance(args.hr, str) else args.hr
    time = json.loads(args.time) if isinstance(args.time, str) else args.time
    if len(hr) != len(time):
        sys.exit("hr and time streams must be the same length")

    above = below = 0
    for i in range(1, len(hr)):
        dt = time[i] - time[i - 1]
        if hr[i] > args.ceiling:
            above += dt
        else:
            below += dt
    total = above + below or 1

    # Where were the excursions? Split into thirds.
    thirds = []
    n = len(hr)
    for k in range(3):
        seg = hr[k * n // 3 : (k + 1) * n // 3] or [0]
        thirds.append(round(sum(seg) / len(seg)))

    payload = {
        "ceiling": args.ceiling,
        "seconds_above": above,
        "seconds_below": below,
        "pct_above": round(above / total * 100, 1),
        "avg_hr": round(sum(hr[1:]) / max(len(hr) - 1, 1), 1),
        "max_hr": max(hr),
        "thirds_avg_hr": thirds,
    }
    lines = [
        f"Ceiling {args.ceiling} bpm",
        f"  above  {fmt_time(above)}  ({above/total*100:.0f}%)",
        f"  below  {fmt_time(below)}  ({below/total*100:.0f}%)",
        f"  average {payload['avg_hr']}, max {payload['max_hr']}",
        f"  by thirds: {thirds[0]} / {thirds[1]} / {thirds[2]} bpm",
    ]
    if thirds[0] > thirds[2] + 6:
        lines.append("  front-loaded: went out too hard, or climbed early.")
    elif thirds[2] > thirds[0] + 6:
        lines.append("  rising: cardiac drift, heat, dehydration or fatigue.")
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# weekly rollup
# --------------------------------------------------------------------------

RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
# Motor-assisted commuting (activities.commute_types): not training load, never
# counted or congratulated.
COMMUTE_TYPES = set(cc.get("activities.commute_types", []) or [])
# The rest of the athlete's sport, split by what it trains. Gym sessions once
# landed in other_aerobic_h, which is how two gym sessions became "aerobic
# volume".
AEROBIC_TYPES = {
    "Ride", "GravelRide", "MountainBikeRide", "VirtualRide", "Hike", "Swim",
    "NordicSki", "BackcountrySki", "RollerSki", "Rowing", "VirtualRow",
    "Elliptical", "StairStepper", "InlineSkate", "Snowshoe", "Kayaking",
}
STRENGTH_TYPES = {"WeightTraining", "Crossfit", "HighIntensityIntervalTraining"}
CLIMB_TYPES = {"RockClimbing"}
# An easy run counts as held when no more than this share of its moving time
# sat above the ceiling. Judged by time, not average: a run averaging 5 bpm
# under the ceiling can still spend half its time above it (a group run), and
# an average-based rating had to be hand-corrected twice before this existed.
CEILING_TIME_TOLERANCE_PCT = 20
WEEKLOG_NOTE_MAX = 300


def load_activities(arg):
    """Accept a file path, '-' for stdin, or a raw JSON string.

    The MCP tool hands back JSON in a couple of shapes depending on how it's
    called, so unwrap the common envelopes rather than making the caller
    reshape it by hand.
    """
    if arg == "-":
        raw = sys.stdin.read()
    elif arg.lstrip()[:1] in "[{":
        raw = arg
    else:
        with open(arg) as fh:
            raw = fh.read()
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("activities", "results", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data


def field(act, *names, default=None):
    """Read a field that may sit on the activity or inside its summary.

    Field names vary between the summary and detail shapes Strava returns;
    guessing wrong silently produces zeros, so try each candidate in both
    places before giving up.
    """
    summary = act.get("summary") if isinstance(act.get("summary"), dict) else {}
    for name in names:
        for src in (act, summary):
            if src.get(name) is not None:
                return src[name]
    return default


def week_bounds(week_of):
    from datetime import date, timedelta

    start = date.fromisoformat(week_of)
    start -= timedelta(days=start.weekday())  # snap to Monday
    return start, start + timedelta(days=7)


def cmd_week(args):
    acts = load_activities(args.activities)

    from collections import defaultdict
    from datetime import date, timedelta

    weeks = defaultdict(lambda: {"run_km": 0.0, "run_s": 0, "runs": 0, "other": 0})
    for a in acts:
        start = a.get("start_local", "")[:10]
        if not start:
            continue
        d = date.fromisoformat(start)
        monday = (d - timedelta(days=d.weekday())).isoformat()
        s = a.get("summary", a)
        if a.get("sport_type") in RUN_TYPES:
            weeks[monday]["run_km"] += s.get("distance", 0) / 1000.0
            weeks[monday]["run_s"] += s.get("moving_time", 0)
            weeks[monday]["runs"] += 1
        else:
            weeks[monday]["other"] += 1

    payload = {"weeks": []}
    lines = [f"{'week of':<12}{'km':>7}{'runs':>6}{'avg pace':>10}"]
    for monday in sorted(weeks):
        w = weeks[monday]
        pace = fmt_pace(w["run_s"] / w["run_km"]) if w["run_km"] else "-"
        payload["weeks"].append(
            {
                "week_of": monday,
                "run_km": round(w["run_km"], 1),
                "runs": w["runs"],
                "avg_pace": pace,
                "non_run_activities": w["other"],
            }
        )
        lines.append(f"{monday:<12}{w['run_km']:7.1f}{w['runs']:6d}{pace:>10}")

    if payload["weeks"]:
        last = payload["weeks"][-1]["run_km"]
        payload["max_next_week_km"] = round(last * 1.1, 1)
        lines += [
            "",
            f"Most recent week: {last:.1f} km",
            f"Ceiling for next week (+10%): {last*1.1:.1f} km",
        ]
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# weekly log: planned versus actual, and ceiling adherence over time
# --------------------------------------------------------------------------
#
# Volume, long run and load come from list_activities summaries, which are one
# cheap call for the whole week.
#
# Heart rate does NOT: list_activities returns no HR field whatsoever (checked
# against the real response - summary carries distance, times, elevation,
# relative_effort, calories, and nothing cardiac). So ceiling adherence can't
# be derived here, and this deliberately does not guess. Pass the week's easy-run
# average HRs in via --easy-hr; the coach already has them, either from the
# daily check-in that analysed each run or from get_activity_performance during
# the Sunday close-out. Omitting the flag records the week with adherence unknown,
# which is honest, rather than recording a confident zero.


def cmd_weeklog(args):
    acts = load_activities(args.activities)
    start, end = week_bounds(args.week_of)

    ceiling = args.ceiling or cc.easy_ceiling()
    if easy_hr_needed(args) and not ceiling:
        sys.exit("no easy ceiling - pass --ceiling or set physiology.easy_ceiling")
    runs, others, commutes = [], [], []
    for a in acts:
        day = str(field(a, "start_local", "start_date_local", default=""))[:10]
        if not day:
            continue
        from datetime import date as _date

        try:
            d = _date.fromisoformat(day)
        except ValueError:
            continue
        if not (start <= d < end):
            continue
        sport = field(a, "sport_type", "type", default="")
        if sport in RUN_TYPES:
            runs.append(a)
        elif sport in COMMUTE_TYPES:
            commutes.append(a)
        else:
            others.append(a)

    def km(a):
        return (field(a, "distance", default=0) or 0) / 1000.0

    def hours(a):
        return (field(a, "moving_time", "elapsed_time", default=0) or 0) / 3600.0

    def effort(a):
        return field(a, "relative_effort", default=0) or 0

    runs.sort(key=km, reverse=True)
    long_run = runs[0] if runs else None

    # Supplied by the caller - see the note above on why this can't come from
    # the activity list. --easy-above (percent of time over the ceiling, from
    # `analyze.py ceiling`) is the real rating; --easy-hr alone falls back to
    # averages and says so.
    easy_hr = [float(x) for x in args.easy_hr.split(",") if x.strip()] if args.easy_hr else []
    easy_above = ([float(x) for x in args.easy_above.split(",") if x.strip()]
                  if args.easy_above else [])
    if easy_above:
        rated_by = "time_above"
        rated = easy_above
        under = [p for p in easy_above if p <= CEILING_TIME_TOLERANCE_PCT]
    else:
        rated_by = "avg_hr" if easy_hr else None
        rated = easy_hr
        under = [h for h in easy_hr if h <= ceiling]

    note = " ".join((args.note or "").split())
    if len(note) > WEEKLOG_NOTE_MAX:
        sys.exit(
            f"note is {len(note)} chars, limit {WEEKLOG_NOTE_MAX}: one clause on "
            "what shaped the week, not the week's story"
        )

    def kind_h(types):
        return round(sum(hours(a) for a in others
                         if field(a, "sport_type", "type", default="") in types), 1)

    actual_km = round(sum(km(a) for a in runs), 1)
    entry = {
        "week": args.week,
        "week_of": start.isoformat(),
        "phase": args.phase,
        "target_km": args.target_km,
        "actual_km": actual_km,
        "target_lr_km": args.target_lr,
        "actual_lr_km": round(km(long_run), 1) if long_run else 0.0,
        "runs": len(runs),
        "easy_runs_rated": len(rated),
        "easy_runs_under_ceiling": len(under),
        "easy_rated_by": rated_by,
        "easy_avg_hr": [round(h) for h in easy_hr],
        "easy_pct_above": [round(p) for p in easy_above],
        "run_h": round(sum(hours(a) for a in runs), 1),
        "run_effort": round(sum(effort(a) for a in runs)),
        "other_aerobic_h": kind_h(AEROBIC_TYPES),
        "other_aerobic_effort": round(sum(
            effort(a) for a in others
            if field(a, "sport_type", "type", default="") in AEROBIC_TYPES)),
        "strength_h": kind_h(STRENGTH_TYPES),
        "strength_sessions": sum(
            1 for a in others
            if field(a, "sport_type", "type", default="") in STRENGTH_TYPES),
        "climb_h": kind_h(CLIMB_TYPES),
        "misc_h": round(sum(
            hours(a) for a in others
            if field(a, "sport_type", "type", default="")
            not in AEROBIC_TYPES | STRENGTH_TYPES | CLIMB_TYPES), 1),
        "emtb_h": round(sum(hours(a) for a in commutes), 1),
    }
    if args.target_km:
        entry["pct_of_target"] = round(actual_km / args.target_km * 100)
    if note:
        entry["note"] = note

    line = json.dumps(entry)
    if not args.append:
        # One line, ready to append to the weekly log.
        print(line)
        return
    # Append-only and nothing downstream dedupes it, so a second entry for the
    # same week would quietly skew every trend afterwards. Refuse instead.
    if cc.WEEKLY_LOG.exists():
        for old in cc.WEEKLY_LOG.read_text().splitlines():
            try:
                if json.loads(old).get("week_of") == entry["week_of"]:
                    sys.exit(f"week of {entry['week_of']} is already in the weekly log - "
                             "not appending a duplicate")
            except json.JSONDecodeError:
                continue
    print(line)
    if cc.write_allowed("weekly log entry"):
        with cc.WEEKLY_LOG.open("a") as fh:
            fh.write(line + "\n")
        print("appended")


def easy_hr_needed(args):
    return bool(args.easy_hr) and not args.easy_above


def cmd_trend(args):
    path = args.log
    try:
        with open(path) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        sys.exit(f"no weekly log at {path} - nothing logged yet")
    rows = rows[-args.last :]
    if not rows:
        sys.exit("weekly log is empty")

    payload = {"weeks": rows}
    lines = [f"{'week':<12}{'target':>8}{'actual':>8}{'%':>7}{'LR':>7}{'easy<=ceiling':>15}"]
    for r in rows:
        wk = f"W{r.get('week')}" if r.get("week") is not None else r.get("week_of", "?")
        rated = r.get("easy_runs_rated", 0)
        adher = f"{r.get('easy_runs_under_ceiling', 0)}/{rated}" if rated else "-"
        # A week with no target is a baseline week, not a missed one.
        pct = f"{r['pct_of_target']}%" if r.get("pct_of_target") is not None else "-"
        target = r.get("target_km") or "-"
        lines.append(
            f"{str(wk):<12}{str(target):>8}{r.get('actual_km', 0):>8}"
            f"{pct:>7}{r.get('actual_lr_km', 0):>7}{adher:>15}"
        )

    # Only weeks that actually had a target can be hit or missed.
    targeted = [r for r in rows if r.get("target_km")]
    hit = [r for r in targeted if (r.get("pct_of_target") or 0) >= 95]
    rated_total = sum(r.get("easy_runs_rated", 0) for r in rows)
    under_total = sum(r.get("easy_runs_under_ceiling", 0) for r in rows)
    last_actual = rows[-1].get("actual_km", 0)

    payload["summary"] = {
        "weeks_at_or_above_target": len(hit),
        "weeks_with_target": len(targeted),
        "weeks_logged": len(rows),
        "easy_runs_under_ceiling": under_total,
        "easy_runs_rated": rated_total,
        "max_next_week_km": round(last_actual * 1.1, 1),
    }
    lines.append("")
    if targeted:
        lines.append(f"Hit target in {len(hit)} of {len(targeted)} weeks with a target.")
    else:
        lines.append("No weeks with a target yet - these are baseline weeks.")
    lines.append(
        f"Easy runs under the ceiling: {under_total}/{rated_total}."
        if rated_total
        else "No easy runs with heart rate recorded."
    )
    lines.append(f"Ceiling for next week (+10% on last actual): {last_actual*1.1:.1f} km")
    if targeted and len(hit) == 0 and len(targeted) >= 2:
        lines.append(
            "Under target in every targeted week - hold volume flat rather than "
            "progressing. Repeating a week the athlete completes beats "
            "prescribing one they can't."
        )
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# block review: the last training block against the one before
# --------------------------------------------------------------------------
#
# The plan runs in four-week blocks ending in a down week (W4, W8, W12, ...).
# The Sunday close-out of a down week is the natural moment to step back:
# this prints the block's numbers next to the previous block's, so the
# monthly review message rests on arithmetic rather than recollection.


def _block_stats(rows):
    km = [r.get("actual_km", 0) or 0 for r in rows]
    targeted = [r for r in rows if r.get("target_km")]
    rated = sum(r.get("easy_runs_rated", 0) for r in rows)
    held = sum(r.get("easy_runs_under_ceiling", 0) for r in rows)
    return {
        "weeks": [r.get("week") for r in rows],
        "run_km_total": round(sum(km), 1),
        "run_km_per_week": round(sum(km) / len(rows), 1) if rows else 0,
        "biggest_week_km": max(km) if km else 0,
        "longest_run_km": max((r.get("actual_lr_km", 0) or 0) for r in rows) if rows else 0,
        "weeks_on_target": sum(1 for r in targeted if (r.get("pct_of_target") or 0) >= 95),
        "weeks_with_target": len(targeted),
        "runs": sum(r.get("runs", 0) or 0 for r in rows),
        "easy_held": held,
        "easy_rated": rated,
        "gym_sessions": sum(r.get("strength_sessions", 0) or 0 for r in rows),
        "endurance_h": round(sum(r.get("other_aerobic_h", 0) or 0 for r in rows), 1),
        "climb_h": round(sum(r.get("climb_h", 0) or 0 for r in rows), 1),
        "zero_run_weeks": sum(1 for k in km if not k),
    }


def cmd_block(args):
    try:
        with open(args.log) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        sys.exit(f"no weekly log at {args.log}")
    rows = [r for r in rows if r.get("week") is not None]
    if not rows:
        sys.exit("no plan weeks logged yet")
    # Blocks follow the plan's boundaries (W1-4, W5-8, ...), not "the last n
    # entries" - a review run mid-block compares the partial block so far.
    n = args.weeks
    blk = lambda r: (r["week"] - 1) // n
    last = blk(rows[-1])
    cur = [r for r in rows if blk(r) == last]
    prev = [r for r in rows if blk(r) == last - 1]
    a = _block_stats(cur)
    b = _block_stats(prev) if prev else None
    payload = {"block": a, "previous": b}

    def line(label, key, unit="", fmt="{}"):
        v = fmt.format(a[key])
        if b is not None:
            return f"  {label:<20}{v:>8}{unit}   (prev {fmt.format(b[key])}{unit})"
        return f"  {label:<20}{v:>8}{unit}"

    wk = f"W{cur[0]['week']}-W{cur[-1]['week']}"
    lines = [f"Block {wk}" + (f" vs W{prev[0]['week']}-W{prev[-1]['week']}" if prev else "")]
    lines += [
        line("running km", "run_km_total", " km"),
        line("per week", "run_km_per_week", " km"),
        line("biggest week", "biggest_week_km", " km"),
        line("longest run", "longest_run_km", " km"),
        line("runs", "runs"),
        f"  {'weeks on target':<20}{a['weeks_on_target']:>5}/{a['weeks_with_target']}",
        f"  {'easy runs held':<20}{a['easy_held']:>5}/{a['easy_rated']}",
        line("gym sessions", "gym_sessions"),
        line("other endurance", "endurance_h", " h"),
        line("climbing", "climb_h", " h"),
    ]
    if a["zero_run_weeks"]:
        lines.append(f"  {a['zero_run_weeks']} week(s) without running - read the notes for why")
    lines.append(f"Next block starts from {cur[-1].get('actual_km', 0)} km actual; +10% ceiling "
                 f"{(cur[-1].get('actual_km', 0) or 0) * 1.1:.1f} km.")
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# weekly chart
# --------------------------------------------------------------------------
#
# Sent to Telegram with the Sunday close-out. Three stacked panels on one x
# axis (never a dual-axis chart):
#   1. running km against target - the marathon's number
#   2. training hours by kind, stacked - the whole athlete: runs, rides and
#      hikes, gym, climbing.
#   3. easy-run discipline, deliberately the smallest panel - still tracked,
#      no longer the headline.
# Colors are the validated four-slot categorical order (blue, orange, aqua,
# yellow); running keeps slot 1 in both panels so it reads as one entity.
# Aqua and yellow sit under 3:1 contrast, which the legend and the total
# label on each bar relieve.

CHART_COLORS = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "grid": "#e8e8e4",
    "run": "#2a78d6",       # slot 1
    "strength": "#eb6834",  # slot 2
    "endurance": "#1baf7a", # slot 3
    "climb": "#eda100",     # slot 4
}

HOURS_SERIES = [  # (log field, label, color key) - bottom to top
    ("run_h", "running", "run"),
    ("other_aerobic_h", "ride / hike / other endurance", "endurance"),
    ("strength_h", "gym", "strength"),
    ("climb_h", "climbing", "climb"),
]


def cmd_chart(args):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch
    except ImportError:
        sys.exit(
            "chart needs matplotlib - run it under the venv:\n"
            f"  {cc.VENV_PY} {cc.SCRIPTS}/analyze.py chart"
        )

    try:
        with open(args.log) as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        sys.exit(f"no weekly log at {args.log} - nothing logged yet")
    rows = rows[-args.last :]
    if not rows:
        sys.exit("weekly log is empty")

    C = CHART_COLORS
    labels = [
        f"W{r['week']}" if r.get("week") is not None else r.get("week_of", "?")[5:]
        for r in rows
    ]
    xs = list(range(len(rows)))
    actual = [r.get("actual_km", 0) or 0 for r in rows]
    target = [r.get("target_km", 0) or 0 for r in rows]

    fig, (ax1, axh, ax2) = plt.subplots(
        3, 1, sharex=True, figsize=(8, 7.2), dpi=150,
        gridspec_kw={"height_ratios": [3, 2.6, 1.1], "hspace": 0.18},
    )
    fig.patch.set_facecolor(C["surface"])

    for ax in (ax1, axh, ax2):
        ax.set_facecolor(C["surface"])
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(C["grid"])
        ax.grid(axis="y", color=C["grid"], linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=C["ink2"], length=0)

    # -- 1. running volume: actual bars, target as a neutral tick --
    ax1.bar(xs, actual, width=0.62, color=C["run"], zorder=3)
    for x, km, t in zip(xs, actual, target):
        if km:
            y = max(km, t) if abs(km - t) < 2.5 else km
            ax1.annotate(
                f"{km:.0f}", (x, y), textcoords="offset points",
                xytext=(0, 4), ha="center", fontsize=8, color=C["ink"], zorder=4,
            )
    for x, t in zip(xs, target):
        if t:
            ax1.hlines(t, x - 0.38, x + 0.38, color=C["ink2"], linewidth=2, zorder=4)
    ax1.set_ylabel("running km", fontsize=9, color=C["ink2"])
    ax1.set_title("Training, week by week", loc="left", fontsize=12,
                  color=C["ink"], pad=12)
    handles = [Patch(facecolor=C["run"], label="ran")]
    if any(target):
        handles.append(Line2D([], [], color=C["ink2"], linewidth=2, label="target"))
    ax1.legend(handles=handles, loc="upper left", frameon=False, fontsize=8,
               labelcolor=C["ink2"], handlelength=1.4)

    # -- 2. hours by kind, stacked. Weeks logged before the split carry no
    #       per-kind fields and are left empty rather than guessed. --
    split = [any(f in r for f, _, _ in HOURS_SERIES) for r in rows]
    totals = [sum((r.get(f) or 0) for f, _, _ in HOURS_SERIES) if ok else 0
              for r, ok in zip(rows, split)]
    # One holiday week (24 h on a bike tour) would flatten every normal week, so
    # the axis caps at the ordinary range and an over-cap bar says its total.
    ordinary = sorted(t for t in totals if t)
    cap = max(8.0, (ordinary[-2] if len(ordinary) > 1 else ordinary[-1] if ordinary else 8) * 1.35)
    bottoms = [0.0] * len(rows)
    for f, lab, ck in HOURS_SERIES:
        vals = [(r.get(f) or 0) if ok else 0 for r, ok in zip(rows, split)]
        if not any(vals):
            continue
        shown = [max(0.0, min(v, cap - b)) for v, b in zip(vals, bottoms)]
        axh.bar(xs, shown, width=0.62, bottom=bottoms, color=C[ck],
                edgecolor=C["surface"], linewidth=1, zorder=3, label=lab)
        bottoms = [b + v for b, v in zip(bottoms, shown)]
    for x, t, ok in zip(xs, totals, split):
        if ok and t:
            over = t > cap
            axh.annotate(
                f"{t:.0f} h" + (" ↑" if over else ""), (x, min(t, cap)),
                textcoords="offset points", xytext=(0, 3), ha="center",
                fontsize=8, color=C["ink"], zorder=4,
            )
    axh.set_ylim(0, cap * 1.15)
    axh.set_ylabel("hours\nby sport", fontsize=9, color=C["ink2"])
    axh.legend(loc="upper left", frameon=False, fontsize=7.5, ncol=4,
               labelcolor=C["ink2"], handlelength=1.2, columnspacing=1.0,
               bbox_to_anchor=(0, 1.12))

    # -- 3. easy-run discipline, small and neutral --
    pts = [
        (x, r["easy_runs_under_ceiling"] / r["easy_runs_rated"] * 100,
         f"{r['easy_runs_under_ceiling']}/{r['easy_runs_rated']}")
        for x, r in zip(xs, rows)
        if r.get("easy_runs_rated")
    ]
    ax2.set_ylim(-15, 135)
    ax2.set_yticks([0, 100])
    ax2.set_yticklabels(["0%", "100%"], fontsize=7)
    ax2.set_ylabel("easy runs\nheld", fontsize=8, color=C["ink2"])
    if pts:
        ax2.plot([p[0] for p in pts], [p[1] for p in pts], color=C["ink2"],
                 linewidth=1.5, marker="o", markersize=5,
                 markeredgecolor=C["surface"], markeredgewidth=1.2, zorder=3)
        for x, y, lab in pts:
            ax2.annotate(lab, (x, y), textcoords="offset points", xytext=(0, 6),
                         ha="center", fontsize=7, color=C["ink2"], zorder=4)
    else:
        ax2.text(0.5, 0.5, "no easy runs rated yet", transform=ax2.transAxes,
                 ha="center", fontsize=8, color=C["ink2"])

    ax2.set_xticks(xs)
    ax2.set_xticklabels(labels, fontsize=8)
    ax2.set_xlim(-0.6, len(rows) - 0.4)

    fig.savefig(args.out, bbox_inches="tight", facecolor=C["surface"])
    print(args.out)


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("vdot", help="VDOT from a race result")
    s.add_argument("--distance", type=float, required=True, help="metres")
    s.add_argument("--time", type=float, required=True, help="seconds")
    s.set_defaults(func=cmd_vdot)

    s = sub.add_parser("paces", help="training paces from a VDOT")
    s.add_argument("--vdot", type=float, default=cc.get("physiology.vdot"),
                   required=not cc.get("physiology.vdot"))
    s.set_defaults(func=cmd_paces)

    s = sub.add_parser("riegel", help="predict a race time at another distance")
    s.add_argument("--distance", type=float, required=True, help="metres")
    s.add_argument("--time", type=float, required=True, help="seconds")
    s.add_argument("--target", type=float, default=42195.0, help="metres")
    s.set_defaults(func=cmd_riegel)

    s = sub.add_parser("zones", help="HR zone boundaries")
    s.add_argument("--hrmax", type=float, help="default: physiology.hrmax")
    s.add_argument("--threshold", type=float, help="default: physiology.threshold_hr")
    s.set_defaults(func=cmd_zones)

    s = sub.add_parser("decouple", help="watts per beat")
    s.add_argument("--watts", type=float, required=True)
    s.add_argument("--hr", type=float, required=True)
    s.add_argument("--compare-watts", type=float)
    s.add_argument("--compare-hr", type=float)
    s.set_defaults(func=cmd_decouple)

    s = sub.add_parser("ceiling", help="time above/below an easy ceiling")
    s.add_argument("--hr", required=True, help="JSON array of bpm")
    s.add_argument("--time", required=True, help="JSON array of elapsed seconds")
    s.add_argument("--ceiling", type=float, default=cc.easy_ceiling(),
                   required=not cc.easy_ceiling(),
                   help="default: physiology.easy_ceiling")
    s.set_defaults(func=cmd_ceiling)

    s = sub.add_parser("week", help="weekly running volume rollup")
    s.add_argument(
        "--activities",
        required=True,
        help="list_activities JSON: a file path, '-' for stdin, or the JSON itself",
    )
    s.set_defaults(func=cmd_week)

    s = sub.add_parser("weeklog", help="build one state/weekly_log.jsonl entry")
    s.add_argument(
        "--activities",
        required=True,
        help="list_activities JSON: a file path, '-' for stdin, or the JSON itself",
    )
    s.add_argument("--week", type=int, help="plan week number, omit for pre-plan weeks")
    s.add_argument("--week-of", required=True, help="any date in the week; snaps to Monday")
    s.add_argument("--target-km", type=float, default=0, help="planned weekly volume")
    s.add_argument("--target-lr", type=float, default=0, help="planned long run km")
    s.add_argument("--phase", default="", help="Base / Aerobic / Build / Taper")
    s.add_argument("--ceiling", type=float, help="default: physiology.easy_ceiling")
    s.add_argument(
        "--easy-hr",
        default="",
        help="comma-separated average HR for the week's easy runs; "
        "list_activities carries no HR, so this must be supplied",
    )
    s.add_argument(
        "--easy-above",
        default="",
        help="comma-separated percent of moving time above the ceiling for each "
        f"easy run (from `ceiling`); a run is held at <= {CEILING_TIME_TOLERANCE_PCT}%%. "
        "Preferred over --easy-hr",
    )
    s.add_argument("--note", default="",
                   help=f"one clause on what shaped the week, <= {WEEKLOG_NOTE_MAX} chars")
    s.add_argument("--append", action="store_true",
                   help="append to the weekly log (refuses a duplicate week)")
    s.set_defaults(func=cmd_weeklog)

    s = sub.add_parser("trend", help="read the weekly log back as a trend")
    s.add_argument(
        "--log",
        default=str(cc.WEEKLY_LOG),
    )
    s.add_argument("--last", type=int, default=8, help="how many weeks to show")
    s.set_defaults(func=cmd_trend)

    s = sub.add_parser("block", help="last training block vs the one before")
    s.add_argument(
        "--log",
        default=str(cc.WEEKLY_LOG),
    )
    s.add_argument("--weeks", type=int, default=4, help="block length")
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("chart", help="render the weekly log as a PNG")
    s.add_argument(
        "--log",
        default=str(cc.WEEKLY_LOG),
    )
    s.add_argument("--last", type=int, default=12, help="how many weeks to plot")
    s.add_argument(
        "--out",
        default=str(cc.STATE / "weekly_chart.png"),
        help="output PNG path (printed on success)",
    )
    s.set_defaults(func=cmd_chart)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
