#!/usr/bin/env bash
# Cron entrypoint for the scheduled coaching check-ins.
#
#   check_in.sh morning
#   check_in.sh evening
#   check_in.sh postrun <activity_id> [sport_type]   (from post_run_watch.sh)
#
# Testing without side effects:
#   check_in.sh morning --print-prompt   show the rendered prompt, run nothing
#   check_in.sh morning --no-send        run the coach, print the message, send
#                                        nothing, write nothing to athlete/state
#
# Cron runs with a minimal environment and will not read your shell profile,
# so everything this needs comes from lib.sh. That is the single most common
# reason a scheduled check-in silently does nothing.

set -uo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:-}"
shift || true
POSTRUN_ID=""
POSTRUN_TYPE=""
case "$MODE" in
  morning|evening) ;;
  postrun)
    POSTRUN_ID="${1:-}"
    POSTRUN_TYPE="${2:-unknown}"
    [[ "$POSTRUN_TYPE" == --* ]] && POSTRUN_TYPE="unknown"
    if [[ -z "$POSTRUN_ID" || "$POSTRUN_ID" == --* ]]; then
      echo "usage: $(basename "$0") postrun <activity_id> [sport_type] [--no-send|--print-prompt]" >&2
      exit 64
    fi
    ;;
  *)
    echo "usage: $(basename "$0") {morning|evening|postrun <activity_id> [sport_type]} [--no-send|--print-prompt]" >&2
    exit 64
    ;;
esac

PRINT_PROMPT=0
NO_SEND=0
for arg in "$@"; do
  case "$arg" in
    --print-prompt) PRINT_PROMPT=1 ;;
    --no-send) NO_SEND=1 ;;
  esac
done

if ! python3 "$SCRIPT_DIR/coach.py" check >/dev/null; then
  echo "$(now) FATAL: $(python3 "$SCRIPT_DIR/coach.py" check)" >&2
  exit 78
fi

# The coach writes the message as its normal response text and this script
# sends it - there is no telegram tool in this invocation (see the comment
# above the claude call below for why). The prompts live in skill/prompts/,
# overridable per athlete in athlete/prompts/.
PROMPT_ARGS=("$MODE")
[[ -n "$POSTRUN_ID" ]] && PROMPT_ARGS+=(--activity "$POSTRUN_ID" --sport "$POSTRUN_TYPE")
PROMPT=$(python3 "$SCRIPT_DIR/coach.py" prompt "${PROMPT_ARGS[@]}")

if [[ "$PRINT_PROMPT" -eq 1 ]]; then
  echo "$PROMPT"
  exit 0
fi

if [[ ! -x "$CLAUDE_BIN" ]]; then
  echo "$(now) FATAL: claude binary not found at '$CLAUDE_BIN'." >&2
  echo "Set CLAUDE_BIN in $COACH_HOME/.env to an absolute path." >&2
  exit 127
fi

if [[ "$NO_SEND" -eq 1 ]]; then
  export COACH_DRY_RUN=1
else
  if [[ "$(coach_get integrations.telegram)" != "true" ]]; then
    echo "$(now) Telegram is off in athlete/config.toml - nothing to send to. Use --no-send to test." >&2
    exit 78
  fi
  if [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "$(now) FATAL: TELEGRAM_CHAT_ID is not set. Check $COACH_HOME/.env." >&2
    exit 78
  fi
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "$(now) FATAL: TELEGRAM_BOT_TOKEN is not set. Check $TELEGRAM_ENV." >&2
    exit 78
  fi
fi

mkdir -p "$STATE_DIR"
touch "$STATE_DIR/checkin_log.jsonl" "$STATE_DIR/weekly_log.jsonl" \
      "$STATE_DIR/niggles.jsonl"

echo "$(now) starting $MODE check-in"

# --settings turns the telegram plugin off for THIS session only. That is the
# whole trick, and it is load-bearing:
#   - The plugin has to stay enabled globally, or the persistent session's
#     --channels binding silently stops delivering inbound messages.
#   - But any session that loads it spins up a competing Telegram poller and
#     kills the persistent session's connection.
# Disabling it per-session satisfies both. Everything else loads normally -
# Strava, Hevy and the claude.ai Google Calendar connector all work here, which
# is why no --strict-mcp-config is used (that flag would also exclude the
# calendar connector). docs/deployment.md "Two processes, one bot token" has
# the full reasoning and the dead ends.
#
# The coach runs from COACH_HOME, which must be a folder claude trusts.
#
# stdout and stderr are kept separate on purpose: stdout is sent verbatim, so
# a stray warning on stderr merged into it would arrive as part of the
# coaching message. stderr goes to the cron log instead.
#
# The timeout is a backstop against a hung run holding the slot until the
# next one. 15 minutes is generous even for the weekly planning run.
STDERR_FILE=$(mktemp)
NO_TELEGRAM='{"enabledPlugins":{"telegram@claude-plugins-official":false}}'
MESSAGE=$(cd "$COACH_HOME" && run_timeout 900 "$CLAUDE_BIN" --settings "$NO_TELEGRAM" -p "$PROMPT" 2>"$STDERR_FILE")
STATUS=$?
CLAUDE_STDERR=$(cat "$STDERR_FILE" 2>/dev/null)
rm -f "$STDERR_FILE"

echo "$MESSAGE"
[[ -n "$CLAUDE_STDERR" ]] && echo "$(now) claude stderr: $CLAUDE_STDERR" >&2

if [[ "$NO_SEND" -eq 1 ]]; then
  echo "$(now) --no-send: exit $STATUS, nothing sent, nothing logged"
  exit "$STATUS"
fi

# Anything that means "don't send this to Telegram" lands here: keep the
# output where it can be read, exit non-zero so it shows up in the log, and
# tell the athlete directly that the check-in failed. Without that, a failed
# check-in is only discovered by noticing that no message ever arrived.
save_and_fail() {
  local reason="$1" stamp cause
  stamp=$(date +%Y%m%dT%H%M%S)
  {
    echo "$MESSAGE"
    [[ -n "$CLAUDE_STDERR" ]] && printf '\n--- stderr ---\n%s\n' "$CLAUDE_STDERR"
  } > "$OUTBOX_DIR/${MODE}-${stamp}.txt"
  echo "$(now) FAILED ($reason). Output saved to outbox/${MODE}-${stamp}.txt" >&2
  cause=$(diagnose "$MESSAGE" "$CLAUDE_STDERR")
  alert "⚠️ The ${MODE} check-in failed (${reason}). ${cause:-Output saved to outbox/${MODE}-${stamp}.txt on the coach machine - check logs/${MODE}.log.}"
}

if [[ $STATUS -eq 124 ]]; then
  save_and_fail "timed out after 15 minutes"
  exit 124
fi

if [[ $STATUS -ne 0 ]]; then
  save_and_fail "exit $STATUS"
  exit "$STATUS"
fi

# An empty reply would be rejected by the Bot API anyway, and silently
# sending nothing is worse than a visible failure in the log.
if [[ -z "${MESSAGE//[[:space:]]/}" ]]; then
  save_and_fail "claude produced an empty message"
  exit 65
fi

# A post-run reaction bows out when the activity was already discussed -
# that's the coach declining to repeat itself, not a failure.
if [[ "$MODE" == "postrun" && "${MESSAGE//[[:space:]]/}" == "SKIP" ]]; then
  echo "$(now) postrun: activity ${POSTRUN_ID} already covered - nothing sent"
  exit 0
fi

# Direct Telegram Bot API call - no MCP, no bun subprocess, no contention
# with the persistent poller. Chunk defensively at Telegram's 4096-char cap
# even though check-ins are meant to stay well under it.
send_chunk() {
  local chunk="$1" http_code body resp
  resp=$(mktemp)
  http_code=$(curl -s -o "$resp" -w '%{http_code}' \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${chunk}")
  body=$(cat "$resp" 2>/dev/null)
  rm -f "$resp"
  if [[ "$http_code" != "200" ]]; then
    echo "$(now) Telegram send failed (HTTP $http_code): $body" >&2
    return 1
  fi
}

SEND_OK=1
REST="$MESSAGE"
while [[ -n "$REST" ]]; do
  send_chunk "${REST:0:4000}" || SEND_OK=0
  REST="${REST:4000}"
done

if [[ "$SEND_OK" -ne 1 ]]; then
  STAMP=$(date +%Y%m%dT%H%M%S)
  echo "$MESSAGE" > "$OUTBOX_DIR/${MODE}-${STAMP}.txt"
  echo "$(now) FAILED to send to Telegram. Message saved to outbox/${MODE}-${STAMP}.txt" >&2
  exit 1
fi

# The weekly close-out gets the trend as a picture, rendered from
# weekly_log.jsonl. Needs matplotlib in the venv. Strictly best-effort: a
# missing venv or a render error must never fail a check-in that already sent.
lower() { tr '[:upper:]' '[:lower:]'; }
if [[ "$MODE" == "evening" && "$(coach_get integrations.weekly_chart)" == "true" \
      && "$(LC_ALL=C date +%A | lower)" == "$(coach_get schedule.closeout_day | lower)" \
      && -x "$COACH_HOME/venv/bin/python3" ]]; then
  CHART_PNG=$("$COACH_HOME/venv/bin/python3" "$SCRIPT_DIR/analyze.py" chart 2>>"$LOG_DIR/evening.log")
  if [[ -f "$CHART_PNG" ]]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto" \
      -F "chat_id=${TELEGRAM_CHAT_ID}" \
      -F "photo=@${CHART_PNG}" > /dev/null \
      || echo "$(now) weekly chart failed to send (message itself went out fine)" >&2
  else
    echo "$(now) weekly chart was not rendered (message itself went out fine)" >&2
  fi
fi

echo "$(now) $MODE check-in complete"
