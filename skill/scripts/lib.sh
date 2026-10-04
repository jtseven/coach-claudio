# Shared by every shell script here - source it, don't run it.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
#
# Gives you: SCRIPT_DIR, COACH_HOME, ATHLETE_DIR, STATE_DIR, LOG_DIR,
# OUTBOX_DIR, CLAUDE_BIN, the secrets from .env and the Telegram plugin's
# .env, and the helpers now, run_timeout, coach_get, diagnose, alert.
#
# Written for bash on Linux and macOS alike: no `date -Is`, no `flock`, no
# bare `timeout`, no `nproc` - each of those is GNU-only.

# Resolve the real script dir even when the skill is reached through the
# ~/.claude/skills symlink.
_lib_src="${BASH_SOURCE[0]}"
while [[ -L "$_lib_src" ]]; do
  _lib_dir="$(cd "$(dirname "$_lib_src")" && pwd)"
  _lib_src="$(readlink "$_lib_src")"
  [[ "$_lib_src" != /* ]] && _lib_src="$_lib_dir/$_lib_src"
done
SCRIPT_DIR="$(cd "$(dirname "$_lib_src")" && pwd -P)"
unset _lib_src _lib_dir

COACH_HOME="${COACH_HOME:-${RUNNING_COACH_HOME:-$(cd "$SCRIPT_DIR/../.." && pwd -P)}}"
export COACH_HOME
ATHLETE_DIR="$COACH_HOME/athlete"
STATE_DIR="$ATHLETE_DIR/state"
LOG_DIR="$COACH_HOME/logs"
OUTBOX_DIR="$COACH_HOME/outbox"
TELEGRAM_ENV="${TELEGRAM_ENV:-$HOME/.claude/channels/telegram/.env}"

# Secrets: chat id, health key and CLAUDE_BIN in the coach's own .env; the bot
# token lives with the Telegram plugin and is deliberately not duplicated.
# shellcheck disable=SC1091
[[ -f "$COACH_HOME/.env" ]] && source "$COACH_HOME/.env"
# shellcheck disable=SC1090
[[ -f "$TELEGRAM_ENV" ]] && source "$TELEGRAM_ENV"

# Cron's PATH is minimal and will not contain the claude binary.
export PATH="$HOME/.local/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"

mkdir -p "$LOG_DIR" "$OUTBOX_DIR"

# ISO-8601 timestamp, portable (BSD date has no -I).
now() { date +%Y-%m-%dT%H:%M:%S%z; }

# run_timeout SECONDS cmd... - GNU timeout, Homebrew's gtimeout, or (last
# resort) no timeout at all. Exit 124 means it timed out.
run_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    "$@"
  fi
}

# coach_get KEY - one value from athlete/config.toml (empty if unset).
coach_get() {
  python3 "$SCRIPT_DIR/coach.py" get "$1" 2>/dev/null
}

# A failed claude run should become something the athlete can act on from
# their phone, sent with a plain curl that doesn't depend on claude working.
# Without this, five days of check-ins once failed on an expired login with
# every warning saying just "exit 1".

# diagnose <text...> - prints a one-line cause + fix, or nothing if unknown.
diagnose() {
  local text="$*"
  case "$text" in
    *"OAuth session expired"*|*"Failed to authenticate"*|*"Invalid API key"*|*"Please run /login"*)
      echo "Claude login expired. Fix: on the coach machine run 'claude', then /login." ;;
    *"strava"*"failed to connect"*|*"strava"*"503"*|*"strava"*"no healthy upstream"*)
      echo "Strava connector was unreachable - usually transient on Strava's side; re-authenticate it with /mcp if it persists." ;;
    *"rate limit"*|*"usage limit"*|*"429"*)
      echo "Claude usage limit reached - it clears on its own." ;;
    *"ENOTFOUND"*|*"getaddrinfo"*|*"Could not resolve host"*|*"network"*"unreachable"*)
      echo "The coach machine had no network." ;;
  esac
}

# alert <text> - one Telegram message to the athlete. Needs TELEGRAM_BOT_TOKEN
# and TELEGRAM_CHAT_ID; logs and returns 1 otherwise.
alert() {
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "$(now) alert not sent (no Telegram credentials): $1" >&2
    return 1
  fi
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$1" > /dev/null \
    || { echo "$(now) alert also failed to send" >&2; return 1; }
}
