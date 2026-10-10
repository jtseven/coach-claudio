"""Shared paths and athlete config for every coach script.

Standard library only. Needs Python 3.11+ for tomllib.

Everything personal lives in one place, `athlete/config.toml`, and every script
reads it through here instead of hardcoding a heart rate, a city or a path.
The layout, relative to the coach home (the repo checkout):

    skill/                 the engine - shared, never edited per athlete
    athlete/config.toml    structured settings: zones, goal, schedule, ...
    athlete/profile.md     the athlete in prose: history, numbers, quirks
    athlete/plan.md        the season's phase map and weekly targets
    athlete/prompts/       optional overrides for the check-in prompts
    athlete/state/         the coach's memory: logs, niggles, health data
    .env                   secrets (Telegram chat id, health API key)

The coach home is found from this file's location (resolving the
~/.claude/skills symlink), so a checkout works wherever it is cloned.
COACH_HOME overrides it; tests use that to run against a scratch copy.
"""

import copy
import os
import tomllib
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
HOME = Path(
    os.environ.get("COACH_HOME")
    or os.environ.get("RUNNING_COACH_HOME")
    or SKILL_DIR.parent
).expanduser()
ATHLETE = HOME / "athlete"
STATE = ATHLETE / "state"
CONFIG_PATH = ATHLETE / "config.toml"
LOGS = HOME / "logs"
OUTBOX = HOME / "outbox"
VENV_PY = HOME / "venv" / "bin" / "python3"
SCRIPTS = SKILL_DIR / "scripts"

CHECKIN_LOG = STATE / "checkin_log.jsonl"
WEEKLY_LOG = STATE / "weekly_log.jsonl"
NIGGLES = STATE / "niggles.jsonl"
HEALTH_DIR = STATE / "health" / "daily"

# A test run of a check-in writes to the logs exactly like a real one, and the
# next real check-in then skips whatever the test "covered". With
# COACH_DRY_RUN=1 every write to athlete/state is skipped and says so.
DRY_RUN = os.environ.get("COACH_DRY_RUN") == "1"

DEFAULTS = {
    "athlete": {
        "name": "",
        "pronouns": "they/them",
        "language": "English",
        "timezone": "",
    },
    "home": {"place": "", "lat": None, "lon": None},
    "goal": {
        "race": "",
        "date": "",
        "distance_km": 42.195,
        "target_time": "",
        "plan_start": "",
        "plan_weeks": 0,
        "down_week_every": 4,
    },
    "physiology": {
        "hrmax": None,
        "threshold_hr": None,
        "easy_ceiling": None,
        "vdot": None,
        "zones": [],
        "wpb_band": [],
    },
    "schedule": {
        "morning": "07:30",
        "evening": "21:00",
        "plan_day": "Friday",
        "closeout_day": "Sunday",
        "weekday_run_time": "18:00",
        "long_run_time": "09:00",
        "postrun_hours": "6-21",
    },
    "calendar": {
        "training": "",
        "training_id": "",
        "personal": [],
        "never_write": [],
    },
    "integrations": {
        "strava": True,
        "google_calendar": True,
        "hevy": False,
        "apple_health": False,
        "telegram": False,
        "voice": False,
        "weekly_chart": False,
    },
    "activities": {
        "commute_types": ["EBikeRide", "EMountainBikeRide"],
    },
    "messages": {
        "morning_words": "40-80",
        "evening_words": "60-120",
        "postrun_words": "60-120",
        "planning_words": "up to ~180",
        "review_words": "up to ~250",
    },
}

# Without these the coach can't do its job; `coach.py check` reports them.
REQUIRED = [
    "athlete.name",
    "athlete.language",
    "athlete.timezone",
    "goal.race",
    "goal.date",
    "physiology.hrmax",
    "physiology.easy_ceiling",
]

_cache = None


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def exists():
    return CONFIG_PATH.exists()


def load():
    """The athlete config merged over the defaults. Missing file = defaults."""
    global _cache
    if _cache is None:
        user = {}
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("rb") as fh:
                user = tomllib.load(fh)
        _cache = _merge(DEFAULTS, user)
    return _cache


def get(dotted, default=None):
    node = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def missing():
    """Required keys that are unset or empty."""
    return [k for k in REQUIRED if get(k) in (None, "", [])]


def enabled(integration):
    return bool(get(f"integrations.{integration}", False))


def easy_ceiling(fallback=None):
    v = get("physiology.easy_ceiling")
    return float(v) if v else fallback


def backup_repo():
    """Where backup.sh commits: athlete/ if it is its own repo, else the coach
    home if that is a repo tracking athlete/, else None."""
    import subprocess

    if (ATHLETE / ".git").exists():
        return ATHLETE
    try:
        inside = subprocess.run(["git", "-C", str(HOME), "rev-parse", "--git-dir"],
                                capture_output=True).returncode == 0
        ignored = subprocess.run(["git", "-C", str(HOME), "check-ignore", "-q",
                                  "athlete/config.toml"],
                                 capture_output=True).returncode == 0
    except FileNotFoundError:
        return None
    return HOME if inside and not ignored else None


def write_allowed(what):
    """False (and a note on stdout) in dry-run mode."""
    if DRY_RUN:
        print(f"dry run - {what} not written")
        return False
    STATE.mkdir(parents=True, exist_ok=True)
    return True
