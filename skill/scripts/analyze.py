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
  load      Fitness, fatigue and form (CTL/ATL/TSB) from Strava relative effort
  streams   One activity's streams: decoupling, efficiency, time in zones
  runlog    The per-run log those feed, or the runs missing from it
  efficiency  Easy-run efficiency over the weeks, long-run decoupling
  zonedist  Weekly time by intensity: low / mid / high
  cs        Critical speed and D' from best efforts

Charts of all of this are in charts.py, which needs matplotlib and runs
through uv:  uv run --script <skill>/scripts/charts.py --help

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


def read_json(arg):
    """A file path, '-' for stdin, or the JSON itself."""
    if arg == "-":
        raw = sys.stdin.read()
    elif arg.lstrip()[:1] in "[{":
        raw = arg
    else:
        with open(arg) as fh:
            raw = fh.read()
    return json.loads(raw)


def load_activities(arg):
    """Accept a file path, '-' for stdin, or a raw JSON string.

    The MCP tool hands back JSON in a couple of shapes depending on how it's
    called, so unwrap the common envelopes rather than making the caller
    reshape it by hand.
    """
    data = read_json(arg)
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
# training load: fitness, fatigue and form
# --------------------------------------------------------------------------
#
# The classic performance-management model: each day's training load feeds
# two exponentially weighted averages, a slow one (fitness, 42 days) and a
# fast one (fatigue, 7 days). Form is the gap between them.
#
# The load per activity is Strava's relative_effort - its own HR-based
# score, present in list_activities, so a whole season costs one call rather
# than one stream per activity. Activities recorded without HR have none and
# count as zero; the output says how many. Commutes never count.
#
# Absolute numbers are on Strava's scale, not TSS, so thresholds are judged
# on form as a percentage of fitness, which doesn't care about the scale.
#
# Load is remembered in athlete/state/load_log.json, keyed by activity id
# (--save), so after one 90-day seed each close-out only has to add its week.

CTL_DAYS = 42
ATL_DAYS = 7
# Form as % of fitness, the bands intervals.icu popularised.
FORM_BANDS = [
    (-30, "high risk: fatigue well beyond what fitness absorbs"),
    (-10, "productive: carrying training load"),
    (5, "neutral"),
    (25, "fresh: race-ready, or a down week"),
    (None, "very fresh: fitness is starting to decay"),
]


def read_load_log():
    try:
        return json.loads(cc.LOAD_LOG.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def daily_load_series(store, until):
    """[(date, load, ctl, atl)] from the first stored day through `until`."""
    from collections import defaultdict
    from datetime import date, timedelta

    per_day = defaultdict(float)
    for rec in store.values():
        per_day[rec["date"]] += rec.get("load") or 0
    if not per_day:
        return []
    day = date.fromisoformat(min(per_day))
    ctl = atl = 0.0
    out = []
    while day <= until:
        load = per_day.get(day.isoformat(), 0.0)
        ctl += (load - ctl) / CTL_DAYS
        atl += (load - atl) / ATL_DAYS
        out.append((day, load, ctl, atl))
        day += timedelta(days=1)
    return out


def form_band(pct):
    for limit, label in FORM_BANDS:
        if limit is None or pct < limit:
            return label


def cmd_load(args):
    from datetime import date

    store = read_load_log()
    added = 0
    if args.activities:
        for a in load_activities(args.activities):
            sport = field(a, "sport_type", "type", default="")
            day = str(field(a, "start_local", "start_date_local", default=""))[:10]
            if not day or sport in COMMUTE_TYPES or a.get("id") is None:
                continue
            effort = field(a, "relative_effort", "suffer_score")
            store[str(a["id"])] = {"date": day, "sport": sport,
                                   "load": effort if effort is not None else None}
            added += 1
    if args.save and added and cc.write_allowed("load log"):
        tmp = cc.LOAD_LOG.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=0, sort_keys=True))
        tmp.replace(cc.LOAD_LOG)
    until = date.fromisoformat(args.until) if args.until else date.today()
    series = daily_load_series(store, until)
    if not series:
        sys.exit("no training load yet - pass --activities (90+ days the first time) "
                 "and --save to remember them")

    day, _, ctl, atl = series[-1]
    tsb = ctl - atl
    form_pct = tsb / ctl * 100 if ctl else 0.0
    week_ago = series[-8] if len(series) >= 8 else series[0]
    ramp = ctl - week_ago[2]
    span = len(series)
    # Bands describe a state, not a day: one hard day always dips form, and
    # against a low fitness it dips a long way. Count how long it has held.
    band = form_band(form_pct)
    streak = 0
    for _, _, c, a in reversed(series):
        if not c or form_band((c - a) / c * 100) != band:
            break
        streak += 1
    no_hr = sum(1 for r in store.values() if r.get("load") is None)
    last_activity = max(r["date"] for r in store.values())

    payload = {
        "as_of": day.isoformat(),
        "fitness_ctl": round(ctl, 1),
        "fatigue_atl": round(atl, 1),
        "form_tsb": round(tsb, 1),
        "form_pct": round(form_pct),
        "form_band": None if span < CTL_DAYS else band,
        "form_band_days": streak,
        "ramp_7d": round(ramp, 1),
        "ramp_7d_pct": round(ramp / week_ago[2] * 100) if week_ago[2] else None,
        "history_days": span,
        "seeding": span < CTL_DAYS,
        "activities_without_effort": no_hr,
        "last_activity": last_activity,
        "added": added,
        "series": [{"date": d.isoformat(), "load": round(l), "ctl": round(c, 1),
                    "atl": round(a, 1), "tsb": round(c - a, 1)}
                   for d, l, c, a in series[-args.days:]],
    }
    lines = [
        f"Training load after {day.isoformat()} (Strava relative effort, all sports but commutes)",
        f"  fitness (42 d)  {ctl:6.1f}   7 days ago {week_ago[2]:.1f} ({ramp:+.1f})",
        f"  fatigue  (7 d)  {atl:6.1f}",
        f"  form            {tsb:+6.1f}   = {form_pct:+.0f}% of fitness: "
        + ("not judged until the history is long enough" if span < CTL_DAYS
           else f"{band} ({streak} day{'s' if streak != 1 else ''})"),
        "",
        "Last 7 days: " + " · ".join(f"{d.strftime('%a')} {l:.0f}" for d, l, _, _ in series[-7:]),
    ]
    if span < CTL_DAYS:
        lines.append(f"Only {span} days of history: fitness is understated until about "
                     f"{CTL_DAYS} days are loaded - pass 90+ days of activities once with --save.")
    if span >= CTL_DAYS and streak <= 2 and form_pct < -10:
        lines.append("A dip of a day or two is what any hard day does to form - "
                     "it matters when it lasts.")
    if no_hr:
        lines.append(f"{no_hr} activities had no relative effort (no HR) and count as 0.")
    lines.append(f"Latest activity in the history: {last_activity}"
                 + ("" if not args.activities else f" ({added} read, "
                    + ("saved" if args.save and not cc.DRY_RUN else "not saved") + ")"))
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# one activity's streams: decoupling, efficiency, time in zones
# --------------------------------------------------------------------------
#
# Input is get_activity_streams output. Ask for time, heart_rate,
# velocity_smooth, moving (+ watts when the run has power, + distance and
# altitude for the hill check and charts) at resolution ~300: that is a
# sample every 10-30 s, plenty for halves and zones, and small enough to pass
# through a file.
#
# Aerobic decoupling compares the efficiency of the first half of the steady
# part of a run with the second. Efficiency is output per heartbeat: speed
# (m/min) per beat, or watts per beat when power exists - preferred, because
# power doesn't care about hills. Below 5% the athlete is aerobically solid at
# that effort; above 10% the effort outran their aerobic base (or heat,
# dehydration, fuelling).

DECOUPLE_MIN_S = 30 * 60   # steady time needed after the warm-up
EASY_RUN_MIN_S = 20 * 60   # shorter runs are too noisy for the efficiency trend
STREAM_ALIASES = {"heartrate": "heart_rate", "velocity": "velocity_smooth"}


def parse_streams(data):
    """{name: list} from the MCP shape, the raw API's list or keyed shape."""
    if isinstance(data, dict) and isinstance(data.get("streams"), (dict, list)):
        data = data["streams"]
    out = {}
    if isinstance(data, list):
        for s in data:
            if isinstance(s, dict) and "type" in s:
                out[s["type"]] = s.get("data", [])
    elif isinstance(data, dict):
        for k, v in data.items():
            out[k] = v.get("data", []) if isinstance(v, dict) else v
    return {STREAM_ALIASES.get(k, k): v for k, v in out.items() if isinstance(v, list)}


def stream_samples(st):
    """One dict per sample, weighted by the seconds since the previous one."""
    t = st.get("time")
    if not t:
        sys.exit("streams need a time stream")
    n = len(t)
    for k, v in st.items():
        if len(v) != n:
            sys.exit(f"stream {k} has {len(v)} samples, time has {n}")

    def at(name, i):
        s = st.get(name)
        return s[i] if s else None

    out = []
    for i in range(1, n):
        dt = t[i] - t[i - 1]
        if dt <= 0:
            continue
        v = at("velocity_smooth", i)
        moving = at("moving", i)
        if moving is None:
            moving = v is None or v > 0.5
        out.append({"dt": dt, "moving": bool(moving), "hr": at("heart_rate", i),
                    "v": v, "w": at("watts", i), "alt": at("altitude", i),
                    "d": at("distance", i)})
    return out


def _wavg(samples, key):
    tot = sum(s["dt"] for s in samples if s[key] is not None)
    if not tot:
        return None
    return sum(s[key] * s["dt"] for s in samples if s[key] is not None) / tot


def _gain(samples):
    alts = [s["alt"] for s in samples if s["alt"] is not None]
    return sum(max(0.0, b - a) for a, b in zip(alts, alts[1:]))


def _km(samples):
    ds = [s["d"] for s in samples if s["d"] is not None]
    if len(ds) >= 2:
        return (ds[-1] - ds[0]) / 1000.0
    return sum((s["v"] or 0) * s["dt"] for s in samples) / 1000.0


def hr_zones():
    """[(label, lo, hi)] - configured, else derived from HRmax, else []."""
    own = configured_zones()
    if own:
        return own
    hrmax = cc.get("physiology.hrmax")
    if not hrmax:
        return []
    return [(label, round(lo * hrmax) if lo else None, round(hi * hrmax) if hi else None)
            for label, lo, hi in PCT_ZONES]


def zone_index(zones, hr):
    rh = round(hr)
    for i, (_, _, hi) in enumerate(zones):
        if hi is None or rh <= hi:
            return i
    return len(zones) - 1


def intensity(hr, ceiling, threshold, zones):
    """low / mid / high: the three-zone view the polarisation check uses."""
    if hr <= ceiling:
        return "low"
    if threshold:
        return "high" if hr >= threshold else "mid"
    return "high" if zones and zone_index(zones, hr) >= 3 else "mid"


def decoupling(samples, skip_s):
    """First half against second half of the moving time after the warm-up."""
    steady, skipped = [], 0
    for s in samples:
        if not s["moving"] or not s["hr"] or s["hr"] <= 0:
            continue
        if skipped < skip_s:
            skipped += s["dt"]
            continue
        steady.append(s)
    total = sum(s["dt"] for s in steady)
    if total < DECOUPLE_MIN_S:
        return {"steady_s": total, "reason": f"only {fmt_time(total)} steady after the "
                f"warm-up; decoupling needs {DECOUPLE_MIN_S // 60}+ min"}
    acc, cut = 0, len(steady)
    for i, s in enumerate(steady):
        acc += s["dt"]
        if acc >= total / 2:
            cut = i + 1
            break
    halves = [steady[:cut], steady[cut:]]
    has_power = all(any((s["w"] or 0) > 0 for s in h) for h in halves)
    basis = "power" if has_power else "pace"
    res = {"steady_s": total, "basis": basis, "halves": []}
    for h in halves:
        hr = _wavg(h, "hr")
        km = _km(h)
        half = {"avg_hr": round(hr, 1), "km": round(km, 2),
                "gain_m": round(_gain(h)), "net_m": None}
        alts = [s["alt"] for s in h if s["alt"] is not None]
        if alts:
            half["net_m"] = round(alts[-1] - alts[0])
        if basis == "power":
            w = _wavg([s for s in h if (s["w"] or 0) > 0], "w")
            half["avg_watts"] = round(w, 1)
            half["ef"] = w / hr
        else:
            v = _wavg(h, "v")
            if v is None:
                return {"steady_s": total, "reason": "no velocity_smooth or watts stream"}
            half["pace"] = fmt_pace(1000 / v) if v else "-"
            half["ef"] = v * 60 / hr
        res["halves"].append(half)
    e1, e2 = res["halves"][0]["ef"], res["halves"][1]["ef"]
    res["pct"] = round((e1 - e2) / e1 * 100, 1)
    for h in res["halves"]:
        h["ef"] = round(h["ef"], 3)
    if basis == "pace":
        # Pace-based decoupling is only fair if both halves climb alike.
        per_km = [h["gain_m"] / h["km"] if h["km"] else 0 for h in res["halves"]]
        nets = [(h["net_m"] or 0) / h["km"] if h["km"] else 0 for h in res["halves"]]
        if abs(per_km[0] - per_km[1]) > 5 or abs(nets[0] - nets[1]) > 5:
            res["terrain_warning"] = (
                f"halves differ in terrain ({per_km[0]:.0f} vs {per_km[1]:.0f} m/km climbing, "
                f"net {nets[0]:+.0f} vs {nets[1]:+.0f} m/km) - pace decoupling is unreliable here")
    return res


def decoupling_verdict(pct):
    if pct < -5:
        return "negative: HR fell against output - easier second half, downhill, or an HR artifact"
    if pct < 5:
        return "coupled: aerobically solid at this effort"
    if pct <= 10:
        return "some drift: the effort is near the edge of the aerobic base, or heat/fuelling"
    return "decoupled: the effort outran the aerobic base (or heat, dehydration, no fuel)"


def analyse_streams(st, ceiling, skip_s):
    samples = stream_samples(st)
    moving = [s for s in samples if s["moving"]]
    with_hr = [s for s in moving if s["hr"] and s["hr"] > 0]
    moving_s = sum(s["dt"] for s in moving)
    res = {"moving_s": moving_s, "km": round(_km(moving), 2)}
    if not with_hr:
        res["note"] = "no heart rate in these streams"
        return res
    hr = _wavg(with_hr, "hr")
    v = _wavg(moving, "v")
    res["avg_hr"] = round(hr, 1)
    if v:
        res["pace"] = fmt_pace(1000 / v)
        res["ef"] = round(v * 60 / hr, 3)
    powered = [s for s in with_hr if (s["w"] or 0) > 0]
    if powered:
        res["avg_watts"] = round(_wavg(powered, "w"), 1)
        res["wpb"] = round(res["avg_watts"] / _wavg(powered, "hr"), 3)
    if res["km"]:
        res["gain_m_per_km"] = round(_gain(moving) / res["km"], 1)

    hr_s = sum(s["dt"] for s in with_hr)
    if ceiling:
        above = sum(s["dt"] for s in with_hr if s["hr"] > ceiling)
        res["pct_above_ceiling"] = round(above / hr_s * 100, 1)
    zones = hr_zones()
    if zones:
        zs = [0] * len(zones)
        for s in with_hr:
            zs[zone_index(zones, s["hr"])] += s["dt"]
        res["zone_labels"] = [z[0] for z in zones]
        res["zone_s"] = zs
    if ceiling:
        threshold = cc.get("physiology.threshold_hr")
        buckets = {"low": 0, "mid": 0, "high": 0}
        for s in with_hr:
            buckets[intensity(s["hr"], ceiling, threshold, zones)] += s["dt"]
        res["intensity_s"] = buckets
    res["decoupling"] = decoupling(samples, skip_s)
    return res


def read_run_log():
    if not cc.RUN_LOG.exists():
        return []
    rows = []
    for line in cc.RUN_LOG.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def cmd_streams(args):
    ceiling = args.ceiling or cc.easy_ceiling()
    res = analyse_streams(parse_streams(read_json(args.streams)), ceiling, args.skip_warmup)
    lines = [f"{res['km']:.2f} km, {fmt_time(res['moving_s'])} moving"]
    if "avg_hr" in res:
        bits = [f"avg HR {res['avg_hr']:g}"]
        if "pace" in res:
            bits.append(f"{res['pace']}/km")
            bits.append(f"efficiency {res['ef']:.2f} m/min per beat")
        if "wpb" in res:
            bits.append(f"{res['avg_watts']:g} W, {res['wpb']:.2f} W/beat")
        lines.append("  " + ", ".join(bits))
        if "gain_m_per_km" in res:
            lines.append(f"  climbing {res['gain_m_per_km']:g} m/km")
        if "pct_above_ceiling" in res:
            lines.append(f"  {res['pct_above_ceiling']:g}% of the time above the "
                         f"{ceiling:g} ceiling")
        if "zone_s" in res:
            tot = sum(res["zone_s"]) or 1
            lines.append("  zones: " + " · ".join(
                f"{lab.split()[0]} {s / tot * 100:.0f}%"
                for lab, s in zip(res["zone_labels"], res["zone_s"])))
        d = res["decoupling"]
        if "pct" in d:
            h1, h2 = d["halves"]
            out1 = f"{h1['avg_watts']:g} W" if d["basis"] == "power" else f"{h1['pace']}/km"
            out2 = f"{h2['avg_watts']:g} W" if d["basis"] == "power" else f"{h2['pace']}/km"
            lines += [
                f"Decoupling ({d['basis']}:HR, after a {args.skip_warmup // 60}-min warm-up): "
                f"{d['pct']:+.1f}%",
                f"  1st half {out1} at {h1['avg_hr']:g} bpm, 2nd half {out2} at {h2['avg_hr']:g} bpm",
                f"  {decoupling_verdict(d['pct'])}",
            ]
            if "terrain_warning" in d:
                lines.append(f"  WARNING: {d['terrain_warning']}")
        else:
            lines.append(f"Decoupling: {d['reason']}")
    else:
        lines.append(res["note"])

    if args.append:
        if not (args.id and args.date):
            sys.exit("--append needs --id and --date (the activity's start_local)")
        from datetime import date

        try:
            date.fromisoformat(args.date[:10])
        except ValueError:
            sys.exit(f"--date {args.date!r} is not a date - pass the activity's start_local")
        if any(str(r.get("id")) == str(args.id) for r in read_run_log()):
            lines.append(f"activity {args.id} is already in the run log - not appended")
        else:
            d = res.get("decoupling", {})
            entry = {"id": str(args.id), "date": args.date[:10], "sport": args.sport,
                     **{k: res[k] for k in ("km", "moving_s", "avg_hr", "pace", "ef",
                                            "avg_watts", "wpb", "gain_m_per_km",
                                            "pct_above_ceiling", "zone_labels", "zone_s",
                                            "intensity_s") if k in res}}
            if "pct" in d:
                entry["decoupling_pct"] = d["pct"]
                entry["decoupling_basis"] = d["basis"]
            if cc.write_allowed("run log entry"):
                with cc.RUN_LOG.open("a") as fh:
                    fh.write(json.dumps(entry) + "\n")
                lines.append("appended to the run log")
    emit(res, lines, args.json)


def cmd_runlog(args):
    rows = read_run_log()
    if args.missing:
        logged = {str(r.get("id")) for r in rows}
        missing = [a for a in load_activities(args.missing)
                   if field(a, "sport_type", "type", default="") in RUN_TYPES
                   and str(a.get("id")) not in logged]
        payload = {"missing": [{"id": str(a.get("id")),
                                "date": str(field(a, "start_local", "start_date_local",
                                                  default=""))[:10],
                                "name": a.get("name", "")} for a in missing]}
        lines = ([f"{m['id']}  {m['date']}  {m['name']}" for m in payload["missing"]]
                 or ["every run is in the run log"])
        emit(payload, lines, args.json)
        return
    rows = rows[-args.last:]
    if not rows:
        sys.exit("run log is empty - `analyze.py streams --append` adds runs")
    lines = [f"{'date':<11}{'km':>6}{'HR':>6}{'EF':>6}{'W/b':>6}{'>ceil':>7}{'decoup':>8}"]
    for r in rows:
        def f(k, fmt):
            return fmt.format(r[k]) if r.get(k) is not None else "-"
        lines.append(f"{r.get('date', '?'):<11}{f('km', '{:.1f}'):>6}{f('avg_hr', '{:.0f}'):>6}"
                     f"{f('ef', '{:.2f}'):>6}{f('wpb', '{:.2f}'):>6}"
                     f"{f('pct_above_ceiling', '{:.0f}%'):>7}{f('decoupling_pct', '{:+.1f}%'):>8}")
    emit({"runs": rows}, lines, args.json)


# --------------------------------------------------------------------------
# trends from the run log: easy-run efficiency, intensity distribution
# --------------------------------------------------------------------------


def _monday(day):
    from datetime import date, timedelta

    d = date.fromisoformat(day[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def _median(xs):
    import statistics

    return statistics.median(xs) if xs else None


def is_easy(r):
    return (r.get("pct_above_ceiling") is not None
            and r["pct_above_ceiling"] <= CEILING_TIME_TOLERANCE_PCT
            and (r.get("moving_s") or 0) >= EASY_RUN_MIN_S)


def efficiency_weeks(rows, weeks):
    """[(monday, {n, ef, wpb, gain})] for easy runs, the last `weeks` weeks."""
    from collections import defaultdict

    by = defaultdict(list)
    for r in rows:
        if r.get("sport", "Run") in RUN_TYPES and is_easy(r):
            by[_monday(r["date"])].append(r)
    out = []
    for monday in sorted(by)[-weeks:]:
        rs = by[monday]
        out.append((monday, {
            "n": len(rs),
            "ef": _median([r["ef"] for r in rs if r.get("ef")]),
            "wpb": _median([r["wpb"] for r in rs if r.get("wpb")]),
            "gain": _median([r["gain_m_per_km"] for r in rs if r.get("gain_m_per_km") is not None]),
            "hr": _median([r["avg_hr"] for r in rs if r.get("avg_hr")]),
        }))
    return out


def cmd_efficiency(args):
    rows = read_run_log()
    weeks = efficiency_weeks(rows, args.weeks)
    if not weeks:
        sys.exit("no easy runs in the run log yet - `analyze.py streams --append` adds runs")
    ceiling = cc.easy_ceiling()
    lines = [f"Easy-run efficiency (runs >= {EASY_RUN_MIN_S // 60} min, <= "
             f"{CEILING_TIME_TOLERANCE_PCT}% above the ceiling), weekly medians", "",
             f"{'week of':<12}{'runs':>5}{'HR':>6}{'EF':>7}{'≈pace@ceil':>12}{'W/beat':>8}{'m/km':>6}"]
    for monday, w in weeks:
        at_ceiling = fmt_pace(60000 / (w["ef"] * ceiling)) if w["ef"] and ceiling else "-"
        wpb = f"{w['wpb']:.2f}" if w["wpb"] else "-"
        gain = f"{w['gain']:.0f}" if w["gain"] is not None else "-"
        lines.append(f"{monday:<12}{w['n']:>5}{w['hr'] or 0:>6.0f}"
                     f"{(w['ef'] or 0):>7.2f}{at_ceiling:>12}{wpb:>8}{gain:>6}")
    easy = [r for r in rows if r.get("sport", "Run") in RUN_TYPES and is_easy(r) and r.get("ef")]
    payload = {"weeks": [{"week_of": m, **w} for m, w in weeks]}
    if len(easy) >= 6:
        k = min(len(easy) // 2, 8)
        old, new = easy[-2 * k:-k], easy[-k:]
        a, b = _median([r["ef"] for r in old]), _median([r["ef"] for r in new])
        payload["change_pct"] = round((b - a) / a * 100, 1)
        lines += ["", f"Last {k} easy runs against the {k} before: EF {a:.2f} -> {b:.2f} "
                      f"({(b - a) / a * 100:+.1f}%)"]
        if ceiling:
            lines.append(f"  ≈ pace at {ceiling:g} bpm: {fmt_pace(60000 / (a * ceiling))} -> "
                         f"{fmt_pace(60000 / (b * ceiling))}/km")
        lines.append("  Within ±2% is noise. Check the m/km column before crediting "
                     "fitness - flatter routes raise EF too.")
    longs = [r for r in rows if r.get("decoupling_pct") is not None
             and (r.get("moving_s") or 0) >= 60 * 60][-6:]
    if longs:
        payload["long_runs"] = longs
        lines += ["", "Runs of an hour or more, decoupling:"]
        lines += [f"  {r['date']}  {r.get('km', 0):.1f} km  {r['decoupling_pct']:+.1f}% "
                  f"({r.get('decoupling_basis', 'pace')})" for r in longs]
    lines += ["", "≈pace@ceil is EF scaled to the ceiling - a trend marker, not a "
                  "prescription: HR isn't proportional to speed."]
    emit(payload, lines, args.json)


def intensity_weeks(rows, weeks, all_sports=False):
    """[(monday, {low, mid, high, zone_s, zone_labels, runs})] in seconds."""
    from collections import defaultdict

    by = defaultdict(lambda: {"low": 0, "mid": 0, "high": 0, "zone_s": None,
                              "zone_labels": None, "n": 0})
    for r in rows:
        if not all_sports and r.get("sport", "Run") not in RUN_TYPES:
            continue
        if not r.get("intensity_s") and not r.get("zone_s"):
            continue
        w = by[_monday(r["date"])]
        w["n"] += 1
        for k in ("low", "mid", "high"):
            w[k] += (r.get("intensity_s") or {}).get(k, 0)
        if r.get("zone_s"):
            if w["zone_s"] is None or len(w["zone_s"]) != len(r["zone_s"]):
                w["zone_s"] = [0] * len(r["zone_s"])
                w["zone_labels"] = r.get("zone_labels")
            w["zone_s"] = [a + b for a, b in zip(w["zone_s"], r["zone_s"])]
    return [(m, by[m]) for m in sorted(by)[-weeks:]]


def cmd_zonedist(args):
    weeks = intensity_weeks(read_run_log(), args.weeks, args.all_sports)
    if not weeks:
        sys.exit("no runs with HR in the run log yet - `analyze.py streams --append` adds runs")
    what = "all logged sports" if args.all_sports else "runs"
    lines = [f"Time by intensity, {what} (low <= easy ceiling, high >= threshold)", "",
             f"{'week of':<12}{'runs':>5}{'hours':>7}{'low':>6}{'mid':>6}{'high':>6}"]
    tot = {"low": 0, "mid": 0, "high": 0}
    payload = {"weeks": []}
    for monday, w in weeks:
        s = w["low"] + w["mid"] + w["high"]
        for k in tot:
            tot[k] += w[k]
        pct = {k: round(w[k] / s * 100) if s else 0 for k in tot}
        payload["weeks"].append({"week_of": monday, "runs": w["n"], "hours": round(s / 3600, 1),
                                 "pct": pct, "zone_s": w["zone_s"],
                                 "zone_labels": w["zone_labels"]})
        lines.append(f"{monday:<12}{w['n']:>5}{s / 3600:>7.1f}"
                     f"{pct['low']:>5}%{pct['mid']:>5}%{pct['high']:>5}%")
    s = sum(tot.values())
    if s:
        pct = {k: round(v / s * 100) for k, v in tot.items()}
        payload["overall_pct"] = pct
        lines += ["", f"Overall: {pct['low']}% low, {pct['mid']}% mid, {pct['high']}% high."]
        if pct["mid"] > 20 and pct["mid"] > pct["high"]:
            lines.append("  A large middle: easy runs drifting up and hard ones not hard - "
                         "the grey zone that tires without building much.")
        elif pct["low"] >= 75:
            lines.append("  Mostly easy, as the base needs.")
    lines.append("By HR time: intervals' recoveries count as low, and HR lags short reps.")
    emit(payload, lines, args.json)


# --------------------------------------------------------------------------
# critical speed from best efforts
# --------------------------------------------------------------------------
#
# Distance covered at maximal effort is close to linear in time between
# roughly 2 and 25 minutes: d = CS * t + D'. CS is the fastest pace that is
# sustainable for a long while (close to threshold, ~30-40 min race pace);
# D' is the finite distance they can run above it. Fitted from best efforts
# at three or more distances - each of them a genuinely maximal effort at
# that distance. Splits inside one race are not (the fastest 1k of a 5k is a
# paced 1k), which is why one activity's efforts alone get a warning.

BEST_EFFORT_M = {
    "fastest400": 400, "fastesthalfmile": 804.672, "fastest1k": 1000,
    "fastestmile": 1609.344, "fastest2mile": 3218.688, "fastest5k": 5000,
    "fastest10k": 10000, "fastest15k": 15000, "fastest10mile": 16093.44,
    "fastest20k": 20000, "fastesthalfmarathon": 21097.5,
    "400m": 400, "1/2 mile": 804.672, "1k": 1000, "1 mile": 1609.344,
    "2 mile": 3218.688, "5k": 5000, "10k": 10000, "15k": 15000,
    "10 mile": 16093.44, "20k": 20000, "half-marathon": 21097.5,
}
CS_MIN_S, CS_MAX_S = 120, 25 * 60


def best_efforts(doc):
    """[(metres, seconds)] from get_activity_performance output (or a list)."""
    docs = doc if isinstance(doc, list) else [doc]
    out = []
    for d in docs:
        for e in (d.get("best_efforts") or []) if isinstance(d, dict) else []:
            key = str(e.get("type_value") or e.get("name") or "").lower()
            metres = BEST_EFFORT_M.get(key) or BEST_EFFORT_M.get(
                str(e.get("display_text", "")).lower())
            secs = e.get("value") or e.get("elapsed_time")
            if metres and secs:
                out.append((metres, float(secs)))
    return out, len(docs)


def cmd_cs(args):
    import statistics

    efforts, sources = [], 0
    if args.performance:
        efforts, sources = best_efforts(read_json(args.performance))
    for e in args.effort or []:
        try:
            m, t = e.split(":", 1)
            efforts.append((float(m), float(t)))
            sources += 1
        except ValueError:
            sys.exit(f"--effort takes METRES:SECONDS, got {e!r}")
    best = {}
    for m, t in efforts:
        best[m] = min(t, best.get(m, t))
    used = sorted((m, t) for m, t in best.items() if CS_MIN_S <= t <= CS_MAX_S)
    if len(used) < 3:
        sys.exit(f"need maximal efforts at 3+ distances lasting {CS_MIN_S // 60}-"
                 f"{CS_MAX_S // 60} min; have {len(used)}")
    ts = [t for _, t in used]
    ds = [m for m, _ in used]
    if max(ts) / min(ts) < 2.5:
        sys.exit("efforts span too narrow a range of durations - want the longest "
                 "at least 2.5x the shortest (e.g. 3 min to 20 min)")
    fit = statistics.linear_regression(ts, ds)
    cs, dprime = fit.slope, fit.intercept
    r2 = statistics.correlation(ts, ds) ** 2
    payload = {"cs_m_s": round(cs, 3), "cs_pace": fmt_pace(1000 / cs),
               "d_prime_m": round(dprime), "r2": round(r2, 4),
               "efforts": [{"m": m, "s": t, "pace": fmt_pace(t / m * 1000)} for m, t in used]}
    lines = ["Efforts used (best per distance):"]
    lines += [f"  {m / 1000:>7.3f} km  {fmt_time(t):>8}  {fmt_pace(t / m * 1000)}/km" for m, t in used]
    lines += ["", f"Critical speed {fmt_pace(1000 / cs)}/km ({cs:.2f} m/s), D' {dprime:.0f} m, "
                  f"R² {r2:.3f}"]
    vdot = cc.get("physiology.vdot")
    if vdot:
        thr = pace_at_intensity(float(vdot), 0.86)
        payload["vdot_threshold_pace"] = fmt_pace(thr)
        lines.append(f"  VDOT {vdot:g} threshold pace for comparison: {fmt_pace(thr)}/km "
                     "(CS usually sits a little faster)")
    preds = {}
    for label, dist in [("3 km", 3000), ("5 km", 5000), ("10 km", 10000)]:
        t = (dist - dprime) / cs
        if CS_MIN_S <= t <= CS_MAX_S * 1.6:
            preds[label] = fmt_time(t)
    if preds:
        payload["predictions"] = preds
        lines.append("  Model times: " + ", ".join(f"{k} {v}" for k, v in preds.items()))
    if r2 < 0.995:
        lines.append("  R² below 0.995: at least one effort was not maximal - treat with care.")
    if dprime < 0 or dprime > 600:
        lines.append(f"  D' of {dprime:.0f} m is implausible (typical 100-400 m): the "
                     "efforts are not all maximal.")
    if sources == 1:
        lines.append("  All efforts came from one activity: splits of a single race are "
                     "not maximal at each distance. Use bests from separate hard efforts.")
    emit(payload, lines, args.json)



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

    s = sub.add_parser("load", help="fitness, fatigue and form from relative effort")
    s.add_argument("--activities", help="list_activities JSON: a file path, '-' for stdin, "
                   "or the JSON itself; omit to read the saved history only")
    s.add_argument("--save", action="store_true",
                   help="remember these activities in athlete/state/load_log.json")
    s.add_argument("--until", help="last day to compute, YYYY-MM-DD (default today)")
    s.add_argument("--days", type=int, default=90, help="days of series in --json")
    s.set_defaults(func=cmd_load)

    s = sub.add_parser("streams", help="one activity's streams: decoupling, efficiency, zones")
    s.add_argument("--streams", required=True,
                   help="get_activity_streams JSON: a file path, '-' for stdin, or the JSON")
    s.add_argument("--ceiling", type=float, help="default: physiology.easy_ceiling")
    s.add_argument("--skip-warmup", type=int, default=600,
                   help="seconds of moving time left out of decoupling (default 600)")
    s.add_argument("--append", action="store_true",
                   help="record it in athlete/state/run_log.jsonl (needs --id and --date)")
    s.add_argument("--id", help="Strava activity id")
    s.add_argument("--date", help="the activity's start_local")
    s.add_argument("--sport", default="Run", help="sport_type (default Run)")
    s.set_defaults(func=cmd_streams)

    s = sub.add_parser("runlog", help="read the run log, or list runs missing from it")
    s.add_argument("--last", type=int, default=10)
    s.add_argument("--missing", metavar="ACTIVITIES",
                   help="list_activities JSON: print the runs not yet in the run log")
    s.set_defaults(func=cmd_runlog)

    s = sub.add_parser("efficiency", help="easy-run efficiency trend and long-run decoupling")
    s.add_argument("--weeks", type=int, default=12)
    s.set_defaults(func=cmd_efficiency)

    s = sub.add_parser("zonedist", help="weekly time by intensity (low / mid / high)")
    s.add_argument("--weeks", type=int, default=8)
    s.add_argument("--all-sports", action="store_true",
                   help="include logged non-run activities (zones are running zones)")
    s.set_defaults(func=cmd_zonedist)

    s = sub.add_parser("cs", help="critical speed and D' from best efforts")
    s.add_argument("--performance",
                   help="get_activity_performance JSON (one, or a list of several)")
    s.add_argument("--effort", action="append", metavar="METRES:SECONDS",
                   help="a maximal effort, repeatable, e.g. --effort 5000:1500")
    s.set_defaults(func=cmd_cs)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
