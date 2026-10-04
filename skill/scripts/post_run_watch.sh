#!/usr/bin/env bash
# Polls Strava for a freshly uploaded activity and fires a post-run
# check-in for it, so feedback arrives minutes after a workout instead of
# at the evening review. Run from cron every 30 minutes during waking hours.
#
# Two stages, deliberately split by cost:
#   1. DETECT - a minimal Haiku run whose only job is to list the last few
#      activities as JSON. --strict-mcp-config with an inline Strava-only
#      server keeps every other MCP out, most importantly telegram: this is
#      the one configuration proven NOT to steal the persistent session's
#      poll lock (see docs/deployment.md "Two processes, one bot token").
#      It needs a user-scope MCP server named exactly `strava-mcp` that has
#      been authenticated once - the inline definition reuses its stored login.
#   2. REACT - only when something new and worth reacting to appears,
#      check_in.sh postrun runs the full coaching pipeline on it.
#
# What does NOT trigger a reaction:
#   - commute sport types (activities.commute_types in athlete/config.toml,
#     e-bike rides by default). Their ids are still marked seen.
#   - anything older than 12 hours (a backfilled upload is the evening
#     check-in's business, not a "just finished!" reaction)
#   - anything already reacted to (state/postrun_seen.txt), and, one layer
#     deeper, anything already covered in checkin_log.jsonl - check_in.sh
#     postrun asks the coach to reply SKIP for those.

set -uo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SEEN="$STATE_DIR/postrun_seen.txt"
FAILS="$STATE_DIR/postrun_failures"
LOCK="$STATE_DIR/postrun.lock"
mkdir -p "$STATE_DIR"

if [[ ! -x "$CLAUDE_BIN" ]]; then
  echo "$(now) FATAL: claude binary not found at '$CLAUDE_BIN'." >&2
  exit 127
fi

# One watcher at a time; a still-running reaction (up to 15 min) must not be
# piled onto by the next poll. mkdir is atomic everywhere, unlike flock which
# macOS lacks. A lock older than 30 minutes is a crashed run, not a live one.
if ! mkdir "$LOCK" 2>/dev/null; then
  if [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ]]; then
    echo "$(now) removing stale lock"
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    echo "$(now) previous poll still running - skipping"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK"' EXIT

STRAVA_ONLY='{"mcpServers":{"strava-mcp":{"type":"http","url":"https://mcp.strava.com/mcp"}}}'
# --allowedTools: an ad-hoc --mcp-config server carries no stored approval,
# and a -p run can't prompt, so without this the model just reports it needs
# permission and the poll parses nothing.
RAW=$(cd "$COACH_HOME" && run_timeout 300 "$CLAUDE_BIN" --model haiku \
  --strict-mcp-config --mcp-config "$STRAVA_ONLY" \
  --allowedTools "mcp__strava-mcp" \
  -p "Call the strava-mcp list_activities tool with per_page=3. From the result, output ONLY a JSON array with one object per activity, shaped exactly like: [{\"id\": 123, \"sport_type\": \"Run\", \"start_local\": \"2026-08-14T18:00:00\"}]. No prose, no markdown fences, nothing but the JSON array." \
  2>>"$LOG_DIR/postrun.log")
STATUS=$?

# A single failed poll is noise - the next one retries. But a run of them
# means post-run reactions have quietly stopped, so after four in a row (two
# hours) say so once, and once more when it recovers.
record_detect() {
  local n cause
  n=$(cat "$FAILS" 2>/dev/null || echo 0)
  if [[ "$1" == "ok" ]]; then
    [[ "$n" -ge 4 ]] && alert "Post-run reactions are working again."
    rm -f "$FAILS"
    return
  fi
  n=$((n + 1))
  echo "$n" > "$FAILS"
  if [[ "$n" -eq 4 ]]; then
    cause=$(diagnose "$RAW" "$(tail -20 "$LOG_DIR/postrun.log" 2>/dev/null)")
    alert "⚠️ Post-run reactions have failed 4 polls in a row (2 h). ${cause:-See logs/postrun.log on the coach machine.}"
  fi
}

if [[ $STATUS -ne 0 ]]; then
  echo "$(now) detect stage failed (exit $STATUS): $RAW" >&2
  record_detect fail
  exit 0   # transient; next poll retries
fi

COMMUTE="$(coach_get activities.commute_types)"

# Decide in python: parse leniently (grab the first [...] block), mark all
# ids seen, emit at most one TRIGGER line for the newest new activity.
DECISION=$(python3 - "$SEEN" "$RAW" "$COMMUTE" <<'PY'
import json, re, sys, os
from datetime import datetime, timedelta

seen_path, raw, commute = sys.argv[1], sys.argv[2], set(sys.argv[3].split())
m = re.search(r"\[.*\]", raw, re.S)
if not m:
    print(f"NOPARSE {raw[:200]!r}", file=sys.stderr)
    sys.exit(0)
try:
    acts = json.loads(m.group(0))
except json.JSONDecodeError as e:
    print(f"NOPARSE {e}", file=sys.stderr)
    sys.exit(0)

first_run = not os.path.exists(seen_path)
seen = set()
if not first_run:
    with open(seen_path) as fh:
        seen = {line.strip() for line in fh if line.strip()}

now = datetime.now()
trigger = None
for a in acts:  # newest first from list_activities
    aid = str(a.get("id", "")).strip()
    if not aid or aid in seen:
        continue
    sport = str(a.get("sport_type", "unknown"))
    fresh = False
    try:
        start = datetime.fromisoformat(str(a.get("start_local", ""))[:19])
        fresh = now - start < timedelta(hours=12)
    except ValueError:
        pass
    if trigger is None and sport not in commute and fresh and not first_run:
        trigger = (aid, sport)

# Mark everything current as seen, keep the tail bounded.
all_ids = seen | {str(a.get("id", "")).strip() for a in acts if a.get("id")}
with open(seen_path, "w") as fh:
    fh.write("\n".join(sorted(all_ids)[-200:]) + "\n")

if first_run:
    print("INIT")
elif trigger:
    print(f"TRIGGER {trigger[0]} {trigger[1]}")
else:
    print("NOTHING_NEW")
PY
)

if [[ -z "$DECISION" ]]; then record_detect fail; else record_detect ok; fi

case "$DECISION" in
  TRIGGER\ *)
    read -r _ ACT_ID ACT_TYPE <<< "$DECISION"
    echo "$(now) new activity $ACT_ID ($ACT_TYPE) - running post-run reaction"
    "$SCRIPT_DIR/check_in.sh" postrun "$ACT_ID" "$ACT_TYPE"
    ;;
  INIT)
    echo "$(now) first run - recorded current activities as seen, no trigger"
    ;;
  NOTHING_NEW|"")
    : # quiet - this is the normal case ~30 times a day
    ;;
  *)
    echo "$(now) unexpected decision: $DECISION" >&2
    ;;
esac
