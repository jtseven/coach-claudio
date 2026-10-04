#!/usr/bin/env python3
"""The coach's memory of what it has told the athlete, and of their niggles.

Standard library only.

Subcommands:
  recent    The last few check-in log entries, compact - read before writing
  covered   Exit 0 if an activity id is already in the log, 1 if not
  add       Append a check-in log entry (validates mode, caps the note)
  niggles   Open niggles: the latest line per body area, resolved ones hidden
  niggle    Record a niggle, or resolve one (--resolve)

Why a script rather than `tail` and `echo`
------------------------------------------
Every run of the coach is a fresh process, so this log is how the morning
brief knows what last night's message said. It only works if it stays cheap to
read. Left to itself, the coach grows notes into kilobytes of its own
reasoning each - one log reached 443 KB, ~25 KB pulled into context by every
`tail -10` - and the mode field drifted into three spellings of post-run.
`add` refuses an over-long note instead of trusting the instruction, and
`recent` truncates long legacy ones so history stays intact but stops costing
anything.

A note records what *the athlete was told* and anything promised for later
("cadence check tomorrow"), not how the coach got there.

Niggles are an append-only log too: the latest line per `area` is the current
state, and a `resolved` line closes one. Read through `niggles`, never by
hand-parsing the file.
"""

import argparse
import json
import sys
from datetime import datetime

import coachconf as cc

LOG = cc.CHECKIN_LOG
NIGGLES = cc.NIGGLES
MODES = ("morning", "evening", "postrun", "conversation")
# Legacy spellings seen in the log, mapped on read so `recent` is consistent.
ALIASES = {"post-run": "postrun", "post_run": "postrun"}
NOTE_MAX = 300
SHOW_MAX = 240


def entries():
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        e["mode"] = ALIASES.get(e.get("mode"), e.get("mode"))
        out.append(e)
    return out


def cmd_recent(a):
    rows = entries()[-a.n:]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("log is empty")
        return 0
    for e in rows:
        note = e.get("note", "")
        if len(note) > SHOW_MAX:
            note = note[:SHOW_MAX].rstrip() + " [...]"
        ids = ",".join(str(i) for i in e.get("activity_ids") or []) or "-"
        print(f"{e.get('ts', '?')[:16]}  {e.get('mode', '?'):<12} ids={ids}")
        print(f"    {note}")
    return 0


def cmd_covered(a):
    want = str(a.activity_id)
    for e in entries():
        if want in (str(i) for i in e.get("activity_ids") or []):
            print(f"covered: {e.get('ts', '?')[:16]} {e.get('mode')}")
            return 0
    print("not covered")
    return 1


def cmd_add(a):
    mode = ALIASES.get(a.mode, a.mode)
    if mode not in MODES:
        print(f"mode must be one of {', '.join(MODES)}", file=sys.stderr)
        return 2
    note = " ".join(a.note.split())
    if len(note) > NOTE_MAX:
        print(
            f"note is {len(note)} chars, limit {NOTE_MAX}. Write what the "
            "athlete was told and any promise for later, not the reasoning.",
            file=sys.stderr,
        )
        return 2
    ids = []
    for chunk in a.ids or []:
        ids += [int(x) for x in str(chunk).replace(",", " ").split()]
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "activity_ids": ids,
        "note": note,
    }
    if not cc.write_allowed("check-in log entry"):
        return 0
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("logged")
    return 0


def niggle_entries():
    if not NIGGLES.exists():
        return []
    out = []
    for line in NIGGLES.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def cmd_niggles(a):
    latest = {}
    for e in niggle_entries():
        area = (e.get("area") or "").strip().lower()
        if area:
            latest[area] = e
    rows = [e for e in latest.values()
            if a.all or e.get("status", "open") != "resolved"]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no open niggles")
        return 0
    for e in rows:
        print(f"{e.get('ts', '?')[:10]}  {e.get('area')}  severity "
              f"{e.get('severity', '?')}  {e.get('status', 'open')}")
        if e.get("note"):
            print(f"    {e['note']}")
    return 0


def cmd_niggle(a):
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "area": a.area.strip(),
        "status": "resolved" if a.resolve else "open",
    }
    if not a.resolve:
        if a.severity not in (1, 2, 3):
            print("--severity 1, 2 or 3 is required for an open niggle",
                  file=sys.stderr)
            return 2
        entry["severity"] = a.severity
    note = " ".join((a.note or "").split())
    if len(note) > NOTE_MAX:
        print(f"note is {len(note)} chars, limit {NOTE_MAX}", file=sys.stderr)
        return 2
    if note:
        entry["note"] = note
    if not cc.write_allowed("niggle"):
        return 0
    with NIGGLES.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print("resolved" if a.resolve else "recorded")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("recent", help="last entries, compact")
    r.add_argument("--n", type=int, default=8)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_recent)

    c = sub.add_parser("covered", help="is this activity already discussed?")
    c.add_argument("activity_id")
    c.set_defaults(fn=cmd_covered)

    d = sub.add_parser("add", help="append an entry")
    d.add_argument("--mode", required=True)
    d.add_argument("--ids", nargs="*", help="Strava activity ids discussed")
    d.add_argument("--note", required=True,
                   help=f"what the athlete was told, <= {NOTE_MAX} chars")
    d.set_defaults(fn=cmd_add)

    n = sub.add_parser("niggles", help="open niggles, latest per area")
    n.add_argument("--all", action="store_true", help="include resolved")
    n.add_argument("--json", action="store_true")
    n.set_defaults(fn=cmd_niggles)

    g = sub.add_parser("niggle", help="record or resolve a niggle")
    g.add_argument("--area", required=True, help='e.g. "left knee"')
    g.add_argument("--severity", type=int,
                   help="1 watch, 2 affects training, 3 stop and see a physio")
    g.add_argument("--note", default="")
    g.add_argument("--resolve", action="store_true")
    g.set_defaults(fn=cmd_niggle)

    a = p.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
