# Delivery reliability

Scheduled text delivery is owned by `delivery.py`, not by the model. The
Telegram plugin stays disabled for scheduled generation; the existing shell
entrypoints and prompt overrides still work.

## State and confirmation

`athlete/state/delivery.sqlite3` is the durable queue **and outbox**:

- `activity`: one row per discovered activity, with `pending`, `delivered`,
  `skipped`, `covered`, `seeded`, or `ignored` status. Discovery is not delivery.
- `job`: saved message, staged coverage notes, next unconfirmed chunk, and
  status. Keys are `postrun:<id>` or `<morning|evening>:<local-date>`.
- `meta`: first-run initialization and consecutive detection failures.

SQLite transactions use `synchronous=FULL`. Successful generation saves the
whole message and staged notes before attempting transport. Every chunk needs
both successful curl/HTTP status and a JSON response with literal `ok: true`.
Curl has a 10-second connect timeout and 30-second overall timeout; a 35-second
subprocess backstop also applies. Alerts and optional charts use the same
checked, bounded transport. Charts remain best effort, outside the text job.

A failed chunk stops that job. Confirmed chunks are checkpointed individually;
retry resumes at the first unconfirmed chunk without rerunning the model.
Coverage commits only after **all** chunks are confirmed. Stable log entry IDs
make recovery after a crash during log commit idempotent. A failed or empty
model response is never sent; its diagnostic output is saved in `outbox/*.txt`.

During generation, `COACH_DELIVERY_STAGE` redirects `coachlog.py add` to an
isolated staging log. `recent` and `covered` still read only committed history.
The runner persists staged entries together with the message and commits them
after delivery. `SKIP` is terminal but is **not** a delivered message and commits
no staged coverage. `--no-send` uses `COACH_DRY_RUN=1`, generates afresh, and does
not write delivery state, coverage, or outbox messages. Other analysis and
calendar side effects are not delivery transactions. Keep prompt overrides on
`coachlog.py add`; direct file writes or explicitly removing the transaction
environment bypass the protocol and are unsupported, not sandboxed.

## Polling and recovery

Each eligible newly discovered activity gets its own reaction, not only the
newest. Commutes, future/invalid timestamps and activities at least 12 hours old
are ignored **at discovery**. Pending activities keep retrying even after they
age out or disappear from the detection result. A failed activity does not stop
other activities from being attempted. Previously committed coverage suppresses
new reactions without relying on a model decision.

Every watcher poll also retries saved text jobs, including morning/evening jobs,
even when activity detection fails. To retry saved jobs without detection or
regeneration (this sends real messages when credentials are configured):

```bash
source skill/scripts/lib.sh
export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
python3 "$SCRIPT_DIR/delivery.py" retry
```

If the watcher is not scheduled (for example Strava is disabled), schedule this
retry command separately or invoke it manually. Re-running a scheduled mode on
the same local date resumes its existing job; it does not generate another
message after success. Failed **generation** for post-run activities retries on
the next poll; failed scheduled generation requires re-running that mode.

Detection requests the latest 100 activities. Discovery is still bounded by
that upstream result and the model correctly extracting it; it cannot recover
unseen uploads that have already fallen outside that window. Four consecutive
detection failures trigger one best-effort alert, and recovery triggers another.

## Migration and operations

1. Pause cron and let old in-flight watchers/check-ins finish before updating.
2. Keep `postrun_seen.txt` and `checkin_log.jsonl`. Existing seen IDs are imported
   as a no-replay baseline; legacy committed coverage remains authoritative.
   Old seen IDs are not assumed to have confirmed delivery, but are deliberately
   **not replayed** because legacy state cannot distinguish historical success
   from failure. Review old `outbox/*.txt` manually rather than bulk-replaying it.
3. With no seen file and no initialized database, the first valid poll seeds
   the currently returned activities without sending historic reactions.
4. Back up the database with athlete state (pause writers or use SQLite backup).
   Do not delete it to clear a lock: doing so loses pending jobs/checkpoints.

`postrun_seen.txt` continues to be maintained as a compatibility discovery
snapshot; delivery status lives in SQLite. Deleting only the seen file does not
reset an existing queue. IDs are not evicted and later rediscovered as new.

Locks use Python `fcntl.flock` on Linux/macOS: live locks are never removed based
on age or a guessed stale PID, and the kernel releases them when a process
exits. Leave the lock files in place. Polls do not overlap; all scheduled text
generation/delivery and retries serialize through the same delivery lock.

**Exactly-once delivery is impossible here.** Telegram may accept a message and
then lose the response, or the process may crash before saving its checkpoint.
That ambiguous chunk is retried and can appear twice. A confirmed API response
also does not prove that the athlete read the message. This is durable retry
with checkpointed confirmations, not an exactly-once or read-receipt guarantee.

Offline tests use temporary homes and injected model/curl executables:
`TMPDIR=<scratch-directory> python3 -m unittest discover -s tests -p test_delivery.py -v`.
`COACH_CURL_BIN` is an optional transport executable override used by these tests;
production defaults to `curl` and the Telegram HTTPS API.
