#!/usr/bin/env bash
# Scheduled generation and durable, acknowledged delivery.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
MODE="${1:-}"; shift || true
POSTRUN_ID=""; POSTRUN_TYPE="unknown"
case "$MODE" in
  morning|evening) ;;
  postrun)
    POSTRUN_ID="${1:-}"; POSTRUN_TYPE="${2:-unknown}"
    [[ "$POSTRUN_TYPE" == --* ]] && POSTRUN_TYPE="unknown"
    [[ "$POSTRUN_ID" =~ ^[0-9]+$ ]] || { echo 'postrun requires a numeric activity id' >&2; exit 64; }
    ;;
  *) echo 'usage: check_in.sh morning|evening|postrun <id> [sport] [--no-send|--print-prompt]' >&2; exit 64 ;;
esac
PRINT_PROMPT=0; NO_SEND=0
for arg in "$@"; do
  case "$arg" in
    --print-prompt) PRINT_PROMPT=1 ;;
    --no-send) NO_SEND=1 ;;
  esac
done
python3 "$SCRIPT_DIR/coach.py" check >/dev/null || exit 78
PROMPT_ARGS=("$MODE")
[[ -n "$POSTRUN_ID" ]] && PROMPT_ARGS+=(--activity "$POSTRUN_ID" --sport "$POSTRUN_TYPE")
PROMPT=$(python3 "$SCRIPT_DIR/coach.py" prompt "${PROMPT_ARGS[@]}") || exit 78
[[ "$PRINT_PROMPT" -eq 0 ]] || { echo "$PROMPT"; exit 0; }
[[ -x "$CLAUDE_BIN" ]] || { echo 'Claude executable missing' >&2; exit 127; }
if [[ "$NO_SEND" -eq 1 ]]; then
  export COACH_DRY_RUN=1
else
  [[ "$(coach_get integrations.telegram)" == true && -n "${TELEGRAM_CHAT_ID:-}" && -n "${TELEGRAM_BOT_TOKEN:-}" ]] || {
    echo 'Telegram is disabled or unconfigured' >&2; exit 78;
  }
fi
export CLAUDE_BIN TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
python3 "$SCRIPT_DIR/delivery.py" checkin "$MODE" "$POSTRUN_ID" "$POSTRUN_TYPE" "$NO_SEND" <<< "$PROMPT"
STATUS=$?
[[ "$STATUS" -eq 0 ]] || exit "$STATUS"
[[ "$NO_SEND" -eq 0 ]] || exit 0

# Weekly charts remain best effort; text delivery is authoritative.
lower() { tr '[:upper:]' '[:lower:]'; }
if [[ "$MODE" == evening && "$(coach_get integrations.weekly_chart)" == true \
      && "$(LC_ALL=C date +%A | lower)" == "$(coach_get schedule.closeout_day | lower)" ]]; then
  PNGS=()
  for chart in weekly load aerobic; do
    png=$(charts "$chart" 2>>"$LOG_DIR/evening.log") && [[ -f "$png" ]] && PNGS+=("$png")
  done
  if [[ ${#PNGS[@]} -eq 1 ]]; then
    python3 "$SCRIPT_DIR/delivery.py" telegram sendPhoto \
      -F "chat_id=${TELEGRAM_CHAT_ID}" -F "photo=@${PNGS[0]}" || echo 'Weekly chart delivery failed' >&2
  elif [[ ${#PNGS[@]} -gt 1 ]]; then
    MEDIA=""; FORM=()
    for i in "${!PNGS[@]}"; do
      MEDIA+="${MEDIA:+,}{\"type\":\"photo\",\"media\":\"attach://c$i\"}"
      FORM+=(-F "c$i=@${PNGS[$i]}")
    done
    python3 "$SCRIPT_DIR/delivery.py" telegram sendMediaGroup \
      -F "chat_id=${TELEGRAM_CHAT_ID}" -F "media=[$MEDIA]" "${FORM[@]}" || echo 'Weekly charts delivery failed' >&2
  fi
fi
echo "$(now) $MODE check-in complete"
