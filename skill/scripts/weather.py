#!/usr/bin/env python3
"""Weather for training decisions. Standard library only, no API key.

Open-Meteo is a keyless public API, so this is a plain HTTPS GET rather than
an MCP server - nothing to register, nothing loaded into every session, and it
behaves identically from cron and from a conversation.

Subcommands:
  forecast  Conditions at a specific session time
  window    Best slot in a day, by apparent temperature and rain
  history   What it was actually like during a past run

Every subcommand takes --json for machine-readable output.

What this is for, and what it is not for
----------------------------------------
Heat moves heart rate 5-10 bpm at a fixed effort, and easy running is
governed by an HR ceiling. So conditions change what a given session will
*feel* like and what pace the ceiling will allow - which is worth saying
before the athlete runs, and worth checking before blaming fatigue afterwards.

It is not a reason to skip a run. Consistency is what builds a runner;
weather adjusts timing and expectation, not whether the session happens.
The only genuine exceptions are safety: thunderstorms, ice, and the like.

The default location and timezone are `home` and `athlete.timezone` in
athlete/config.toml; `--place` looks anywhere else up by name.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta

import coachconf as cc

# Home, overridable with --place because athletes travel and the long run
# sometimes starts somewhere else entirely.
DEFAULT_LAT = cc.get("home.lat")
DEFAULT_LON = cc.get("home.lon")
DEFAULT_PLACE = cc.get("home.place") or ""
TIMEZONE = cc.get("athlete.timezone") or "auto"

HOURLY = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation_probability",
    "precipitation",
    "wind_speed_10m",
    "weather_code",
]

# Rules of thumb, not measurements. The bpm figure is the same 5-10 band the
# analysis playbook already uses for heat; the rest is ordinary endurance
# coaching guidance. Deliberately coarse - a false precision here would be
# worse than none, because it would get quoted back as if it were measured.
def heat_note(apparent, humidity):
    if apparent is None:
        return "no apparent-temperature reading"
    if apparent < 5:
        base = "cold: long warm-up, watch for ice underfoot"
    elif apparent < 12:
        base = "ideal running conditions"
    elif apparent < 18:
        base = "good conditions, no real HR cost"
    elif apparent < 24:
        base = "warm: expect a few bpm at the same effort"
    elif apparent < 28:
        base = "hot: expect 5-10 bpm at the same effort, so the easy ceiling will force a slower pace than usual - that is correct, not a bad day"
    else:
        base = "very hot: move the session earlier or later if the calendar allows; if it can't move, hold the ceiling and let pace go wherever it goes"
    if humidity is not None and humidity >= 70 and apparent >= 18:
        base += ". Humidity above 70% compounds this - sweat evaporates poorly, so cooling is worse than the temperature alone suggests"
    return base


WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}

UNSAFE_CODES = {95, 96, 99, 75, 86}


def fetch(url, params):
    query = urllib.parse.urlencode(params, doseq=True)
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=25) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:  # network, DNS, HTTP, malformed JSON
        sys.exit(f"weather lookup failed: {exc}")


def geocode(name):
    """(lat, lon, label, timezone) for a place name, via Open-Meteo's keyless
    geocoder. A forecast for home while the athlete runs somewhere else is
    worse than none."""
    data = fetch(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": name, "count": 1, "language": "en", "format": "json"},
    )
    hits = (data or {}).get("results") or []
    if not hits:
        sys.exit(f"could not find a place called '{name}' - pass --lat/--lon instead")
    h = hits[0]
    label = ", ".join(x for x in (h.get("name"), h.get("country")) if x)
    return h["latitude"], h["longitude"], label, h.get("timezone") or TIMEZONE


def get_hourly(lat, lon, start, end, past=False, tz=None):
    """Hourly data between two dates (inclusive).

    The forecast endpoint serves both future and up to 92 days of past data
    via past_days, which covers every run in a training block. The separate
    archive endpoint is only needed further back than that, and lags a few
    days behind the present, so it is the fallback rather than the default.
    """
    base = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(HOURLY),
        "timezone": tz or TIMEZONE,
    }
    if past:
        days_back = (_date.today() - start).days
        if days_back <= 92:
            base["past_days"] = min(max(days_back + 1, 1), 92)
            base["forecast_days"] = 1
            return fetch("https://api.open-meteo.com/v1/forecast", base)
        base["start_date"] = start.isoformat()
        base["end_date"] = end.isoformat()
        return fetch("https://archive-api.open-meteo.com/v1/archive", base)
    base["start_date"] = start.isoformat()
    base["end_date"] = end.isoformat()
    return fetch("https://api.open-meteo.com/v1/forecast", base)


def row_at(data, when):
    """Nearest hourly sample to a datetime, with its index."""
    times = data.get("hourly", {}).get("time") or []
    if not times:
        sys.exit("no hourly data returned for that date")
    target = when.strftime("%Y-%m-%dT%H:00")
    if target in times:
        idx = times.index(target)
    else:
        parsed = [datetime.fromisoformat(t) for t in times]
        idx = min(range(len(parsed)), key=lambda i: abs(parsed[i] - when))
    return idx, times[idx]


def sample(data, idx):
    h = data["hourly"]

    def val(key):
        seq = h.get(key)
        return seq[idx] if seq and idx < len(seq) else None

    code = val("weather_code")
    return {
        "time": h["time"][idx],
        "temp_c": val("temperature_2m"),
        "apparent_c": val("apparent_temperature"),
        "humidity_pct": val("relative_humidity_2m"),
        "rain_chance_pct": val("precipitation_probability"),
        "precip_mm": val("precipitation"),
        "wind_kmh": val("wind_speed_10m"),
        "conditions": WEATHER_CODES.get(code, f"code {code}"),
        "unsafe": code in UNSAFE_CODES,
    }


def describe(s, place):
    # The archive endpoint reports what actually fell, not a forecast
    # probability, so fall back to millimetres rather than printing "None%".
    if s["rain_chance_pct"] is not None:
        rain = f"{s['rain_chance_pct']}% rain"
    elif s["precip_mm"] is not None:
        rain = f"{s['precip_mm']} mm rain" if s["precip_mm"] else "no rain"
    else:
        rain = "rain unknown"
    line = (
        f"{place} {s['time'][:16].replace('T', ' ')} - "
        f"{s['temp_c']}C (feels {s['apparent_c']}C), {s['conditions']}, "
        f"{rain}, wind {s['wind_kmh']} km/h, humidity {s['humidity_pct']}%"
    )
    notes = [heat_note(s["apparent_c"], s["humidity_pct"])]
    if s["unsafe"]:
        notes.append(
            "SAFETY: this is one of the few conditions that justifies moving or "
            "dropping a session rather than just adjusting it"
        )
    return line, notes


def cmd_forecast(args):
    when = datetime.fromisoformat(args.at)
    past = when.date() < _date.today()
    data = get_hourly(args.lat, args.lon, when.date(), when.date(), past=past, tz=args.tz)
    idx, _ = row_at(data, when)
    s = sample(data, idx)
    line, notes = describe(s, args.place)
    emit(s | {"notes": notes}, [line, ""] + [f"  {n}" for n in notes], args.json)


def cmd_history(args):
    when = datetime.fromisoformat(args.at)
    if when.date() > _date.today():
        sys.exit("that time is in the future - use `forecast`")
    data = get_hourly(args.lat, args.lon, when.date(), when.date(), past=True, tz=args.tz)
    idx, _ = row_at(data, when)
    s = sample(data, idx)
    line, notes = describe(s, args.place)
    lines = [line, ""] + [f"  {n}" for n in notes]
    lines += [
        "",
        "Use this before attributing an elevated HR or a poor watts-per-beat",
        "reading to fatigue. If it was materially warmer than their usual runs,",
        "heat is the simpler explanation and should be said first.",
    ]
    emit(s | {"notes": notes}, lines, args.json)


def cmd_window(args):
    day = _date.fromisoformat(args.date)
    past = day < _date.today()
    data = get_hourly(args.lat, args.lon, day, day, past=past, tz=args.tz)
    lo_h, hi_h = (int(x.split(":")[0]) for x in args.between.split("-"))

    rows = []
    for i, t in enumerate(data["hourly"]["time"]):
        when = datetime.fromisoformat(t)
        if when.date() != day or not (lo_h <= when.hour <= hi_h):
            continue
        rows.append(sample(data, i))
    if not rows:
        sys.exit(f"no hourly data for {day} between {args.between}")

    def score(s):
        # Lower is better: distance from ~12C ideal, plus rain and wind
        # penalties. Rain matters more than a couple of degrees because it
        # changes whether the athlete goes out at all.
        app = s["apparent_c"] if s["apparent_c"] is not None else 15
        rain = s["rain_chance_pct"] or 0
        wind = s["wind_kmh"] or 0
        return abs(app - 12) + rain * 0.15 + max(wind - 25, 0) * 0.2 + (40 if s["unsafe"] else 0)

    ranked = sorted(rows, key=score)
    best = ranked[0]
    line, notes = describe(best, args.place)

    lines = [f"Best slot {args.between} on {day}:", "  " + line, ""]
    lines += [f"  {n}" for n in notes]
    lines += ["", "All slots:"]
    for s in rows:
        lines.append(
            f"  {s['time'][11:16]}  feels {s['apparent_c']:>5}C  "
            f"{s['rain_chance_pct']:>3}% rain  {s['wind_kmh']:>4} km/h  {s['conditions']}"
        )
    payload = {"best": best, "slots": rows, "notes": notes}
    emit(payload, lines, args.json)


def emit(payload, lines, as_json):
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("\n".join(lines))


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--lat", type=float, default=None)
    p.add_argument("--lon", type=float, default=None)
    p.add_argument(
        "--place", default=None,
        help="where the athlete will be, e.g. 'Stockholm' - looked up when "
        "--lat/--lon aren't given (default: home in athlete/config.toml). "
        "Times are that place's local time",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("forecast", help="conditions at a session time")
    s.add_argument("--at", required=True, help="ISO datetime, e.g. 2026-08-10T18:00")
    s.set_defaults(func=cmd_forecast)

    s = sub.add_parser("window", help="best slot in a day")
    s.add_argument("--date", required=True, help="YYYY-MM-DD")
    s.add_argument("--between", default="06:00-21:00", help="HH:MM-HH:MM")
    s.set_defaults(func=cmd_window)

    s = sub.add_parser("history", help="conditions during a past run")
    s.add_argument("--at", required=True, help="the activity's start_local")
    s.set_defaults(func=cmd_history)

    args = p.parse_args()
    args.tz = TIMEZONE
    if args.lat is None or args.lon is None:
        if args.place and args.place.lower() != DEFAULT_PLACE.lower():
            args.lat, args.lon, args.place, args.tz = geocode(args.place)
        elif DEFAULT_LAT and DEFAULT_LON:  # 0/0 is the template's "unset"
            args.lat, args.lon = float(DEFAULT_LAT), float(DEFAULT_LON)
            args.place = args.place or DEFAULT_PLACE
        elif DEFAULT_PLACE:
            args.lat, args.lon, args.place, args.tz = geocode(DEFAULT_PLACE)
        else:
            sys.exit("no home location - set home.place/lat/lon in "
                     "athlete/config.toml, or pass --place")
    args.place = args.place or f"{args.lat:.2f},{args.lon:.2f}"
    args.func(args)


if __name__ == "__main__":
    main()
