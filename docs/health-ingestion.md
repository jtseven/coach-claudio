# Health ingestion safety and compatibility

`skill/scripts/health_server.py` accepts Health Auto Export JSON using only the
Python standard library. `POST /health` and `POST /healthexport` are equivalent;
`GET /ping` and `GET /healthexport/ping` remain available for watchdogs. Both
Content-Length and chunked uploads are supported, with the existing 256 MiB cap.

## Deployment: authentication and binding

Set a nonempty `HEALTH_API_KEY` in the environment used by the watchdog. Every
POST requires the exact value in `x-api-key`; comparison uses `hmac.compare_digest`.
Missing/wrong headers return 403. An unset, empty, or whitespace-only configured
key stops startup with a nonzero exit status instead of opening an unauthenticated
receiver. Do not put the key in a URL or publish an athlete's `.env`.

The default bind is now **127.0.0.1**, port **5556** (`HEALTH_PORT` overrides the
port). A reverse proxy on the same host should forward to `127.0.0.1:5556` and
retain the authentication header. Use TLS on the external connection. The ping
endpoints intentionally disclose only service availability without authentication.

For direct phone-to-LAN use, explicitly set `HEALTH_BIND` to the machine's LAN
IPv4 address, or `0.0.0.0` to listen on all IPv4 interfaces. Apply a firewall and
keep authentication enabled. A proxy in a separate container may likewise need
an explicit reachable bind rather than loopback. Restart/reload of a running
service is an operator action; changing the source does not change an already
running receiver. `ensure_health_server.sh` continues to use the existing port
and loopback watchdog endpoint.

## Payload and disk format

Both `{"data": {"metrics": [...], "workouts": [...]}}` and an unwrapped
`{"metrics": [...], "workouts": [...]}` object are accepted. Omitted lists, or
null top-level metric/workout lists, mean no entries. Metric entries require a
nonempty string `name` and a `data` list of objects; duplicate metric names in
one export are rejected. Workouts require an object with a valid `start`.
Datapoints require a valid `date`. Dates are real `YYYY-MM-DD` calendar dates,
optionally followed by a valid HAE or ISO timestamp. Optional workout end and
sleep start/end timestamps are also validated. Numeric quantities must be finite
numbers, not strings or booleans. Accumulating metrics require `qty`.

The existing disk contract is unchanged:

```json
{
  "metrics": {"step_count": [{"date": "2026-06-01 01:00:00 +0000", "qty": 12, "units": "count"}]},
  "workouts": [],
  "metric_aggregation": {"step_count": "hourly"},
  "updated": "..."
}
```

`metric_aggregation` is additive ingestion metadata; `health.py` still reads
metric lists and normalizes units per datapoint. Unknown existing day-file fields
are retained. No background rewrite or bulk migration occurs. Metric-level units
are copied to datapoints; datapoint units survive when the metric omits units.
Workouts retain the existing newest-nonempty-list-per-day replacement behavior.
Strava remains the training source of truth.

## Aggregation rules

When available, provide `aggregation` on each metric. A default may instead be
provided on the `data` object or outer payload; the more specific value wins.
Supported values are `hourly`/`hour`, `daily`/`day`, and `raw`/`none` (case
insensitive). This is an optional receiver extension, **not a claim that every
Health Auto Export version emits aggregation metadata**. Do not invent a mode
from the number of datapoints alone.

* **Hourly:** merge by the complete timestamp string, including UTC offset.
  Reposted buckets replace matching timestamps; unmentioned buckets and days
  remain intact. A single incoming hourly bucket does not replace the whole day.
  Explicit hourly data must be on an actual hour boundary (no fractional second).
* **Daily:** explicitly declared daily data must have one point per metric/day.
  That snapshot replaces the metric's list, preventing daily totals from sitting
  beside hourly buckets and being summed twice. Other metrics are untouched.
  A subsequent partial hourly/raw upload into a known daily total is rejected.
* **Raw/high-frequency:** raw accumulating metrics use `fold_hourly` to sum into
  hourly buckets before timestamp-keyed merge. Explicit raw samples are folded
  even if their timestamps happen to be exactly hour-aligned. Previously stored
  legacy high-frequency accumulating samples are folded when that same metric
  is updated, rather than mixed with their folded equivalents. Mixed units in
  one folded hour are rejected, not added together. Nonaccumulating raw vitals
  keep their timestamp-keyed datapoints, preserving reader behavior.
* **Sleep:** overlapping spans keep the record with more total sleep; disjoint
  spans remain. Exact duplicate records without a span are deduplicated. Both
  HAE and ISO span timestamps are supported. Existing wall-clock comparison
  semantics are retained, including their DST limitation.

Without metadata, non-midnight hour-aligned timestamps are treated as hourly;
other full timestamps as raw. A lone date-only or midnight point is **ambiguous**:
it could be a daily aggregate or one midnight bucket. It can be stored and
reposted at the same timestamp, but cannot be combined with a different set of
timestamps. Such an upload returns 400 rather than silently mixing a daily total
with hourly data. A daily-only legacy file remains readable and exact-timestamp
reposts remain idempotent. Clients exporting daily data at other timestamps
should explicitly declare daily aggregation; timestamp shape cannot prove it.

Raw and hourly representations cannot be safely reconciled by summing them.
The receiver rejects known raw/hourly mode changes within a metric/day. Old
already-folded raw data without metadata looks like hourly data; it cannot be
reliably distinguished after the fact. Rejecting an incompatible update is safer
than guessing and double-counting. Keep one aggregation mode per metric/day.

Two cases seen in real HAE setups are tolerated instead of rejected, because a
rejection discards the *whole* export (sleep and steps included):

* **Point metrics from two automations** (e.g. `cardio_recovery`, `vo2_max`
  sent raw by one job and hourly by another). They are never summed, so the raw
  record is kept: raw replaces a stored hourly/ambiguous copy, and an incoming
  non-raw copy is skipped. Accumulating metrics still reject the mix.
* **Offset hourly buckets.** HAE anchors the first buckets of an export at the
  previous export's time (`22:29:26`, `23:29:26`, …). When the day already has
  clock-hour buckets and no aggregation is declared, incoming points sharing
  one identical minute:second offset are skipped as overlapping that day's
  complete record. Genuine raw samples (varying seconds) are still rejected.

Every skip and every 400 is logged with the metric, day and reason
(`REJECTED: ... [metric=... day=...]`), so a failing export can be diagnosed
from `health.log` without capturing the payload.

**Raw folding is a size guard, not a source deduplicator or a lossless raw archive.**
A raw upload must contain the full snapshot of each hour it touches: a later
upload replaces the corresponding folded hour, it does not add to the prior
sum. Delta-only raw samples inside an already stored hour are not supported.
Phone/watch overlap can double-count in raw mode; configure HAE's source-deduped
hourly export for accumulating metrics. Use a consistent timestamp spelling
within an export stream: timestamp-keyed merge does not equate alternate textual
representations of the same instant.

## Safe upgrade and recovery

1. Back up the existing health directory before any operator-led changes. Existing
   history remains readable without any migration. Do not rewrite all days or
   delete existing athlete data to adopt this change.
2. Set the key and explicitly opt into LAN binding if required before the operator
   restarts the service. Same-host reverse-proxy users normally need no bind
   override. Verify an authenticated synthetic POST before changing phone jobs.
3. Preserve the existing HAE JSON envelope, header, URLs, timestamps and units.
   Normal partial hourly uploads continue to merge, including one non-midnight
   bucket. If your HAE version omits aggregation metadata, date-only/midnight
   singletons remain inherently ambiguous; do not automatically relabel them.
4. For a 400 caused by ambiguity or a mode change, keep the file unchanged and
   inspect the exporter settings. An explicit full **daily** snapshot may replace
   the affected metric/day. To switch that day to hourly/raw, first preserve a
   backup and perform an operator-reviewed, targeted replacement of that one
   metric/day with a complete export in the desired mode, including correct
   metadata. There is deliberately no destructive automatic reset endpoint.
   Alternatively switch modes at a new day with no existing data.

Invalid payloads return 400 before any day is changed, including when the invalid
record/conflict occurs after an otherwise valid earlier day. Day paths are checked
for resolved containment (including existing symlinks). Existing malformed JSON
is not silently discarded. Each output is fully prepared, staged in a uniquely
created same-directory temporary file, flushed/fsynced, then atomically replaced.
Temporary files are cleaned up. This is atomic **per file**, not a multi-day
transaction: a filesystem failure during the replacement phase can leave some
valid days updated. Local filesystem writers must coordinate separately; the
HTTP server serializes its own requests with a lock.

## Offline verification

```sh
python3 -m unittest discover -s tests -p test_health_ingestion.py -v
```

All fixtures are synthetic, temporary directories hold every write, HTTP servers
use ephemeral loopback ports, and startup failure is checked in a subprocess with
a synthetic `COACH_HOME`. No real phone, athlete data, external APIs or service
restart is needed. Regression cases were observed failing before their fixes:
single-bucket day overwrite, unsafe workout dates/path containment, empty-key
fail-open, non-loopback default binding, mixed aggregation, raw folding and
spanless sleep duplicates. Integration coverage includes authentication, 400s,
both endpoint spellings, chunked uploads, repeat posts, raw sums, and sleep spans.
