#!/usr/bin/env python3
"""The coach's own control panel: config, setup and rendered deployment files.

  check     Exit 0 when athlete/config.toml exists and is complete, 1 if not
  show      Compact summary of the athlete config, today's plan week, paths
  get KEY   One config value (dotted, e.g. physiology.easy_ceiling) for shell
  init      Create athlete/, state files and .env from the templates
  week      Plan week for a date: number, down week or not, days to race
  prompt    Render a scheduled check-in prompt (morning, evening, postrun)
  crontab   Render the crontab for this machine from the config
  hooks     Render the Claude Code settings.json hook block (Telegram typing)

Standard library only (Python 3.11+).
"""

import argparse
import json
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

import coachconf as cc

TEMPLATES = cc.SKILL_DIR / "templates"
PROMPTS = cc.SKILL_DIR / "prompts"


# --------------------------------------------------------------------------
# check / show / get
# --------------------------------------------------------------------------


def cmd_check(args):
    if not cc.exists():
        print(f"no athlete config at {cc.CONFIG_PATH} - run the setup "
              "(references/setup.md)")
        return 1
    try:
        cc.load()
    except Exception as e:  # tomllib.TOMLDecodeError and friends
        print(f"athlete/config.toml does not parse: {e}")
        return 1
    gaps = cc.missing()
    if gaps:
        print("athlete config incomplete, missing: " + ", ".join(gaps))
        return 1
    for name in ("profile.md", "plan.md"):
        if not (cc.ATHLETE / name).exists():
            print(f"athlete/{name} is missing - run the setup")
            return 1
    print("ok")
    return 0


def plan_week(day=None):
    """(week number, total, is_down_week) or None outside the plan."""
    start = cc.get("goal.plan_start")
    if not start:
        return None
    day = day or date.today()
    start = date.fromisoformat(str(start))
    start -= timedelta(days=start.weekday())
    n = (day - start).days // 7 + 1
    total = int(cc.get("goal.plan_weeks") or 0)
    every = int(cc.get("goal.down_week_every") or 0)
    if n < 1 or (total and n > total):
        return None
    # The last weeks of a plan are the taper, not a down week; the plan
    # file says what they are.
    down = bool(every) and n % every == 0 and (not total or n <= total - 2)
    return n, total, down


def week_line(day=None):
    day = day or date.today()
    w = plan_week(day)
    race = cc.get("goal.date")
    days_to = (date.fromisoformat(str(race)) - day).days if race else None
    if not w:
        out = "outside the plan"
    else:
        n, total, down = w
        out = f"W{n}/{total}" + (" (down week)" if down else "")
    if days_to is not None and days_to >= 0:
        out += f", {days_to} days to {cc.get('goal.race')}"
    return out


def cmd_show(args):
    if not cc.exists():
        print(f"no athlete config yet ({cc.CONFIG_PATH}) - run the setup")
        return 1
    c = cc.load()
    a, g, p, s = c["athlete"], c["goal"], c["physiology"], c["schedule"]
    on = [k for k, v in c["integrations"].items() if v]
    off = [k for k, v in c["integrations"].items() if not v]
    lines = [
        f"athlete   {a['name']} ({a['pronouns']}), writes in {a['language']}, "
        f"tz {a['timezone'] or '?'}",
        f"home      {c['home']['place'] or '?'}",
        f"goal      {g['race']} on {g['date']}"
        + (f", target {g['target_time']}" if g["target_time"] else ""),
        f"today     {date.today().isoformat()} ({date.today():%A}), {week_line()}",
        f"numbers   HRmax {p['hrmax']}, threshold {p['threshold_hr']}, "
        f"easy ceiling {p['easy_ceiling']}, VDOT {p['vdot']}",
        f"week      plan on {s['plan_day']}, close out on {s['closeout_day']}; "
        f"runs {s['weekday_run_time']}, long run {s['long_run_time']}",
        f"calendar  training '{c['calendar']['training'] or '-'}', personal "
        f"{', '.join(c['calendar']['personal']) or '-'}"
        + (f", never write: {', '.join(c['calendar']['never_write'])}"
           if c["calendar"]["never_write"] else ""),
        f"on        {', '.join(on) or '-'}",
        f"off       {', '.join(off) or '-'}",
        f"files     {cc.ATHLETE}/{{profile.md,plan.md}}, state in {cc.STATE}",
        f"scripts   {cc.SCRIPTS}",
    ]
    if args.json:
        print(json.dumps({"config": c, "week": week_line(),
                          "home": str(cc.HOME), "scripts": str(cc.SCRIPTS)}))
    else:
        print("\n".join(lines))
    return 0


def cmd_get(args):
    v = cc.get(args.key)
    if v is None:
        return 1
    if isinstance(v, list):
        print(" ".join(str(x) for x in v))
    elif isinstance(v, bool):
        print("true" if v else "false")
    else:
        print(v)
    return 0


def cmd_week(args):
    day = date.fromisoformat(args.date) if args.date else date.today()
    w = plan_week(day)
    if args.json:
        print(json.dumps({"date": day.isoformat(), "week": w[0] if w else None,
                          "of": w[1] if w else None, "down": w[2] if w else None}))
    else:
        print(week_line(day))
    return 0


# --------------------------------------------------------------------------
# init
# --------------------------------------------------------------------------


def cmd_init(args):
    made = []
    cc.ATHLETE.mkdir(parents=True, exist_ok=True)
    for name in ("config.toml", "profile.md", "plan.md"):
        dst = cc.ATHLETE / name
        if not dst.exists():
            shutil.copy(TEMPLATES / name, dst)
            made.append(f"athlete/{name}")
    gi = cc.ATHLETE / ".gitignore"
    if not gi.exists():
        shutil.copy(TEMPLATES / "athlete.gitignore", gi)
        made.append("athlete/.gitignore")
    cc.STATE.mkdir(parents=True, exist_ok=True)
    for f in (cc.CHECKIN_LOG, cc.WEEKLY_LOG, cc.NIGGLES):
        if not f.exists():
            f.touch()
            made.append(str(f.relative_to(cc.HOME)))
    for d in (cc.LOGS, cc.OUTBOX):
        d.mkdir(exist_ok=True)
    env = cc.HOME / ".env"
    if not env.exists():
        shutil.copy(TEMPLATES / "env.example", env)
        env.chmod(0o600)
        made.append(".env")
    print("created: " + (", ".join(made) if made else "nothing, all present"))
    return 0


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

# A block whose body holds no further opening tag - i.e. an innermost one.
COND_RE = re.compile(
    r"\{\{#(if|unless) (\w+)\}\}((?:(?!\{\{#).)*?)\{\{/\1\}\}", re.S)


def render(text, flags, values):
    """Tiny template language: {{#if flag}}...{{/if}} and {{#unless flag}}...
    {{/unless}}, nestable, then {placeholders}. Unknown placeholders are left
    as they are rather than raising, so a prompt override can't crash cron."""
    def cond(m):
        on = flags.get(m.group(2), False)
        keep = on if m.group(1) == "if" else not on
        return m.group(3) if keep else ""

    # Resolve innermost blocks first until nothing is left to resolve.
    while True:
        new = COND_RE.sub(cond, text)
        if new == text:
            break
        text = new

    class Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    text = text.format_map(Keep(values))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def prompt_values(extra=None):
    c = cc.load()
    w = plan_week()
    values = {
        "name": c["athlete"]["name"] or "the athlete",
        "pronouns": c["athlete"]["pronouns"],
        "language": c["athlete"]["language"],
        "scripts": str(cc.SCRIPTS),
        "athlete_dir": str(cc.ATHLETE),
        "training_calendar": c["calendar"]["training"] or "the training calendar",
        "personal_calendars": ", ".join(c["calendar"]["personal"]) or "the personal calendar",
        "plan_day": c["schedule"]["plan_day"],
        "closeout_day": c["schedule"]["closeout_day"],
        "easy_ceiling": c["physiology"]["easy_ceiling"],
        "down_week_every": c["goal"]["down_week_every"],
        "week": week_line(),
        "today": f"{date.today():%A %d %B %Y}",
    }
    values.update(c["messages"])
    values.update(extra or {})
    flags = dict(c["integrations"])
    today = f"{date.today():%A}".lower()
    flags["plan_day"] = today == str(c["schedule"]["plan_day"]).lower()
    flags["closeout_day"] = today == str(c["schedule"]["closeout_day"]).lower()
    flags["down_week"] = bool(w and w[2])
    return values, flags


def cmd_prompt(args):
    name = f"{args.mode}.md"
    src = cc.ATHLETE / "prompts" / name
    if not src.exists():
        src = PROMPTS / name
    values, flags = prompt_values({
        "activity_id": args.activity or "",
        "sport_type": args.sport or "unknown",
    })
    print(render(src.read_text(encoding="utf-8"), flags, values), end="")
    return 0


# --------------------------------------------------------------------------
# crontab / hooks
# --------------------------------------------------------------------------


def hm(value):
    h, m = str(value).split(":")
    return int(h), int(m)


def cmd_crontab(args):
    home, scripts, logs = cc.HOME, cc.SCRIPTS, cc.LOGS
    s = cc.load()["schedule"]
    tz = cc.get("athlete.timezone") or "your timezone"
    mh, mm = hm(s["morning"])
    eh, em = hm(s["evening"])
    tg = cc.enabled("telegram")
    out = [
        "# Running coach - generated by `coach.py crontab`. Install with",
        "#   coach.py crontab | crontab -      (replaces your crontab!)",
        "# or paste into `crontab -e`.",
        "#",
        f"# Cron uses the machine's local time; it must match {tz} or the",
        "# check-ins fire at the wrong hour. `doctor.py` checks this.",
        "",
        "PATH=" + ":".join([str(Path.home() / ".local/bin"), str(Path.home() / ".bun/bin"),
                            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]),
        "SHELL=/bin/bash",
        f"COACH_HOME={home}",
        "",
    ]
    if tg:
        out += [
            "# Scheduled check-ins. The evening run also plans the week on "
            f"{s['plan_day']} and closes it out on {s['closeout_day']}.",
            f"{mm} {mh} * * *  {scripts}/check_in.sh morning >> {logs}/morning.log 2>&1",
            f"{em} {eh} * * *  {scripts}/check_in.sh evening >> {logs}/evening.log 2>&1",
            "",
        ]
        if cc.enabled("strava"):
            out += [
                "# Post-run reactions: cheap Strava poll, full reaction only on "
                "a new activity.",
                f"*/30 {s['postrun_hours']} * * * {scripts}/post_run_watch.sh "
                f">> {logs}/postrun.log 2>&1",
                "",
            ]
        out += [
            "# Watchdog for the persistent Telegram conversation session.",
            f"@reboot     {scripts}/ensure_channel.sh >> {logs}/channel.log 2>&1",
            f"*/5 * * * * {scripts}/ensure_channel.sh >> {logs}/channel.log 2>&1",
            "",
        ]
    else:
        out += ["# Telegram is off in athlete/config.toml, so there are no",
                "# scheduled check-ins - the coach is conversation-only.", ""]
    if cc.enabled("apple_health"):
        out += [
            "# Watchdog for the Health Auto Export receiver.",
            f"@reboot     {scripts}/ensure_health_server.sh >> {logs}/health.log 2>&1",
            f"*/5 * * * * {scripts}/ensure_health_server.sh >> {logs}/health.log 2>&1",
            "",
        ]
    backup = "" if cc.backup_repo() else "# "
    out += [
        "# Nightly backup of athlete/ to a private git remote "
        "(docs/deployment.md \"Backup\").",
        f"{backup}30 3 * * *  {scripts}/backup.sh >> {logs}/backup.log 2>&1",
        "",
        "# Keep logs from growing without bound.",
        f"0 4 1 * *   find {logs} -name '*.log' -size +10M -delete",
    ]
    print("\n".join(out))
    return 0


def cmd_hooks(args):
    typing = f"python3 {cc.SCRIPTS}/telegram_typing.py"

    def hook(cmd, is_async):
        h = {"type": "command", "command": cmd, "timeout": 10}
        if is_async:
            h["async"] = True
        return [{"hooks": [h]}]

    print(json.dumps({
        "hooks": {
            "UserPromptSubmit": hook(f"{typing} start", True),
            "PreToolUse": hook(f"{typing} start", True),
            "Stop": hook(f"{typing} stop", False),
            "SessionEnd": hook(f"{typing} stop", False),
        },
        "enabledPlugins": {"telegram@claude-plugins-official": True},
    }, indent=2))
    return 0


# --------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(fn=cmd_check)
    s = sub.add_parser("show")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("get")
    s.add_argument("key")
    s.set_defaults(fn=cmd_get)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    s = sub.add_parser("week")
    s.add_argument("--date")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_week)
    s = sub.add_parser("prompt")
    s.add_argument("mode", choices=["morning", "evening", "postrun"])
    s.add_argument("--activity")
    s.add_argument("--sport")
    s.set_defaults(fn=cmd_prompt)
    sub.add_parser("crontab").set_defaults(fn=cmd_crontab)
    sub.add_parser("hooks").set_defaults(fn=cmd_hooks)

    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
