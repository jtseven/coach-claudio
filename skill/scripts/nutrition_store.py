"""The private food ledger: one SQLite file under athlete/state/nutrition/.

Standard library only. Every mutation is one transaction, so a meal with
five items is saved whole or not at all. Nothing is overwritten: a
correction is a new revision, a deletion is a tombstone revision, and
`erase` blanks every revision's content but keeps the id and the source
events so a redelivered message cannot bring the meal back.

Dedupe is by *source event* (e.g. `telegram:<chat_id>:<message_id>`), never
by image hash or barcode: two meals may use the same photo or product.

With dry_run every transaction is rolled back, and a ledger that doesn't
exist yet is never created.
"""

from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess

import nutrition_core as core

SCHEMA_VERSION = 1
MEALS = ("breakfast", "lunch", "dinner", "snack", "drink", "other")
ENTRY_KEYS = {"eaten_at", "meal", "note", "source_events", "items", "image_sha256"}
EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}:[A-Za-z0-9:_.\-]{1,120}$")
MAX_ITEMS = 40
FUTURE_SLACK = timedelta(minutes=15)
PARTIAL_NOTE = "partial day: logged so far, not total intake"
# Telegram caps bot downloads at 20 MB; Claude reads these four formats.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_FORMATS = (("jpeg", "jpg", (b"\xff\xd8\xff",)),
                 ("png", "png", (b"\x89PNG\r\n\x1a\n",)),
                 ("gif", "gif", (b"GIF87a", b"GIF89a")))
TOKEN_RE = re.compile(r"^i_[0-9a-f]{16}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  rev INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active', 'deleted', 'erased')),
  local_date TEXT,
  eaten_at TEXT);
CREATE INDEX IF NOT EXISTS entries_day ON entries(local_date);
CREATE TABLE IF NOT EXISTS revisions(
  entry_id TEXT NOT NULL REFERENCES entries(id),
  rev INTEGER NOT NULL,
  recorded_at TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('create', 'correct', 'delete', 'erase')),
  reason TEXT,
  payload TEXT,
  PRIMARY KEY(entry_id, rev));
CREATE TABLE IF NOT EXISTS proposals(
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending', 'confirmed', 'cancelled')),
  local_date TEXT,
  payload TEXT,
  questions TEXT,
  entry_id TEXT);
CREATE TABLE IF NOT EXISTS source_events(
  key TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('entry', 'proposal')),
  target TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS day_marks(
  local_date TEXT NOT NULL,
  marked_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('complete', 'partial')),
  note TEXT);
CREATE TABLE IF NOT EXISTS images(
  token TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  ext TEXT NOT NULL,
  staged_at TEXT NOT NULL,
  discarded_at TEXT,
  source_event TEXT,
  owner TEXT);
"""


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def merge_patch(target, patch):
    """RFC 7386 JSON merge patch, plus one extension: `items` given as an
    object keyed by index ({"0": {"amount": "300 g"}}) patches those items
    in place; `items` given as a list replaces them."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif (key == "items" and isinstance(value, dict)
              and isinstance(out.get("items"), list)):
            items = list(out["items"])
            for idx, sub in value.items():
                if not str(idx).isdigit() or int(idx) >= len(items):
                    raise core.InvalidInput(f"items.{idx}: no such item")
                items[int(idx)] = merge_patch(items[int(idx)], sub)
            out["items"] = items
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def parse_date(s, what="date"):
    try:
        return date.fromisoformat(str(s)).isoformat()
    except ValueError:
        raise core.InvalidInput(f"{what}: {s!r} is not YYYY-MM-DD") from None


class Ledger:
    def __init__(self, path, tz, dry_run=False, now=None, inbox=None):
        self.path = Path(path)
        self.inbox = Path(inbox) if inbox else self.path.parent / "inbox"
        self.tz = tz
        self.dry_run = dry_run
        self._now = now or (lambda: datetime.now(self.tz))
        self._db = None
        # item -> item, e.g. a barcode-only item filled in from a provider.
        # May raise core.MissingInfo or core.LookupFailed.
        self.resolver = None

    # -- plumbing ----------------------------------------------------------

    @property
    def db(self):
        if self._db is None:
            memory = self.dry_run and not self.path.exists()
            if memory:
                self._db = sqlite3.connect(":memory:", isolation_level=None)
            else:
                if not self.path.exists():
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(self.path.parent, 0o700)
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                self._db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA secure_delete = ON")
            self._db.execute("BEGIN IMMEDIATE")
            # Not executescript(): that commits first, which dry run must not.
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    self._db.execute(statement)
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                             (str(SCHEMA_VERSION),))
            # A dry run's scratch ledger lives in memory and may keep its schema.
            self._db.execute("COMMIT" if memory else "ROLLBACK" if self.dry_run else "COMMIT")
        return self._db

    def _end(self, commit):
        if commit and not self.dry_run:
            self._db.execute("COMMIT")
        else:
            self._db.execute("ROLLBACK")

    def tx(self, fn, *args):
        """Run fn inside one IMMEDIATE transaction; dry run rolls it back."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            out = fn(db, *args)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        self._end(commit=True)
        return out

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def now(self):
        return self._now().astimezone(self.tz).isoformat(timespec="seconds")

    def new_id(self, db, prefix, table):
        while True:
            candidate = f"{prefix}_{secrets.token_hex(6)}"
            if not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone():
                return candidate

    # -- validation --------------------------------------------------------

    def parse_time(self, value):
        if value is None:
            return self._now().astimezone(self.tz)
        if not isinstance(value, str):
            raise core.InvalidInput("eaten_at: an ISO 8601 date-time string")
        try:
            t = datetime.fromisoformat(value)
        except ValueError:
            raise core.InvalidInput(f"eaten_at: {value!r} is not an ISO 8601 date-time") from None
        if t.tzinfo is None:
            t = t.replace(tzinfo=self.tz)
        if t - self._now() > FUTURE_SLACK:
            raise core.InvalidInput("eaten_at is in the future - log a meal once it is eaten")
        return t

    @staticmethod
    def parse_events(events):
        if events is None:
            return []
        if not isinstance(events, list) or not all(
                isinstance(e, str) and EVENT_RE.match(e) for e in events):
            raise core.InvalidInput("source_events: a list like ['telegram:<chat_id>:<message_id>']")
        return sorted(set(events))

    def normalize(self, raw, strict=True):
        """(normalized input, computed view). strict=False lets a proposal
        keep gaps: items that can't be computed yet are kept as given."""
        if not isinstance(raw, dict):
            raise core.InvalidInput("entry: expected a JSON object")
        unknown = set(raw) - ENTRY_KEYS
        if unknown:
            raise core.InvalidInput(f"entry: unknown keys {sorted(unknown)}")
        hashes = raw.get("image_sha256") or []
        if not isinstance(hashes, list) or not all(
                isinstance(h, str) and core.SHA256_RE.match(h) for h in hashes):
            raise core.InvalidInput("image_sha256: a list of SHA-256 hex digests")
        meal = raw.get("meal")
        if meal is not None and meal not in MEALS:
            raise core.InvalidInput(f"meal must be one of {', '.join(MEALS)}")
        note = core._text(raw.get("note"), "note", 300)
        events = self.parse_events(raw.get("source_events"))
        eaten = self.parse_time(raw.get("eaten_at"))
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            raise core.InvalidInput("items: a non-empty list")
        if len(items) > MAX_ITEMS:
            raise core.InvalidInput(f"items: at most {MAX_ITEMS} per meal")
        computed, missing, kept = [], [], []
        for i, it in enumerate(items):
            try:
                if self.resolver is not None:
                    it = self.resolver(it)
                kept.append(it)
                computed.append(core.compute_item(it))
            except core.MissingInfo as gap:
                if strict:
                    raise core.MissingInfo(f"items[{i}].{gap.field}", gap.question) from None
                if len(kept) <= i:
                    kept.append(items[i])
                missing.append({"item": i, "field": gap.field, "question": gap.question})
            except core.LookupFailed as err:
                if strict:
                    raise
                kept.append(items[i])
                missing.append({"item": i, "field": "nutrients",
                                "question": f"{err} - confirming later retries the lookup"})
            except core.InvalidInput as err:
                raise core.InvalidInput(f"items[{i}]: {err}") from None
        stored = {"eaten_at": eaten.isoformat(timespec="seconds"), "meal": meal,
                  "note": note, "source_events": events, "items": kept,
                  "image_sha256": sorted(set(hashes))}
        return stored, computed, missing

    @staticmethod
    def view_payload(stored, computed):
        tot = core.totals(computed)
        estimate = any(i["estimate"] for i in computed)
        warnings = [f"{i['name']}: {w}" for i in computed for w in i["warnings"]]
        return {"input": stored, "items": computed, "totals": tot, "estimate": estimate,
                "warnings": warnings, "receipt": core.receipt(tot, estimate)}

    # -- source events -----------------------------------------------------

    def _claimed(self, db, events):
        rows = [db.execute("SELECT kind, target FROM source_events WHERE key = ?",
                           (e,)).fetchone() for e in events]
        targets = {(r["kind"], r["target"]) for r in rows if r}
        if len(targets) > 1:
            raise Conflict("source events already belong to different meals: "
                           + ", ".join(sorted(t for _, t in targets)))
        if not targets:
            return None
        kind, target = targets.pop()
        if kind == "proposal":
            row = db.execute("SELECT state, entry_id FROM proposals WHERE id = ?",
                             (target,)).fetchone()
            if row and row["entry_id"]:
                kind, target = "entry", row["entry_id"]
            elif row and row["state"] == "cancelled" and not all(rows):
                # A new message alongside a dropped proposal's is a fresh meal,
                # not a redelivery: release the dropped claims, don't bind to it.
                db.executemany("DELETE FROM source_events WHERE key = ?",
                               [(e,) for e, r in zip(events, rows) if r])
                return None
        for e in events:
            db.execute("INSERT OR IGNORE INTO source_events VALUES (?, ?, ?, ?)",
                       (e, kind, target, self.now()))
        return kind, target

    def _claim(self, db, events, kind, target):
        for e in events:
            db.execute("INSERT INTO source_events VALUES (?, ?, ?, ?)",
                       (e, kind, target, self.now()))

    def _duplicate(self, db, claimed):
        kind, target = claimed
        if kind == "entry":
            return {"status": "duplicate", "entry": self._entry(db, target)}
        return {"status": "duplicate", "proposal": self._proposal(db, target)}

    # -- entries -----------------------------------------------------------

    def add(self, raw):
        """Log a complete meal. `images` lists stage tokens: their hashes go
        into the entry and the staged copies are deleted after the commit."""
        tokens, raw = self._split_images(raw)
        # Redelivery of an unresolved original must not repeat lookup or fail
        # validation after its proposal has already become a complete meal.
        events = self.parse_events(raw.get("source_events")) if isinstance(raw, dict) else []
        if events:
            duplicate = self.tx(lambda db: self._claimed(db, events))
            if duplicate:
                return self._duplicate(self.db, duplicate)
        stored, computed, _ = self.normalize(raw)

        def run(db):
            claimed = self._claimed(db, stored["source_events"])
            if claimed:
                self._use_images(db, tokens)
                return self._duplicate(db, claimed)
            stored["image_sha256"] = sorted(set(stored["image_sha256"])
                                            | set(self._use_images(db, tokens)))
            eid = self._insert_entry(db, stored, computed)
            return {"status": "logged", "entry": self._entry(db, eid)}
        out = self.tx(run)
        self._discard_all(tokens)
        return out

    def _insert_entry(self, db, stored, computed):
        eid = self.new_id(db, "m", "entries")
        now = self.now()
        local = datetime.fromisoformat(stored["eaten_at"]).astimezone(self.tz).date().isoformat()
        db.execute("INSERT INTO entries VALUES (?, ?, 1, 'active', ?, ?)",
                   (eid, now, local, stored["eaten_at"]))
        db.execute("INSERT INTO revisions VALUES (?, 1, ?, 'create', NULL, ?)",
                   (eid, now, json.dumps(self.view_payload(stored, computed))))
        self._claim(db, stored["source_events"], "entry", eid)
        return eid

    def _entry(self, db, eid, history=False):
        row = db.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
        if not row:
            raise NotFound(f"no entry {eid}")
        revs = db.execute("SELECT * FROM revisions WHERE entry_id = ? ORDER BY rev",
                          (eid,)).fetchall()
        content = next((json.loads(r["payload"]) for r in reversed(revs) if r["payload"]), None)
        events = [r["key"] for r in db.execute(
            "SELECT key FROM source_events WHERE kind = 'entry' AND target = ? ORDER BY key",
            (eid,))]
        out = {"id": eid, "rev": row["rev"], "state": row["state"],
               "created_at": row["created_at"], "updated_at": revs[-1]["recorded_at"],
               "eaten_at": row["eaten_at"], "local_date": row["local_date"],
               "source_events": events}
        if content and row["state"] != "erased":
            inp = content["input"]
            out.update({"meal": inp.get("meal"), "note": inp.get("note"),
                        "image_sha256": inp.get("image_sha256", []),
                        "items": content["items"], "totals": content["totals"],
                        "estimate": content["estimate"], "warnings": content["warnings"],
                        "receipt": content["receipt"]})
        if history:
            out["history"] = [{"rev": r["rev"], "recorded_at": r["recorded_at"],
                               "action": r["action"], "reason": r["reason"],
                               "receipt": json.loads(r["payload"])["receipt"]
                               if r["payload"] else None} for r in revs]
        return out

    def get(self, eid, history=False):
        return self._entry(self.db, eid, history)

    def list_entries(self, start=None, end=None, include_deleted=False):
        q, args = "SELECT id FROM entries WHERE 1=1", []
        if not include_deleted:
            q += " AND state = 'active'"
        if start:
            q += " AND local_date >= ?"
            args.append(parse_date(start, "from"))
        if end:
            q += " AND local_date <= ?"
            args.append(parse_date(end, "to"))
        q += " ORDER BY eaten_at, created_at"
        return [self._entry(self.db, r["id"]) for r in self.db.execute(q, args).fetchall()]

    # -- days --------------------------------------------------------------

    def day(self, day):
        day = parse_date(day)
        entries = self.list_entries(day, day)
        items = [i for e in entries for i in e["items"]]
        mark = self.db.execute("SELECT * FROM day_marks WHERE local_date = ? "
                               "ORDER BY marked_at DESC, rowid DESC LIMIT 1", (day,)).fetchone()
        status = mark["status"] if mark else "partial"
        pending = self.db.execute("SELECT COUNT(*) FROM proposals WHERE state = 'pending' "
                                  "AND local_date = ?", (day,)).fetchone()[0]
        estimate = any(e["estimate"] for e in entries)
        tot = core.totals(items)
        return {
            "date": day, "status": status,
            "marked_at": mark["marked_at"] if mark else None,
            "entries": [{"id": e["id"], "eaten_at": e["eaten_at"], "meal": e["meal"],
                         "items": [i["name"] for i in e["items"]], "estimate": e["estimate"],
                         "receipt": e["receipt"]} for e in entries],
            "totals": tot, "estimate": estimate, "pending_proposals": pending,
            "receipt": core.receipt(tot, estimate) if items else "nothing logged",
            "note": "complete day as marked by the athlete" if status == "complete"
            else PARTIAL_NOTE,
        }

    def mark_day(self, day, status, note=None):
        day = parse_date(day)
        if status not in ("complete", "partial"):
            raise core.InvalidInput("day status is 'complete' or 'partial'")
        if day > self._now().astimezone(self.tz).date().isoformat():
            raise core.InvalidInput("cannot mark a future day")
        note = core._text(note, "note", 300)

        def run(db):
            if status == "complete" and db.execute(
                    "SELECT 1 FROM proposals WHERE state = 'pending' AND local_date = ?",
                    (day,)).fetchone():
                raise Conflict(f"{day} still has pending proposals - confirm or cancel "
                               "them before calling the day complete")
            db.execute("INSERT INTO day_marks VALUES (?, ?, ?, ?)",
                       (day, self.now(), status, note))
        self.tx(run)
        return self.day(day)

    def days(self, start, end):
        start, end = parse_date(start, "from"), parse_date(end, "to")
        if end < start:
            raise core.InvalidInput("'to' is before 'from'")
        if (date.fromisoformat(end) - date.fromisoformat(start)).days > 366:
            raise core.InvalidInput("at most one year at a time")
        dates = {r[0] for r in self.db.execute(
            "SELECT local_date FROM entries WHERE state = 'active' AND local_date BETWEEN ? AND ? "
            "UNION SELECT local_date FROM day_marks WHERE local_date BETWEEN ? AND ?",
            (start, end, start, end))}
        days = [self.day(d) for d in sorted(dates)]
        complete = [d for d in days if d["status"] == "complete"]
        average = None
        if complete:
            average = {}
            for key in core.NUTRIENTS:
                vals = [0.0 if not d["entries"] else d["totals"][key]["value"]
                        for d in complete if not d["entries"] or (d["totals"][key]["complete"]
                                                                 and not d["totals"][key].get("bounds"))]
                average[key] = (round(sum(vals) / len(vals), 1)
                                if len(vals) == len(complete) else None)
        return {
            "from": start, "to": end, "complete_days": len(complete),
            "partial_days": len(days) - len(complete),
            "average_complete_days": average,
            "note": "averages use days marked complete only; a nutrient some complete "
                    "day doesn't fully know has no average",
            "days": [{"date": d["date"], "status": d["status"], "estimate": d["estimate"],
                      "receipt": d["receipt"], "pending_proposals": d["pending_proposals"],
                      "totals": {k: v["value"] for k, v in d["totals"].items()}}
                     for d in days],
        }

    # -- proposals ---------------------------------------------------------

    def propose(self, raw, questions=None):
        """Park a meal that needs an answer. Staged images stay on disk until
        the proposal is confirmed or cancelled."""
        tokens, raw = self._split_images(raw)
        stored, computed, missing = self.normalize(raw, strict=False)
        asked = []
        for q in questions or []:
            asked.append(core._text(q, "question", 300, required=True))
        asked += [m["question"] for m in missing if m["question"] not in asked]

        def run(db):
            claimed = self._claimed(db, stored["source_events"])
            if claimed:
                self._use_images(db, tokens)
                return self._duplicate(db, claimed)
            pid = self.new_id(db, "p", "proposals")
            stored["image_sha256"] = sorted(set(stored["image_sha256"])
                                            | set(self._use_images(db, tokens, owner=pid)))
            now = self.now()
            local = datetime.fromisoformat(stored["eaten_at"]).astimezone(self.tz).date().isoformat()
            db.execute("INSERT INTO proposals VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL)",
                       (pid, now, now, local, json.dumps(stored),
                        json.dumps({"questions": asked, "missing": missing})))
            self._claim(db, stored["source_events"], "proposal", pid)
            return {"status": "pending", "proposal": self._proposal(db, pid)}
        out = self.tx(run)
        if out["status"] == "duplicate":
            self._discard_all(tokens)
        return out

    def _proposal(self, db, pid):
        row = db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise NotFound(f"no proposal {pid}")
        out = {"id": pid, "state": row["state"], "created_at": row["created_at"],
               "updated_at": row["updated_at"], "local_date": row["local_date"],
               "entry_id": row["entry_id"]}
        if row["payload"]:
            stored = json.loads(row["payload"])
            meta = json.loads(row["questions"])
            computed = []
            for it in stored["items"]:
                try:
                    computed.append(core.compute_item(it))
                except (core.MissingInfo, core.InvalidInput):
                    pass  # an unresolved barcode-only item, say
            tot = core.totals(computed)
            out.update({"input": stored, "questions": meta["questions"],
                        "missing": meta["missing"],
                        "preview": core.receipt(tot, any(i["estimate"] for i in computed))
                        + (" (incomplete)" if meta["missing"] else "")})
        return out

    def pending(self):
        return [self._proposal(self.db, r["id"]) for r in self.db.execute(
            "SELECT id FROM proposals WHERE state = 'pending' ORDER BY created_at").fetchall()]

    def get_proposal(self, pid):
        return self._proposal(self.db, pid)

    def update_proposal(self, pid, patch):
        """Persist candidate/variant/quantity answers on the SAME proposal.

        No entry is created, including when all gaps have now been filled.
        """
        def run(db):
            row = db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
            if not row:
                raise NotFound(f"no proposal {pid}")
            if row["state"] != "pending":
                raise Conflict(f"proposal {pid} is {row['state']}")
            previous = json.loads(row["payload"])
            previous["source_events"] = [r["key"] for r in db.execute(
                "SELECT key FROM source_events WHERE kind = 'proposal' AND target = ? ORDER BY key",
                (pid,))]
            merged = merge_patch(previous, patch)
            merged["source_events"] = sorted(set(previous["source_events"]) |
                                              set(self.parse_events(merged.get("source_events"))))
            stored, _, missing = self.normalize(merged, strict=False)
            for event in stored["source_events"]:
                owner = db.execute("SELECT kind, target FROM source_events WHERE key = ?", (event,)).fetchone()
                if owner and (owner["kind"], owner["target"]) != ("proposal", pid):
                    raise Conflict(f"source event {event} belongs to {owner['target']}")
            self._claim(db, [e for e in stored["source_events"] if e not in previous["source_events"]], "proposal", pid)
            meta = {"questions": [m["question"] for m in missing], "missing": missing}
            local = datetime.fromisoformat(stored["eaten_at"]).astimezone(self.tz).date().isoformat()
            db.execute("UPDATE proposals SET payload = ?, questions = ?, updated_at = ?, local_date = ? WHERE id = ?",
                       (json.dumps(stored), json.dumps(meta), self.now(), local, pid))
            return {"status": "pending", "proposal": self._proposal(db, pid)}
        return self.tx(run)

    def confirm(self, pid, patch=None):
        def run(db):
            row = db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
            if not row:
                raise NotFound(f"no proposal {pid}")
            if row["state"] != "pending":
                raise Conflict(f"proposal {pid} is {row['state']}")
            previous = json.loads(row["payload"])
            previous["source_events"] = [r["key"] for r in db.execute(
                "SELECT key FROM source_events WHERE kind = 'proposal' AND target = ? ORDER BY key",
                (pid,))]
            merged = merge_patch(previous, patch or {})
            merged["source_events"] = sorted(set(previous["source_events"])
                                             | set(self.parse_events(merged.get("source_events"))))
            stored, computed, _ = self.normalize(merged)
            for e in stored["source_events"]:
                r = db.execute("SELECT kind, target FROM source_events WHERE key = ?",
                               (e,)).fetchone()
                if r and (r["kind"], r["target"]) != ("proposal", pid):
                    raise Conflict(f"source event {e} already belongs to {r['target']}")
            db.execute("DELETE FROM source_events WHERE kind = 'proposal' AND target = ?", (pid,))
            eid = self._insert_entry(db, stored, computed)
            db.execute("UPDATE proposals SET state = 'confirmed', entry_id = ?, updated_at = ? "
                       "WHERE id = ?", (eid, self.now(), pid))
            return {"status": "logged", "entry": self._entry(db, eid), "proposal": pid}
        out = self.tx(run)
        self._discard_owned(pid)
        return out

    def cancel(self, pid):
        def run(db):
            row = db.execute("SELECT state FROM proposals WHERE id = ?", (pid,)).fetchone()
            if not row:
                raise NotFound(f"no proposal {pid}")
            if row["state"] != "pending":
                raise Conflict(f"proposal {pid} is {row['state']}")
            # The source events stay claimed: a redelivered message must not
            # resurrect what the athlete said to drop.
            db.execute("UPDATE proposals SET state = 'cancelled', payload = NULL, "
                       "updated_at = ? WHERE id = ?", (self.now(), pid))
            return self._proposal(db, pid)
        out = self.tx(run)
        self._discard_owned(pid)
        return out

    # -- corrections and deletion -----------------------------------------

    def _current(self, db, eid, expect_rev=None):
        row = db.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
        if not row:
            raise NotFound(f"no entry {eid}")
        if expect_rev is not None and row["rev"] != expect_rev:
            raise Conflict(f"{eid} is at revision {row['rev']}, not {expect_rev} - re-read it")
        return row

    def correct(self, eid, patch, reason=None, expect_rev=None):
        if not isinstance(patch, dict) or not patch:
            raise core.InvalidInput("correction: a non-empty JSON merge patch")
        reason = core._text(reason, "reason", 300)

        def run(db):
            row = self._current(db, eid, expect_rev)
            if row["state"] != "active":
                raise Conflict(f"{eid} is {row['state']}")
            last = db.execute("SELECT payload FROM revisions WHERE entry_id = ? AND rev = ?",
                              (eid, row["rev"])).fetchone()
            current = json.loads(last["payload"])["input"]
            # Redelivery can attach events without writing a revision. The
            # ownership table, not the last payload, is authoritative.
            current["source_events"] = [r["key"] for r in db.execute(
                "SELECT key FROM source_events WHERE kind = 'entry' AND target = ? ORDER BY key",
                (eid,))]
            merged = merge_patch(current, patch)
            # Source events only accumulate: unclaiming one would let a
            # redelivery log the meal a second time.
            merged["source_events"] = sorted(set(current["source_events"])
                                             | set(self.parse_events(merged.get("source_events"))))
            stored, computed, _ = self.normalize(merged)
            for e in stored["source_events"]:
                r = db.execute("SELECT kind, target FROM source_events WHERE key = ?",
                               (e,)).fetchone()
                if r and (r["kind"], r["target"]) != ("entry", eid):
                    raise Conflict(f"source event {e} already belongs to {r['target']}")
            self._claim(db, [e for e in stored["source_events"]
                             if e not in current["source_events"]], "entry", eid)
            rev = row["rev"] + 1
            local = datetime.fromisoformat(stored["eaten_at"]).astimezone(self.tz).date().isoformat()
            db.execute("INSERT INTO revisions VALUES (?, ?, ?, 'correct', ?, ?)",
                       (eid, rev, self.now(), reason,
                        json.dumps(self.view_payload(stored, computed))))
            db.execute("UPDATE entries SET rev = ?, local_date = ?, eaten_at = ? WHERE id = ?",
                       (rev, local, stored["eaten_at"], eid))
            return self._entry(db, eid)
        return self.tx(run)

    def delete(self, eid, reason=None, erase=False, expect_rev=None):
        reason = core._text(reason, "reason", 300)

        def run(db):
            row = self._current(db, eid, expect_rev)
            if row["state"] == "erased" or (row["state"] == "deleted" and not erase):
                raise Conflict(f"{eid} is already {row['state']}")
            rev = row["rev"] + 1
            if erase:
                db.execute("UPDATE revisions SET payload = NULL, reason = NULL "
                           "WHERE entry_id = ?", (eid,))
                db.execute("UPDATE proposals SET payload = NULL, questions = NULL "
                           "WHERE entry_id = ?", (eid,))
                db.execute("INSERT INTO revisions VALUES (?, ?, ?, 'erase', NULL, NULL)",
                           (eid, rev, self.now()))
                db.execute("UPDATE entries SET rev = ?, state = 'erased', local_date = NULL, "
                           "eaten_at = NULL WHERE id = ?", (rev, eid))
            else:
                db.execute("INSERT INTO revisions VALUES (?, ?, ?, 'delete', ?, NULL)",
                           (eid, rev, self.now(), reason))
                db.execute("UPDATE entries SET rev = ?, state = 'deleted' WHERE id = ?",
                           (rev, eid))
            return self._entry(db, eid)
        return self.tx(run)

    # -- feature-owned image copies ---------------------------------------
    #
    # The channel (e.g. the Telegram plugin) keeps its own download; that
    # file, the chat message and any provider-side retention are not ours
    # and are never touched. What we own is the copy `stage_image` makes in
    # self.inbox, addressed only by token - discard never takes a path.

    def _split_images(self, raw):
        if not isinstance(raw, dict) or "images" not in raw:
            return [], raw
        tokens = raw["images"]
        if not isinstance(tokens, list) or not all(
                isinstance(t, str) and TOKEN_RE.match(t) for t in tokens):
            raise core.InvalidInput("images: a list of stage tokens like i_0123456789abcdef")
        return sorted(set(tokens)), {k: v for k, v in raw.items() if k != "images"}

    def _use_images(self, db, tokens, owner=None):
        hashes = []
        for t in tokens:
            row = db.execute("SELECT * FROM images WHERE token = ?", (t,)).fetchone()
            if not row:
                raise NotFound(f"no staged image {t}")
            if row["discarded_at"]:
                raise Conflict(f"staged image {t} was already used or discarded")
            if row["owner"] and row["owner"] != owner:
                raise Conflict(f"staged image {t} belongs to proposal {row['owner']}")
            if owner:
                db.execute("UPDATE images SET owner = ? WHERE token = ?", (owner, t))
            hashes.append(row["sha256"])
        return hashes

    def _discard_all(self, tokens):
        for t in tokens:
            try:
                self.discard_image(t)
            except (Conflict, NotFound, OSError):
                pass  # left for `image purge`; never fatal after a commit

    def _discard_owned(self, pid):
        rows = self.db.execute("SELECT token FROM images WHERE owner = ? AND discarded_at IS NULL",
                               (pid,)).fetchall()
        self._discard_all([r["token"] for r in rows])

    def _inbox_dir(self, create):
        if self.inbox.is_symlink() or (self.inbox.exists() and not self.inbox.is_dir()):
            raise Conflict(f"{self.inbox} is not a plain directory - left alone")
        if create and not self.inbox.exists():
            self.inbox.mkdir(parents=True)
            os.chmod(self.inbox, 0o700)
        return self.inbox

    def stage_image(self, path, source_event=None):
        """Copy an incoming image into the feature's own inbox, hash it, and
        return a token. The source is left exactly as it was."""
        if not isinstance(path, str) or not path:
            raise core.InvalidInput("image path: a file path")
        if source_event is not None:
            self.parse_events([source_event])
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            raise core.InvalidInput(f"image {path}: cannot open ({e.strerror}; links are refused)") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise core.InvalidInput(f"image {path}: not a regular file")
            if st.st_size == 0 or st.st_size > MAX_IMAGE_BYTES:
                raise core.InvalidInput(f"image {path}: empty or larger than "
                                        f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB")
            chunks, size = [], 0
            while size <= MAX_IMAGE_BYTES:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise core.InvalidInput(f"image {path}: empty or too large")
        fmt = image_format(data)
        if not fmt:
            raise core.InvalidInput(f"image {path}: not a JPEG, PNG, GIF or WebP image")
        name, ext = fmt
        digest = hashlib.sha256(data).hexdigest()
        if self.dry_run:
            return {"token": None, "path": path, "sha256": digest, "bytes": len(data),
                    "format": name, "dry_run": True}
        inbox = self._inbox_dir(create=True)
        token = f"i_{secrets.token_hex(8)}"
        target = inbox / f"{token}.{ext}"
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            self.tx(lambda db: db.execute(
                "INSERT INTO images VALUES (?, ?, ?, ?, NULL, ?, NULL)",
                (token, digest, ext, self.now(), source_event)))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {"token": token, "path": str(target), "sha256": digest, "bytes": len(data),
                "format": name}

    def _image_row(self, token):
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            raise core.InvalidInput("image token: i_ followed by 16 hex digits (never a path)")
        row = self.db.execute("SELECT * FROM images WHERE token = ?", (token,)).fetchone()
        if not row:
            raise NotFound(f"no staged image {token}")
        return row

    def _image_path(self, row):
        if row["ext"] not in {ext for _, ext, _ in IMAGE_FORMATS} | {"webp"}:
            raise Conflict(f"staged image {row['token']} has an unexpected extension")
        return self._inbox_dir(create=False) / f"{row['token']}.{row['ext']}"

    def discard_image(self, token):
        """Delete the feature's own staged copy - nothing else, ever."""
        row = self._image_row(token)
        if row["discarded_at"]:
            return {"token": token, "status": "already discarded"}
        path = self._image_path(row)
        if self.dry_run:
            return {"token": token, "status": "dry run"}
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISREG(st.st_mode):
                raise Conflict(f"{path} is not a regular file - left alone")
            os.unlink(path)
        self.tx(lambda db: db.execute("UPDATE images SET discarded_at = ? WHERE token = ?",
                                      (self.now(), token)))
        return {"token": token, "status": "discarded"}

    def purge_images(self, older_than_hours=24):
        """Discard staged copies older than the threshold that no pending
        proposal still needs (a photo staged and never logged, say)."""
        hours = core.parse_number(older_than_hours, "older_than_hours")
        cutoff = self._now() - timedelta(hours=float(hours))
        rows = self.db.execute(
            "SELECT i.token, i.staged_at FROM images i LEFT JOIN proposals p ON p.id = i.owner "
            "WHERE i.discarded_at IS NULL AND (p.state IS NULL OR p.state != 'pending')").fetchall()
        stale = [r["token"] for r in rows
                 if datetime.fromisoformat(r["staged_at"]) <= cutoff]
        done, would, failed = [], [], []
        for t in stale:
            try:
                status = self.discard_image(t)["status"]
            except (Conflict, OSError) as e:
                failed.append({"token": t, "error": str(e)})
                continue
            (would if status == "dry run" else done).append(t)
        return {"discarded": done, "would_discard": would, "failed": failed}

    def scan_image(self, token):
        """Decode barcodes with a locally installed zbarimg, if there is one.
        Optional: without it, read the digits off the image instead."""
        row = self._image_row(token)
        if row["discarded_at"]:
            raise Conflict(f"staged image {token} was already discarded")
        exe = shutil.which("zbarimg")
        if not exe:
            return {"available": False, "codes": [], "rejected": [],
                    "note": "zbarimg is not installed - read the digits from the image"}
        try:
            proc = subprocess.run([exe, "--quiet", "--raw", str(self._image_path(row))],
                                  capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return {"available": True, "codes": [], "rejected": [], "note": "zbarimg timed out"}
        codes, rejected = [], []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                code = core.gtin(line)
                if code not in codes:
                    codes.append(code)
            except core.InvalidInput as e:
                rejected.append({"text": line[:40], "reason": str(e)})
        return {"available": True, "codes": codes, "rejected": rejected}


def image_format(data):
    """(name, extension) from the magic bytes, or None."""
    for name, ext, magics in IMAGE_FORMATS:
        if any(data.startswith(m) for m in magics):
            return name, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "webp"
    return None
