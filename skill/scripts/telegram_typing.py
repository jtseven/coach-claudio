#!/usr/bin/env python3
"""Keeps Telegram's "typing..." indicator alive while the coach is working.

The channel plugin sends `sendChatAction` exactly once when a message
arrives (server.ts, handleInbound). Telegram shows that for about five
seconds - but a real coaching turn takes 30-120s while it pulls Strava, the
calendar and Hevy, so from the athlete's side the chat looks dead for most of
the wait. Telegram has no "typing until further notice"; the only way is to
re-send the action every few seconds, which is what the daemon below does.

Driven by Claude Code hooks (~/.claude/settings.json), NOT by patching the
plugin - the plugin lives in a version-pinned cache directory that is
replaced wholesale on every update (0.0.6 -> 0.0.7 happened mid-project), so
a patch there would silently disappear.

  start   decide whether this turn is a Telegram turn; if so, spawn the
          daemon. Idempotent - safe to call on every single tool use.
  stop    kill the daemon for this session.

Both modes read the hook payload as JSON on stdin and always exit 0: a
broken indicator must never break a coaching reply.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import coachconf as cc  # noqa: E402

STATE_DIR = str(cc.STATE)
TELEGRAM_ENV = os.path.expanduser("~/.claude/channels/telegram/.env")

INTERVAL = 4.0        # Telegram's action lasts ~5s; refresh just inside that
MAX_LIFETIME = 900.0  # 15 min, matching check_in.sh's timeout - a missed
                      # Stop hook must not leave the bot typing forever
CHANNEL_RE = re.compile(r'<channel\s+source="plugin:telegram:telegram"[^>]*chat_id="(\d+)"')


def bot_token():
    """Read TELEGRAM_BOT_TOKEN out of the channel plugin's .env."""
    try:
        with open(TELEGRAM_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN"):
                    _, _, val = line.partition("=")
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def pid_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "default")[:64] or "default"
    return os.path.join(STATE_DIR, f"typing-{safe}.pid")


def read_hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}


# --------------------------------------------------------------------------
# is this turn driven by a Telegram message?
# --------------------------------------------------------------------------


def last_user_text(transcript_path):
    """Content of the most recent real user input in the transcript.

    Only `type: "user"` entries count, and only those carrying actual input:
    a tool result is also stored as type "user" (content is a list of
    tool_result blocks) and must not be mistaken for the person typing,
    otherwise every tool call would look like a fresh user turn.
    """
    try:
        with open(transcript_path, errors="replace") as fh:
            rows = fh.readlines()
    except OSError:
        return None

    for line in reversed(rows):
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if d.get("type") != "user":
            continue
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
            # Pure tool_result turn - keep looking further back.
    return None


def chat_id_for_turn(hook):
    """The chat to type into, or None if this isn't a Telegram turn.

    UserPromptSubmit carries the prompt directly; every other event has to
    go via the transcript. Both are checked so the hook works no matter
    which event fired.
    """
    for candidate in (hook.get("prompt"), last_user_text(hook.get("transcript_path", ""))):
        if isinstance(candidate, str):
            m = CHANNEL_RE.search(candidate)
            if m:
                return m.group(1)
    return None


# --------------------------------------------------------------------------
# the daemon
# --------------------------------------------------------------------------


def run_daemon(chat_id):
    token = bot_token()
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    data = urllib.parse.urlencode({"chat_id": chat_id, "action": "typing"}).encode()
    deadline = time.monotonic() + MAX_LIFETIME
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, data=data, timeout=10).read()
        except Exception:
            # Network blips are not worth dying over; the next tick retries.
            # A dead token or a blocked bot just means no indicator.
            pass
        time.sleep(INTERVAL)


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def read_pid(path):
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def cmd_start(hook):
    path = pid_file(hook.get("session_id", ""))
    pid = read_pid(path)
    if pid and alive(pid):
        return  # already typing for this session

    chat_id = chat_id_for_turn(hook)
    if not chat_id:
        return  # a terminal turn, not a Telegram one - stay silent

    os.makedirs(STATE_DIR, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "daemon", chat_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # survives the hook process exiting
    )
    with open(path, "w") as fh:
        fh.write(str(proc.pid))


def cmd_stop(hook):
    path = pid_file(hook.get("session_id", ""))
    pid = read_pid(path)
    if pid and alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "daemon":
        run_daemon(sys.argv[2])
        return
    if mode == "start":
        cmd_start(read_hook_input())
    elif mode == "stop":
        cmd_stop(read_hook_input())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a hook must never break the session
    sys.exit(0)
