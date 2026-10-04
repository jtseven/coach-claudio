#!/usr/bin/env bash
# Keeps the persistent Telegram channel session alive in a detached tmux
# session. This is the process that listens for inbound Telegram messages and
# replies live - it must never coexist with a second poller (Telegram allows
# exactly one getUpdates consumer per bot token; a second one silently kills
# the first). Run via cron @reboot and every few minutes as a watchdog.
#
# Deliberately does NOT run the scheduled check-ins (check_in.sh) - those turn
# the telegram plugin off for their own session so they never contend for the
# polling slot with this one. See docs/deployment.md "Two processes, one bot
# token".
#
# Five failure modes, all seen for real, five fixes, escalating:
#  1. The whole claude process dies (crash, OOM, reboot) - the tmux session
#     goes with it. Fix: start the session.
#  2. The claude process survives but its telegram MCP sub-connection dies
#     without it - typically because another session on this machine
#     connected to telegram and took the lock. The tmux pane stays alive, so
#     `tmux has-session` alone never notices. Fix: detect via bot.pid (no
#     file, or a pid that isn't live) and send /reload-plugins into the pane.
#  3. /reload-plugins doesn't bring it back - it silently no-ops whenever the
#     running session can't re-resolve the plugin, leaving the bot dead while
#     this watchdog retries forever. Fix: remember that a reload was already
#     tried, and if the poller is still dead on the next run, restart the
#     session outright.
#  4. The plugin auto-updates under a long-running session. The MCP server
#     restarts on the new version and happily polls Telegram, but the
#     session's channel binding does not survive, so inbound messages are
#     consumed and silently dropped - every health signal stays green while
#     the bot is deaf. Nothing observable about the poller distinguishes this
#     from healthy, so detect the *cause*: record the plugin version the
#     session started with and restart when the newest installed one differs.
#  5. The session starts in an untrusted directory and blocks on the "Is
#     this a project you trust?" dialog before it binds the channel. From
#     outside it looks like mode 2, and the /reload-plugins Enter then lands
#     on the dialog's default "No, exit" and kills the session - an endless
#     up/down loop. Fix: start in COACH_HOME, and trust that folder once by
#     running `claude` there interactively during setup.

set -uo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SESSION="${COACH_TMUX_SESSION:-running-coach-telegram}"
CHANNEL_DIR="$HOME/.claude/channels/telegram"
BOT_PID_FILE="$CHANNEL_DIR/bot.pid"
# telegram@claude-plugins-official must stay ENABLED globally. --channels
# resolves its provider from enabled plugins, so with the plugin disabled
# this session gets a banner and a polling MCP server but no channel binding
# - inbound messages are consumed and silently dropped. Loading it
# per-session with --plugin-dir does NOT work around that.
#
# Marks that a /reload-plugins was already attempted for the current outage,
# so the next run escalates instead of retrying the same thing forever.
RELOAD_MARKER="$CHANNEL_DIR/.reload-attempted"
# The plugin version this session was launched against - failure mode 4.
PLUGIN_CACHE="$HOME/.claude/plugins/cache/claude-plugins-official/telegram"
VERSION_FILE="$CHANNEL_DIR/.session-plugin-version"

if [[ "$(coach_get integrations.telegram)" != "true" ]]; then
  exit 0   # conversation over Telegram is off - nothing to keep alive
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "$(now) FATAL: tmux is not installed." >&2
  exit 127
fi

newest_plugin_version() {
  ls -1 "$PLUGIN_CACHE" 2>/dev/null | sort -t. -k1,1n -k2,2n -k3,3n | tail -1
}

start_session() {
  if [[ ! -x "$CLAUDE_BIN" ]]; then
    echo "$(now) FATAL: claude binary not found at '$CLAUDE_BIN'." >&2
    exit 127
  fi
  mkdir -p "$CHANNEL_DIR"
  rm -f "$RELOAD_MARKER"
  newest_plugin_version > "$VERSION_FILE"
  tmux new-session -d -s "$SESSION" -c "$COACH_HOME" \
    "$CLAUDE_BIN --channels plugin:telegram@claude-plugins-official"
}

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "$(now) $SESSION not running - starting it"
  start_session
  exit 0
fi

# Session's alive. Before trusting the poller, check whether the plugin has
# been updated underneath it (failure mode 4).
current_version="$(newest_plugin_version)"
session_version="$(cat "$VERSION_FILE" 2>/dev/null)"
if [[ -n "$current_version" && -n "$session_version" \
      && "$current_version" != "$session_version" ]]; then
  echo "$(now) telegram plugin updated ($session_version -> $current_version) - restarting $SESSION to rebind the channel"
  tmux kill-session -t "$SESSION" 2>/dev/null
  sleep 2
  start_session
  exit 0
fi

# Now the thing that usually matters: is something holding the Telegram poll
# lock, and is it live?
poller_pid=""
[[ -f "$BOT_PID_FILE" ]] && poller_pid="$(cat "$BOT_PID_FILE" 2>/dev/null)"

if [[ -n "$poller_pid" ]] && kill -0 "$poller_pid" 2>/dev/null; then
  rm -f "$RELOAD_MARKER"   # healthy - clear any past outage state
  exit 0
fi

if [[ -f "$RELOAD_MARKER" ]]; then
  echo "$(now) poller still dead after /reload-plugins - restarting $SESSION"
  tmux kill-session -t "$SESSION" 2>/dev/null
  sleep 2
  start_session
  exit 0
fi

echo "$(now) $SESSION is up but telegram poller is dead (bot.pid: '${poller_pid:-none}') - reconnecting"
touch "$RELOAD_MARKER"
# C-u first: inbound Telegram messages transit through the pane's input box,
# so it may hold a queued one, and send-keys would otherwise append the slash
# command to it and submit the concatenation as a chat message. Discarding it
# is the right trade: the poller is already dead, so a message sitting there
# arrived before the outage and won't be answered either way.
tmux send-keys -t "$SESSION" C-u
tmux send-keys -t "$SESSION" "/reload-plugins" Enter
