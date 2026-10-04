#!/usr/bin/env python3
"""Receives Apple Health data from the Health Auto Export iOS app.

The app POSTs JSON to http://<coach machine>:5556/health on its automation schedule
(shape: {"data": {"metrics": [...], "workouts": [...]}}). This stores each
metric's datapoints into one JSON file per calendar day:

    <coach home>/athlete/state/health/daily/YYYY-MM-DD.json
    {"metrics": {"sleep_analysis": [...], "resting_heart_rate": [...]}, ...}

Re-exports overwrite that day's entry for the metric rather than appending,
so the app re-sending "today so far" every few hours is idempotent and the
newest export wins. scripts/health.py reads these files back; nothing else
does. Workouts in the payload are stored under a "workouts" key the same
way, but Strava stays the source of truth for training - they're kept only
because deleting data someone chose to export would be surprising.

Auth: requests must carry the HEALTH_API_KEY value (from the coach's .env)
in an "x-api-key" header (configure it under "Headers" in the app's API
Export settings). Health data is worth an auth header even on a home LAN.

Runs as a daemon via ensure_health_server.sh from cron. If the port is
already bound it exits 0 immediately - that is the watchdog no-op path.

Standard library only, single-threaded on purpose: one iPhone posting every
few hours does not need more, and less machinery means less to break.
"""

import json
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
            out.append(datetime.strptime(str(value).strip()[:19], "%Y-%m-%d %H:%M:%S"))
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


def _on_the_hour(dp):
    return str(dp.get("date", ""))[13:19] == ":00:00"


def fold_hourly(dps):
    """Raw datapoints of an accumulating metric -> one summed point per hour.

    Already-hourly input (every timestamp on the hour) passes through
    untouched. The hour key keeps the original UTC offset, so the result
    reads exactly like an app-aggregated hourly bucket.
    """
    if all(_on_the_hour(dp) for dp in dps):
        return dps
    hours = {}
    for dp in dps:
        qty = dp.get("qty")
        stamp = str(dp.get("date", ""))
        if not isinstance(qty, (int, float)) or len(stamp) < 19:
            continue
        key = stamp[:13] + ":00:00" + stamp[19:]
        if key not in hours:
            hours[key] = {"date": key, "qty": 0.0, "source": "",
                          "units": dp.get("units")}
        hours[key]["qty"] += qty
    return [hours[k] for k in sorted(hours)]


def store(payload):
    """Merge one export into the per-day files. Returns (days, metrics)."""
    data = payload.get("data", payload)
    per_day = {}  # date -> {"metrics": {name: [dp,...]}, "workouts": [...]}

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
                {k: v for k, v in dp.items()} | {"units": metric.get("units")}
            )

    for wo in data.get("workouts", []) or []:
        day = str(wo.get("start", ""))[:10]
        if day:
            per_day.setdefault(day, {"metrics": {}, "workouts": []})[
                "workouts"
            ].append(wo)

    os.makedirs(DAILY_DIR, exist_ok=True)
    metric_names = set()
    for day, incoming in per_day.items():
        path = os.path.join(DAILY_DIR, f"{day}.json")
        try:
            with open(path) as fh:
                existing = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            existing = {"metrics": {}, "workouts": []}
        # Days the export doesn't mention stay put. Within a day:
        #   sleep      -> span merge, so a shorter re-export can't clobber a
        #                 fuller record (see merge_spans)
        #   hourly     -> merge keyed on the hour, so a partial re-export
        #                 refines the hours it carries and leaves the rest
        #   daily      -> replace wholesale, the old newest-wins behaviour
        # "Hourly" is inferred from the payload carrying more than one
        # datapoint for the metric that day. That keeps the two aggregation
        # settings from ever mixing in one list: switching the app back to
        # daily sends a single datapoint, which replaces the hourly buckets
        # outright rather than sitting alongside them and being averaged in.
        for name, dps in incoming["metrics"].items():
            folded = False
            if name in ACCUMULATING_METRICS:
                folded_dps = fold_hourly(dps)
                folded, dps = folded_dps is not dps, folded_dps
            if name in SPAN_MERGE_METRICS:
                existing["metrics"][name] = merge_spans(
                    existing["metrics"].get(name), dps)
            # A folded raw export is hourly by construction even when it only
            # spans one hour - replacing the day with it would drop the rest.
            elif len(dps) > 1 or folded:
                merged = {str(dp.get("date")): dp
                          for dp in (existing["metrics"].get(name) or [])}
                merged.update({str(dp.get("date")): dp for dp in dps})
                existing["metrics"][name] = [merged[k] for k in sorted(merged)]
            else:
                existing["metrics"][name] = dps
        if incoming["workouts"]:
            existing["workouts"] = incoming["workouts"]
        existing["updated"] = datetime.now(timezone.utc).isoformat()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(existing, fh)
        os.replace(tmp, path)
        metric_names.update(incoming["metrics"])

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
        except OSError:
            pass

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
        if API_KEY and self.headers.get("x-api-key", "") != API_KEY:
            self._drain()
            return self._respond(403, "bad or missing x-api-key header")
        body = self._read_body()
        if body is None:
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
        except Exception as e:  # noqa: BLE001 - keep the server alive
            print(f"{datetime.now().isoformat()} STORE FAILED: {e}", flush=True)
            return self._respond(500, "store failed - see health.log")
        print(f"{datetime.now().isoformat()} stored {days} day(s): "
              f"{', '.join(metrics) or 'no metrics'}", flush=True)
        self._respond(200, f"stored {days} day(s)")


def main():
    if not API_KEY:
        print("WARNING: HEALTH_API_KEY is empty - accepting unauthenticated "
              f"posts. Set it in {cc.HOME}/.env.", flush=True)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        if e.errno == 98:  # EADDRINUSE - already running, watchdog no-op
            sys.exit(0)
        raise
    print(f"{datetime.now().isoformat()} health server listening on "
          f"{socket.gethostname()}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
