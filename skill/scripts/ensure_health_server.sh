#!/usr/bin/env bash
# Watchdog for the Health Auto Export receiver, same cron pattern as
# ensure_channel.sh: @reboot and every 5 minutes.
#
# Liveness is checked by actually hitting the port, not with pgrep - a
# pgrep -f for the script name matches any unrelated process whose command
# line merely mentions it (an editor, a shell running a compound command),
# which made the watchdog believe the server was up when it had never
# started. The server itself exits 0 when the port is already bound, so
# even a racing double-start is harmless.

set -uo pipefail

# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ "$(coach_get integrations.apple_health)" == "true" ]] || exit 0

PORT="${HEALTH_PORT:-5556}"
curl -s -m 2 "http://localhost:$PORT/ping" > /dev/null && exit 0

echo "$(now) health server not running - starting it"
nohup python3 "$SCRIPT_DIR/health_server.py" >> "$LOG_DIR/health.log" 2>&1 &
