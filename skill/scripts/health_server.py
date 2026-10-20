#!/usr/bin/env python3
"""Receives Apple Health data from the Health Auto Export iOS app.

The app POSTs JSON to http://<coach machine>:5556/health on its automation schedule
(shape: {"data": {"metrics": [...], "workouts": [...]}}). This stores each
metric's datapoints into one JSON file per calendar day:

    <coach home>/athlete/state/health/daily/YYYY-MM-DD.json
    {"metrics": {"sleep_analysis": [...], "resting_heart_rate": [...]}, ...}

Re-exports merge timestamp-keyed buckets; explicit daily aggregation replaces
that metric's day. Sleep keeps the fullest overlapping span. See
../../docs/health-ingestion.md for aggregation ambiguity and safe upgrades.
scripts/health.py reads the unchanged metric-list format; additive ingestion
metadata is ignored by readers. Workouts retain day-level replacement semantics.
Strava stays the source of truth for training.

Auth: requests must carry the HEALTH_API_KEY value (from the coach's .env)
in an "x-api-key" header (configure it under "Headers" in the app's API
Export settings). Health data is worth an auth header even on a home LAN.

Runs as a daemon via ensure_health_server.sh from cron. If the port is
already bound it exits 0 immediately - that is the watchdog no-op path.

Standard library only. Threaded HTTP supports proxy keep-alive; one lock
serializes validated read-modify-write batches. Loopback bind is the default;
HEALTH_BIND explicitly enables a LAN interface. No key means no startup.
"""

import hmac
import json
import math
import tempfile
from pathlib import Path
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import coachconf as cc

PORT = int(os.environ.get("HEALTH_PORT", "5556"))
DAILY_DIR = str(cc.HEALTH_DIR)
API_KEY = os.environ.get("HEALTH_API_KEY", "")
# A first-time export with months of high-resolution heart rate can be very
# large. 256 MB is the sanity cap, not a target: json.loads of a body this
# size peaks at roughly 4-6x in RAM, which is about what a small box like a
# Raspberry Pi can absorb.
MAX_BODY = 256 * 1024 * 1024


def day_of(datapoint):
    """HAE datapoints carry 'date' like '2026-08-14 07:01:23 +0200'."""
    d = str(datapoint.get("date", ""))[:10]
    try:
        datetime.strptime(d, "%Y-%m-%d")
        return d
    except ValueError:
        return None


# Health Auto Export sometimes re-sends a night as a *shorter* fragment than
# one it already sent: a wake gap makes it score sleep onset later, so the
# record starts later and totalSleep drops. Plain newest-wins then throws away
# data we already had - observed in practice, where a manual re-sync replaced a
# 4.85 h record (start 04:37) with a 4.18 h one (start 05:18). Sleep is a span,
# not a scalar, so it gets merged instead: non-overlapping segments accumulate,
# and where two records describe the same night the fuller one wins.
SPAN_MERGE_METRICS = {"sleep_analysis"}


def _sleep_span(dp):
    """(start, end) as naive wall-clock datetimes, or None if unparseable.

    Wall clock is the right comparison here: every record comes from one phone
    in one timezone, and the only cost is an hour of slop at a DST boundary -
    where the fallback is treating two records as separate nights, which never
    loses data.
    """
    out = []
    for value in (dp.get("sleepStart") or dp.get("inBedStart"),
                  dp.get("sleepEnd") or dp.get("inBedEnd")):
        try:
            out.append(_timestamp(str(value).strip()).replace(tzinfo=None))
        except (ValueError, TypeError):
            return None
    start, end = out
    return (start, end) if start <= end else (end, start)


def merge_spans(existing, incoming):
    """Keep the fullest view of each night rather than simply the newest.

    Datapoints without parseable timestamps fall back to append-only, which is
    never worse than the overwrite it replaces.
    """
    kept = []
    for dp in list(existing or []) + list(incoming or []):
        span = _sleep_span(dp)
        if span is None:
            if dp not in kept:
                kept.append(dp)
            continue
        for i, other in enumerate(kept):
            other_span = _sleep_span(other)
            # overlapping spans describe the same night; keep more sleep
            if other_span and span[0] <= other_span[1] and other_span[0] <= span[1]:
                if (dp.get("totalSleep") or 0) > (other.get("totalSleep") or 0):
                    kept[i] = dp
                break
        else:
            kept.append(dp)
    return sorted(kept, key=lambda d: (_sleep_span(d) or (datetime.min,))[0])


# Metrics that accumulate over the day. Exported raw (no aggregation in the
# app) they arrive as one datapoint per second or so - 80,000+ a day for basal
# energy - which once grew a day file from 30 KB to 30 MB and made
# health.py summary run past ten minutes. For a sum, per-second resolution
# buys nothing, so raw datapoints are folded into hourly sums here, in the
# same shape the app's own hourly aggregation produces. Must stay in step with
# SUM_METRICS in health.py.
#
# This is a safety net, not a dedupe: raw samples from iPhone and Watch can
# describe the same steps, and summing both double-counts. The intended setup
# keeps these metrics on the app's hourly aggregation, which deduplicates
# sources itself; this only stops a raw export from bloating the files.
ACCUMULATING_METRICS = {
    "step_count", "active_energy", "basal_energy_burned", "dietary_energy",
    "protein", "walking_running_distance", "cycling_distance",
    "swimming_distance", "flights_climbed", "apple_exercise_time",
    "apple_stand_time", "apple_stand_hour", "time_in_daylight",
    "mindful_minutes",
}


def _timestamp(value):
    # HAE separates the UTC offset with a space, unlike ISO 8601.
    head, separator, tail = value.rpartition(" ")
    if separator and tail.startswith(("+", "-")) and len(tail) in (5, 6):
        value = head + tail
    return datetime.fromisoformat(value)


def _on_the_hour(dp):
    stamp = dp.get("date", "")
    if len(stamp) < 19:
        return False
    dt = _timestamp(stamp)
    return dt.minute == dt.second == dt.microsecond == 0


def fold_hourly(dps, *, force=False):
    """Raw datapoints of an accumulating metric -> one summed point per hour.

    Already-hourly input (every timestamp on the hour) passes through
    untouched. The hour key keeps the original UTC offset, so the result
    reads exactly like an app-aggregated hourly bucket.
    """
    if not force and all(_on_the_hour(dp) for dp in dps):
        return dps
    hours = {}
    for dp in dps:
        qty = dp.get("qty")
        stamp = str(dp.get("date", ""))
        if not isinstance(qty, (int, float)) or len(stamp) < 19:
            continue
        dt = _timestamp(stamp)
        key = dt.strftime("%Y-%m-%d %H:00:00")
        if dt.tzinfo is not None:
            key += " " + dt.strftime("%z")
        if key not in hours:
            hours[key] = {"date": key, "qty": 0.0, "source": "",
                          "units": dp.get("units")}
        if hours[key]["units"] != dp.get("units"):
            raise ValueError("cannot fold mixed units in an hour")
        hours[key]["qty"] += qty
    return [hours[k] for k in sorted(hours)]


def _validate_stamp(value):
    """Accept HAE wall-clock/offset and ISO timestamps, or a calendar date."""
    if not isinstance(value, str) or len(value) < 10:
        raise ValueError("missing timestamp")
    day = value[:10]
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
        if parsed.strftime("%Y-%m-%d") != day:
            raise ValueError("noncanonical date")
        if len(value) > 10:
            _timestamp(value)
    except ValueError as exc:
        raise ValueError("invalid calendar timestamp") from exc
    return day


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    for key in ("metrics", "workouts"):
        if data.get(key) is not None and not isinstance(data[key], list):
            raise ValueError(f"{key} must be a list")
    names = set()
    for metric in data.get("metrics") or []:
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str) or not metric["name"]:
            raise ValueError("metric must have a name")
        if metric["name"] in names:
            raise ValueError("duplicate metric name")
        names.add(metric["name"])
        if not isinstance(metric.get("data"), list):
            raise ValueError("metric data must be a list")
        if metric.get("units") is not None and not isinstance(metric["units"], str):
            raise ValueError("units must be a string")
        for dp in metric["data"]:
            if not isinstance(dp, dict):
                raise ValueError("datapoint must be an object")
            _validate_stamp(dp.get("date"))
            if dp.get("units") is not None and not isinstance(dp["units"], str):
                raise ValueError("units must be a string")
            if metric["name"] in ACCUMULATING_METRICS and "qty" not in dp:
                raise ValueError("accumulating metric requires qty")
            for key in ("qty", "Min", "Max", "Avg", "totalSleep", "asleep", "inBed", "deep", "rem", "core", "awake"):
                if key in dp and (isinstance(dp[key], bool) or not isinstance(dp[key], (int, float)) or not math.isfinite(dp[key])):
                    raise ValueError("metric values must be finite numbers")
            for key in ("sleepStart", "sleepEnd", "inBedStart", "inBedEnd"):
                if dp.get(key):
                    _validate_stamp(dp[key])
    for workout in data.get("workouts") or []:
        if not isinstance(workout, dict):
            raise ValueError("workout must be an object")
        _validate_stamp(workout.get("start"))
        if workout.get("end") is not None:
            _validate_stamp(workout["end"])
    return data


def _aggregation(dps):
    """Classify timestamps, never cardinality alone; midnight alone is ambiguous."""
    if not dps:
        return None
    if len(dps) == 1 and (len(dps[0]["date"]) == 10 or dps[0]["date"][11:19] == "00:00:00"):
        return "ambiguous"
    return "hourly" if all(_on_the_hour(dp) for dp in dps) else "raw"


def store(payload):
    """Validate and prepare the entire export before replacing any day file.

    Replacement is atomic per file, not a multi-file filesystem transaction.
    """
    data = _validate_payload(payload)
    per_day = {}  # date -> metric and workout lists
    modes = {}
    for metric in data.get("metrics", []) or []:
        mode = metric.get("aggregation", data.get("aggregation", payload.get("aggregation")))
        if mode is not None:
            aliases = {"hour": "hourly", "day": "daily", "none": "raw"}
            mode = aliases.get(str(mode).lower(), str(mode).lower())
            if mode not in {"hourly", "daily", "raw"}:
                raise ValueError("unsupported aggregation")
        modes[metric.get("name")] = mode

    for metric in data.get("metrics", []) or []:
        name = metric.get("name")
        if not name:
            continue
        for dp in metric.get("data", []) or []:
            day = day_of(dp)
            if not day:
                continue
            bucket = per_day.setdefault(day, {"metrics": {}, "workouts": []})
            bucket["metrics"].setdefault(name, []).append(
                dict(dp, units=metric.get("units") or dp.get("units"))
            )

    for wo in data.get("workouts", []) or []:
        day = str(wo.get("start", ""))[:10]
        if day:
            per_day.setdefault(day, {"metrics": {}, "workouts": []})[
                "workouts"
            ].append(wo)

    root = Path(DAILY_DIR).resolve()
    prepared = []
    metric_names = set()
    for day, incoming in per_day.items():
        path = root / f"{day}.json"
        if not path.resolve().is_relative_to(root):
            raise ValueError("day path escapes health directory")
        try:
            with open(path) as fh:
                existing = json.load(fh)
        except FileNotFoundError:
            existing = {"metrics": {}, "workouts": []}
        # Sleep retains span semantics; other metrics carry aggregation metadata.
        # Metadata-free midnight singletons cannot prove daily vs hourly and must
        # not be combined with other timestamps without an explicit mode.
        for name, dps in incoming["metrics"].items():
            previous = existing["metrics"].get(name) or []
            if name in SPAN_MERGE_METRICS:
                existing["metrics"][name] = merge_spans(previous, dps)
                continue
            mode = modes[name] or _aggregation(dps)
            old_mode = existing.get("metric_aggregation", {}).get(name) or _aggregation(previous)
            if mode == "daily":
                if len(dps) != 1:
                    raise ValueError("daily aggregation requires one point per day")
                existing["metrics"][name] = dps
                existing.setdefault("metric_aggregation", {})[name] = mode
                continue
            if modes[name] == "hourly" and not all(_on_the_hour(dp) for dp in dps):
                raise ValueError("hourly aggregation requires hour-aligned timestamps")
            if old_mode == "daily":
                raise ValueError("cannot merge partial data into a daily aggregate")
            if previous:
                # An ambiguous singleton may only update the exact same keys;
                # an explicit daily snapshot can replace it via the branch above.
                if old_mode == "ambiguous" or mode == "ambiguous":
                    same_keys = {dp["date"] for dp in previous} == {dp["date"] for dp in dps}
                    if not same_keys:
                        raise ValueError("ambiguous daily/hourly data; explicit full daily export required")
                elif old_mode != mode:
                    raise ValueError("cannot mix raw and hourly aggregation")
            existing.setdefault("metric_aggregation", {})[name] = mode
            if name in ACCUMULATING_METRICS and mode == "raw":
                if any(len(dp["date"]) < 19 for dp in dps):
                    raise ValueError("raw accumulating metrics require full timestamps")
                dps = fold_hourly(dps, force=True)
                previous = fold_hourly(previous)
            merged = {dp["date"]: dp for dp in previous}
            merged.update({dp["date"]: dp for dp in dps})
            existing["metrics"][name] = [merged[k] for k in sorted(merged)]
        if incoming["workouts"]:
            existing["workouts"] = incoming["workouts"]
        existing["updated"] = datetime.now(timezone.utc).isoformat()
        prepared.append((path, json.dumps(existing, allow_nan=False)))
        metric_names.update(incoming["metrics"])

    if prepared:
        root.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for path, contents in prepared:
            with tempfile.NamedTemporaryFile(mode="w", dir=root, prefix=".health-", delete=False) as fh:
                staged.append((Path(fh.name), path))
                fh.write(contents)
                fh.flush()
                os.fsync(fh.fileno())
        for tmp, path in staged:
            os.replace(tmp, path)
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    return len(per_day), sorted(metric_names)


# Concurrent posts are unlikely (one iPhone) but the daily-file merge is
# read-modify-write, so serialize it rather than hoping.
STORE_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 with keep-alive; every response carries Content-Length. Safe
    # only because the server is threaded - with the old single-threaded
    # HTTPServer, caddy holding a keep-alive connection open would have
    # blocked the watchdog's /ping and gotten the server "healed" to death
    # every 5 minutes.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # timestamped, to stdout for the cron log
        print(f"{datetime.now().isoformat()} {self.address_string()} {fmt % args}",
              flush=True)

    def _respond(self, code, message):
        body = json.dumps({"status": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain(self):
        """Discard an unread request body (bounded) before an error reply.

        Responding while the client is still streaming its upload and then
        closing the socket makes iOS URLSession report the response as
        unparseable (NSURLError -1017) instead of showing the actual error.
        Reading the body out first lets the 4xx arrive cleanly. Bounded by
        MAX_BODY; anything bigger gets the abrupt close anyway.
        """
        try:
            remaining = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            self.close_connection = True

    def do_GET(self):
        # Health check for the watchdog and for curl-based debugging.
        ping = self.path in ("/ping", "/healthexport/ping")
        self._respond(200, "ok" if ping else "POST /health")

    def _read_body(self):
        """Body bytes, honouring chunked transfer encoding; None = too big
        or absent. Clients (and proxies) are free to send large uploads
        chunked without a Content-Length, and rejecting those looks exactly
        like a size limit to the sender."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            body = bytearray()
            while True:
                size_line = self.rfile.readline(80)
                try:
                    size = int(size_line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    return None
                if size == 0:
                    self.rfile.readline()  # trailing CRLF after last-chunk
                    return bytes(body)
                if len(body) + size > MAX_BODY:
                    return None
                body += self.rfile.read(size)
                self.rfile.read(2)  # chunk's trailing CRLF
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return None
        return self.rfile.read(length)

    def do_POST(self):
        # /health is the direct path; /healthexport is the same endpoint for
        # setups that put it behind a reverse proxy on a shared domain (the
        # proxy can pass the path through untouched, so accept both here
        # rather than making it rewrite).
        if self.path not in ("/health", "/healthexport"):
            self._drain()
            return self._respond(404, "unknown path")
        if not API_KEY or not hmac.compare_digest(
                self.headers.get("x-api-key", "").encode("utf-8"), API_KEY.encode("utf-8")):
            self._drain()
            return self._respond(403, "bad or missing x-api-key header")
        try:
            body = self._read_body()
        except ValueError:
            self.close_connection = True
            return self._respond(400, "invalid request framing")
        if body is None:
            self.close_connection = True
            return self._respond(413, "body missing or too large")
        try:
            payload = json.loads(body)
        except ValueError:
            # Covers JSONDecodeError AND UnicodeDecodeError (both are
            # ValueErrors) - a body that isn't UTF-8 raises the latter, and
            # letting it escape kills the request mid-flight (the proxy then
            # reports 502 instead of a clean 400).
            return self._respond(400, "not valid JSON")
        try:
            with STORE_LOCK:
                days, metrics = store(payload)
        except ValueError:
            return self._respond(400, "invalid health payload or incompatible aggregation")
        except Exception as e:  # noqa: BLE001 - keep the server alive
            print(f"{datetime.now().isoformat()} STORE FAILED: {e}", flush=True)
            return self._respond(500, "store failed - see health.log")
        print(f"{datetime.now().isoformat()} stored {days} day(s): "
              f"{', '.join(metrics) or 'no metrics'}", flush=True)
        self._respond(200, f"stored {days} day(s)")


def main():
    if not API_KEY.strip():
        sys.exit("HEALTH_API_KEY must be set to a nonempty secret")
    try:
        server = ThreadingHTTPServer((os.environ.get("HEALTH_BIND", "127.0.0.1"), PORT), Handler)
    except OSError as e:
        if e.errno == 98:  # EADDRINUSE - already running, watchdog no-op
            sys.exit(0)
        raise
    print(f"{datetime.now().isoformat()} health server listening on "
          f"{socket.gethostname()}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
