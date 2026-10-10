#!/usr/bin/env python3
"""Checks that this machine is set up to run the coach, and says how to fix
whatever isn't. Run it after setup and whenever something stops arriving.

  doctor.py          everything relevant to the integrations switched on
  doctor.py --json   machine-readable

It deliberately never starts `claude` itself: any claude session on a
machine with the Telegram plugin enabled connects to Telegram and kills the
live bot's poller (docs/deployment.md "Two processes, one bot token"). The
connectors are checked by asking the coach in a session instead - see
references/setup.md, step 7.

Standard library only.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import coachconf as cc

OK, WARN, FAIL = "ok", "warn", "fail"
results = []


def report(status, what, fix=""):
    results.append({"status": status, "check": what, "fix": fix})


def read_env(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def claude_json():
    try:
        return json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return {}


def mcp_registered(name):
    d = claude_json()
    if name in (d.get("mcpServers") or {}):
        return True
    return any(name in (p.get("mcpServers") or {})
               for p in (d.get("projects") or {}).values())


def check_basics():
    if sys.version_info >= (3, 11):
        report(OK, f"python {sys.version.split()[0]}")
    else:
        report(FAIL, f"python {sys.version.split()[0]} is too old",
               "install Python 3.11 or newer (tomllib)")

    for tool in ("curl", "git"):
        if shutil.which(tool):
            report(OK, f"{tool} installed")
        else:
            report(FAIL, f"{tool} not installed", f"install {tool}")

    env = read_env(cc.HOME / ".env")
    claude = env.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    claude = os.path.expandvars(claude.replace("$HOME", str(Path.home())))
    if os.access(claude, os.X_OK):
        report(OK, f"claude binary at {claude}")
    else:
        report(FAIL, "claude binary not found",
               "install Claude Code, or set CLAUDE_BIN in .env to its absolute path")

    link = Path.home() / ".claude" / "skills" / "running-coach"
    if link.exists() and link.resolve() == cc.SKILL_DIR.resolve():
        report(OK, "skill installed at ~/.claude/skills/running-coach")
    elif link.exists():
        report(FAIL, f"~/.claude/skills/running-coach points elsewhere ({link.resolve()})",
               f"ln -sfn {cc.SKILL_DIR} {link}")
    else:
        report(FAIL, "skill not installed for Claude Code",
               f"mkdir -p ~/.claude/skills && ln -s {cc.SKILL_DIR} {link}")


def check_config():
    if not cc.exists():
        report(FAIL, "no athlete/config.toml",
               "start `claude` in the coach folder and say \"set me up\"")
        return False
    try:
        cc.load()
    except Exception as e:
        report(FAIL, f"athlete/config.toml does not parse: {e}", "fix the TOML syntax")
        return False
    gaps = cc.missing()
    if gaps:
        report(FAIL, "athlete config incomplete: " + ", ".join(gaps),
               "finish the setup, or fill these in athlete/config.toml")
    else:
        report(OK, "athlete config complete")
    for name in ("profile.md", "plan.md"):
        if (cc.ATHLETE / name).exists():
            report(OK, f"athlete/{name} present")
        else:
            report(FAIL, f"athlete/{name} missing", "run the setup")

    tz = cc.get("athlete.timezone")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            want = datetime.now(ZoneInfo(tz)).utcoffset()
            have = datetime.now().astimezone().utcoffset()
            if want == have:
                report(OK, f"system clock is on {tz} time")
            else:
                report(FAIL if cc.enabled("telegram") else WARN,
                       f"system timezone (UTC{_fmt(have)}) differs from {tz} (UTC{_fmt(want)})",
                       "cron fires in system time - set it, e.g. "
                       f"`sudo timedatectl set-timezone {tz}` on Linux")
        except Exception as e:
            report(FAIL, f"athlete.timezone '{tz}' is not a valid IANA zone ({e})",
                   "use a name like Europe/Berlin or America/New_York")
    return True


def _fmt(delta):
    mins = int(delta.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    return f"{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def check_integrations():
    if cc.enabled("strava"):
        if mcp_registered("strava-mcp"):
            report(OK, "strava-mcp server registered")
        else:
            report(WARN if not cc.enabled("telegram") else FAIL,
                   "no MCP server named strava-mcp",
                   "claude mcp add --scope user --transport http strava-mcp "
                   "https://mcp.strava.com/mcp   then authenticate it via /mcp "
                   "(the post-run watcher needs exactly this name)")
    if cc.enabled("hevy"):
        if mcp_registered("hevy"):
            report(OK, "hevy MCP server registered")
        else:
            report(FAIL, "no MCP server named hevy",
                   "claude mcp add --scope user hevy -e HEVY_API_KEY=<key> -- npx -y hevy-mcp")
    if cc.enabled("google_calendar"):
        if cc.get("calendar.training"):
            report(OK, f"training calendar set: {cc.get('calendar.training')}")
        else:
            report(FAIL, "calendar.training not set",
                   "create a calendar for the plan in Google Calendar and put its name in config")

    env = read_env(cc.HOME / ".env")
    if cc.enabled("telegram"):
        envfile = cc.HOME / ".env"
        if envfile.exists() and (envfile.stat().st_mode & 0o077):
            report(WARN, ".env is readable by other users", f"chmod 600 {envfile}")
        if env.get("TELEGRAM_CHAT_ID"):
            report(OK, "TELEGRAM_CHAT_ID set")
        else:
            report(FAIL, "TELEGRAM_CHAT_ID missing from .env",
                   "message your bot, then read chat.id from "
                   "https://api.telegram.org/bot<TOKEN>/getUpdates")
        tg = read_env(Path.home() / ".claude/channels/telegram/.env")
        if tg.get("TELEGRAM_BOT_TOKEN"):
            report(OK, "Telegram bot token configured")
        else:
            report(FAIL, "no Telegram bot token",
                   "in claude: /plugin install telegram@claude-plugins-official, "
                   "then /telegram:configure <token from @BotFather>")
        if shutil.which("tmux"):
            report(OK, "tmux installed")
        else:
            report(FAIL, "tmux not installed", "install tmux (apt/brew install tmux)")
        try:
            cron = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        except FileNotFoundError:
            cron = ""
        active = "\n".join(l for l in cron.splitlines() if not l.lstrip().startswith("#"))
        if str(cc.SCRIPTS / "check_in.sh") in active:
            report(OK, "check-ins are in the crontab")
        else:
            report(FAIL, "check-ins are not in the crontab",
                   f"python3 {cc.SCRIPTS}/coach.py crontab   and install the output")
        pid = (Path.home() / ".claude/channels/telegram/bot.pid")
        try:
            os.kill(int(pid.read_text().strip()), 0)
            report(OK, "Telegram poller is running")
        except (OSError, ValueError):
            report(WARN, "Telegram poller is not running",
                   f"{cc.SCRIPTS}/ensure_channel.sh starts it (cron does this every 5 min)")
        settings = Path.home() / ".claude/settings.json"
        if "telegram_typing.py" in (settings.read_text() if settings.exists() else ""):
            report(OK, "typing-indicator hooks installed")
        else:
            report(WARN, "typing-indicator hooks not installed (optional)",
                   f"merge the output of `coach.py hooks` into {settings}")

    if cc.enabled("apple_health"):
        if env.get("HEALTH_API_KEY"):
            report(OK, "HEALTH_API_KEY set")
        else:
            report(FAIL, "HEALTH_API_KEY missing from .env",
                   "openssl rand -hex 16, put it in .env and in the app's x-api-key header")
        port = env.get("HEALTH_PORT", "5556")
        try:
            urllib.request.urlopen(f"http://localhost:{port}/ping", timeout=2)
            report(OK, f"health receiver answering on port {port}")
        except Exception:
            report(FAIL, f"health receiver not answering on port {port}",
                   f"{cc.SCRIPTS}/ensure_health_server.sh (and add it to cron)")
        days = sorted(cc.HEALTH_DIR.glob("*.json")) if cc.HEALTH_DIR.exists() else []
        if days:
            report(OK, f"health data: {len(days)} days, latest {days[-1].stem}")
        else:
            report(WARN, "no health data received yet",
                   "run 'Export now' in Health Auto Export on the iPhone")

    if cc.enabled("weekly_chart"):
        # charts.py declares matplotlib inline; uv resolves and caches it. The
        # first run downloads it, so this check also warms the cache.
        charts = str(cc.SCRIPTS / "charts.py")
        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
        if Path(uv).exists():
            cmd, how = [uv, "run", "--quiet", "--script", charts, "check"], "uv"
        elif cc.VENV_PY.exists():
            cmd, how = [str(cc.VENV_PY), charts, "check"], "venv"
        else:
            cmd = None
        if cmd is None:
            report(FAIL, "charts: uv not installed",
                   "curl -LsSf https://astral.sh/uv/install.sh | sh")
        else:
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0:
                report(OK, f"charts: {r.stdout.strip()} via {how}")
            else:
                report(FAIL, f"charts: matplotlib does not load via {how}",
                       (r.stderr.strip().splitlines() or ["no output"])[-1])

    if cc.enabled("voice"):
        for tool in ("ffmpeg",):
            report(OK if shutil.which(tool) else FAIL, f"{tool} " +
                   ("installed" if shutil.which(tool) else "not installed"),
                   "" if shutil.which(tool) else f"install {tool}")
        wb = cc.HOME / "vendor/whisper.cpp/build/bin/whisper-cli"
        report(OK if wb.exists() else FAIL, "whisper.cpp " + ("built" if wb.exists() else "missing"),
               "" if wb.exists() else "see docs/deployment.md \"Voice messages\"")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    check_basics()
    if check_config():
        check_integrations()

    if a.json:
        print(json.dumps(results, indent=2))
    else:
        mark = {OK: "  ok ", WARN: " warn", FAIL: " FAIL"}
        for r in results:
            print(f"{mark[r['status']]}  {r['check']}")
            if r["fix"] and r["status"] != OK:
                print(f"         -> {r['fix']}")
        fails = sum(r["status"] == FAIL for r in results)
        warns = sum(r["status"] == WARN for r in results)
        print(f"\n{fails} problem(s), {warns} warning(s)")
    sys.exit(1 if any(r["status"] == FAIL for r in results) else 0)


if __name__ == "__main__":
    main()
