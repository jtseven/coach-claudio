#!/usr/bin/env bash
# Poll discovery and drain the durable per-activity queue, including retries.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export CLAUDE_BIN TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
exec python3 "$SCRIPT_DIR/delivery.py" watch
