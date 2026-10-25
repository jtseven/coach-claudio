#!/usr/bin/env python3
"""The food log: meals the athlete sends as photos, labels, barcodes or text.

Optional and off by default: `[integrations] nutrition_log = true` turns it
on. Standard library only (Python 3.11+).

  status      on or off, where the data lives, provider settings
  schema      the JSON schema of an entry (nutrition.entry.v1)
  add         log a complete meal; --or-propose parks it if a gap remains
  propose     park a meal that needs an answer from the athlete
  pending     open proposals and their questions
  confirm     log a proposal, optionally with a JSON merge patch (the answer)
  cancel      drop a proposal
  get         one entry; --history for every revision
  list        entries in a date range
  correct     new revision from a JSON merge patch
  delete      tombstone an entry; --erase blanks its content too
  day         one day's totals - partial unless the athlete said complete
  mark        mark a day complete or partial
  days        a range, with averages over complete days only
  lookup      a barcode in Open Food Facts (cached)
  image       stage | discard | purge | scan - the feature's own image copies

ENTRY and PATCH arguments are JSON text, @path to a JSON file, or - for
stdin. The model reads the photo; this script does every number.

Exit codes: 0 ok (including "duplicate"), 2 invalid input, 3 information
missing (ask the athlete), 4 nutrition logging is off, 5 not found or
conflict, 6 provider unavailable (no made-up fallback).
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import coachconf as cc
import food_providers as fp
import nutrition_core as core
import nutrition_store as store
import nutrition_bls as bls

LEDGER = cc.NUTRITION_DIR / "ledger.sqlite3"
CACHE = cc.NUTRITION_DIR / "provider_cache.sqlite3"
INBOX = cc.NUTRITION_DIR / "inbox"
OFF_URL_ENV = "COACH_NUTRITION_OFF_URL"  # loopback only; for offline tests

# Commands that write or reach out need the opt-in; reading what is already
# there, and removing it, never do.
NEEDS_OPT_IN = {"add", "propose", "confirm", "choose", "search", "correct", "mark", "lookup", "stage", "scan"}

QUANTITY = {"oneOf": [
    {"type": "string", "pattern": r"^\s*\d+([.,]\d+)?\s*[A-Za-z]+\s*$",
     "examples": ["250 g", "0,33 l", "2 serving"]},
    {"type": "object", "required": ["value", "unit"], "additionalProperties": False,
     "properties": {"value": {"type": ["number", "string"]},
                    "unit": {"enum": sorted(core.MASS_G) + sorted(core.VOLUME_ML)
                             + list(core.COUNT)}}}]}
NUMBER = {"oneOf": [{"type": "number", "minimum": 0},
                    {"type": "string", "pattern": r"^<?\d+([.,]\d+)?$"},
                    {"type": "null"}]}
ENTRY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "nutrition.entry.v1",
    "title": "One meal for nutrition.py add / propose",
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "eaten_at": {"type": "string", "description": "ISO 8601; no offset = athlete timezone; "
                     "default now; not in the future"},
        "meal": {"enum": list(store.MEALS)},
        "note": {"type": "string", "maxLength": 300},
        "source_events": {"type": "array", "items": {"type": "string",
                          "pattern": store.EVENT_RE.pattern},
                          "description": "dedupe keys, e.g. telegram:<chat_id>:<message_id>"},
        "images": {"type": "array", "items": {"type": "string", "pattern": store.TOKEN_RE.pattern},
                   "description": "tokens from `image stage`; copies are deleted after logging"},
        "image_sha256": {"type": "array", "items": {"type": "string",
                         "pattern": core.SHA256_RE.pattern}},
        "items": {"type": "array", "minItems": 1, "maxItems": store.MAX_ITEMS, "items": {
            "type": "object",
            "additionalProperties": False,
            "description": "name+nutrients+provenance, or just barcode+amount to fill "
                           "it from Open Food Facts",
            "properties": {
                "name": {"type": "string", "maxLength": 120},
                "query": {"type": "string", "maxLength": 200},
                "bls_code": {"type": "string", "pattern": r"^[A-Z][0-9]{6}$"},
                "brand": {"type": "string", "maxLength": 120},
                "barcode": {"type": "string", "pattern": r"^[0-9 -]{8,20}$",
                            "description": "GTIN-8/12/13/14 as a string, check digit verified"},
                "amount": QUANTITY,
                "basis": {"oneOf": [{"enum": list(core.BASES)}, {
                    "type": "object", "required": ["per"], "additionalProperties": False,
                    "properties": {"per": {"enum": list(core.BASES)}, "serving": QUANTITY}}]},
                "nutrients": {"type": "object", "additionalProperties": False,
                              "properties": {k: NUMBER for k in core.NUTRIENTS + core.INPUT_ONLY}},
                "density_g_per_ml": {"type": "number", "minimum": 0.2, "maximum": 3},
                "provenance": {"type": "object", "required": ["kind"],
                               "additionalProperties": False, "properties": {
                                   "kind": {"enum": list(core.PROVENANCE_KINDS)},
                                   "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                                                  "description": "required for estimate"},
                                   **{k: {"type": "string"} for k in sorted(
                                       core.PROVENANCE_KEYS - {"kind", "confidence",
                                                               "image_sha256"})},
                                   "image_sha256": {"type": ["string", "array"]}}},
                "note": {"type": "string", "maxLength": 300},
            }}},
    },
}


class Disabled(Exception):
    pass


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------


def dry_run():
    return os.environ.get("COACH_DRY_RUN") == "1"


def timezone():
    name = cc.get("athlete.timezone")
    if name:
        try:
            return ZoneInfo(str(name))
        except (ZoneInfoNotFoundError, ValueError):
            raise core.InvalidInput(f"athlete.timezone {name!r} is not an IANA zone") from None
    return datetime.now().astimezone().tzinfo


def provider():
    try:
        return fp.OpenFoodFacts(country=cc.get("nutrition.country", "de"),
                                language=cc.get("nutrition.language", "de"),
                                contact=cc.get("nutrition.contact", "") or "",
                                timeout=cc.get("nutrition.lookup_timeout_s", 10),
                                base_url=os.environ.get(OFF_URL_ENV) or None,
                                api_version=cc.get("nutrition.off_api_version", "v2" if os.environ.get(OFF_URL_ENV) else "v3.6"))
    except ValueError as e:
        raise core.InvalidInput(f"[nutrition] config: {e}") from None


class Lookups:
    """The provider cache, built on first use: deleting or cancelling never
    depends on the [nutrition] provider settings being valid."""

    def __init__(self):
        self._cache = None

    def cache(self):
        if self._cache is None:
            self._cache = fp.ProviderCache(CACHE, provider(), dry_run=dry_run())
        return self._cache

    def lookup(self, code, refresh=False, offline=False):
        offline = offline or bool(cc.get("nutrition.offline", False))
        return self.cache().lookup(code, refresh=refresh, offline=offline)

    def resolve(self, item):
        if isinstance(item, dict) and item.get("bls_code") is not None:
            return fp.resolve_bls_item(item, self.lookup_bls)
        return fp.resolve_item(item, self.lookup)

    def lookup_bls(self, code):
        try:
            return bls.get(code, db_path=cc.get("nutrition.bls_db_path") or None)
        except bls.NotFound as e:
            raise core.MissingInfo("bls_code", str(e)) from None
        except bls.BlsError as e:
            raise fp.ProviderError("unavailable", f"BLS: {e}") from None

    def search(self, query, source="both", limit=5):
        results, errors = [], {}
        if source in ("off", "both"):
            try:
                if cc.get("nutrition.offline", False):
                    raise fp.ProviderError("offline", "OFF text search unavailable offline; use BLS or cached barcode")
                results.extend(self.cache().search(query, limit=limit)["results"])
            except core.LookupFailed as e:
                if source == "off":
                    raise
                errors["off"] = {"kind": e.kind, "message": str(e)}
        if source in ("bls", "both"):
            try:
                out = bls.search(query, db_path=cc.get("nutrition.bls_db_path") or None, limit=limit)
                results.extend(dict(r, provider="bls", attribution=out["attribution"]) for r in out["results"])
            except bls.BlsError as e:
                if source == "bls":
                    raise fp.ProviderError("unavailable", f"BLS: {e}") from None
                errors["bls"] = {"kind": "unavailable", "message": str(e)}
        if not results and errors:
            raise fp.ProviderError("unavailable", "; ".join(e["message"] for e in errors.values()))
        return {"query": query, "results": results, "errors": errors, "selection_required": True}

    def close(self):
        if self._cache is not None:
            self._cache.close()


def open_ledger(write, create):
    """A ledger that is only ever created by a command that adds data, and
    never changed in a dry run."""
    scratch = dry_run() or (not create and not LEDGER.exists())
    led = store.Ledger(LEDGER, timezone(), dry_run=scratch, inbox=INBOX)
    led.lookups = Lookups()
    if write:
        led.resolver = led.lookups.resolve
    return led


def read_json(arg, what):
    if arg == "-":
        text = sys.stdin.read()
    elif arg.startswith("@"):
        try:
            text = Path(arg[1:]).read_text(encoding="utf-8")
        except OSError as e:
            raise core.InvalidInput(f"{what}: cannot read {arg[1:]}: {e.strerror}") from None
    else:
        text = arg
    try:
        return json.loads(text, parse_constant=lambda c: _bad_constant(c, what))
    except json.JSONDecodeError as e:
        raise core.InvalidInput(f"{what}: not valid JSON ({e.msg} at {e.pos})") from None


def _bad_constant(c, what):
    raise core.InvalidInput(f"{what}: {c} is not a number")


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def hhmm(iso):
    return iso[11:16] if iso else "--:--"


def text_entry(e, verb="logged"):
    if e.get("state") == "erased":
        return f"{e['id']} erased"
    names = ", ".join(i["name"] for i in e.get("items", []))
    head = (f"{verb} {e['id']} rev {e['rev']} ({e.get('meal') or 'meal'} "
            f"{e['local_date']} {hhmm(e['eaten_at'])}): {names}")
    lines = [head, f"  {e['receipt']}"]
    if e.get("state") == "deleted":
        lines[0] += " [deleted]"
    if e.get("estimate"):
        lines.append("  ~ includes estimates")
    lines += [f"  ! {w}" for w in e.get("warnings", [])]
    for h in e.get("history", []):
        lines.append(f"  rev {h['rev']} {h['action']} {h['recorded_at']}"
                     + (f" ({h['reason']})" if h.get("reason") else "")
                     + (f": {h['receipt']}" if h.get("receipt") else ""))
    return "\n".join(lines)


def text_proposal(p):
    if p["state"] != "pending":
        return f"proposal {p['id']} {p['state']}" + (f" -> {p['entry_id']}" if p["entry_id"] else "")
    lines = [f"pending {p['id']} ({p['local_date']}): {p['preview']}"]
    lines += [f"  ? {q}" for q in p["questions"]]
    return "\n".join(lines)


def text_result(res):
    if "entry" in res:
        verb = "duplicate of" if res["status"] == "duplicate" else "logged"
        return text_entry(res["entry"], verb)
    if res["status"] == "duplicate":
        return "duplicate of " + text_proposal(res["proposal"])
    return text_proposal(res["proposal"])


def text_day(d):
    head = f"{d['date']} {d['status']}"
    head += f" (marked {d['marked_at']})" if d["status"] == "complete" else f" - {d['note']}"
    lines = [head]
    for e in d["entries"]:
        lines.append(f"  {hhmm(e['eaten_at'])} {e['meal'] or 'meal':<9} {e['id']} "
                     f"{', '.join(e['items'])}: {e['receipt']}")
    lines.append(f"  total: {d['receipt']}")
    if d["pending_proposals"]:
        lines.append(f"  {d['pending_proposals']} pending proposal(s) not counted")
    return "\n".join(lines)


def text_days(r):
    lines = [f"{r['from']}..{r['to']}: {r['complete_days']} complete, "
             f"{r['partial_days']} partial day(s)"]
    for d in r["days"]:
        lines.append(f"  {d['date']} {d['status']:<8} {d['receipt']}")
    avg = r["average_complete_days"]
    if avg:
        lines.append("  average of complete days: " + ", ".join(
            f"{k} {v if v is not None else '?'}" for k, v in avg.items()))
    else:
        lines.append("  no complete days - no average (partial days are not intake)")
    return "\n".join(lines)


def text_product(p):
    serving = p.get("serving")
    # The core also renders qualified bounds; never stringify '<x' as a
    # measured number, nor substitute zero for a missing provider nutrient.
    nutrient_text = "nutrition unknown"
    if any(v is not None for v in p["nutrients"].values()):
        view = core.compute_item({"name": p.get("name") or "product", "basis": "total",
                                  "nutrients": p["nutrients"], "provenance": {"kind": "user"}})
        nutrient_text = core.receipt(core.totals([view]))
    lines = [f"{p['code']} {p.get('name') or '(no name)'}"
             + (f" - {p['brand']}" if p.get("brand") else "")
             + (f" [{p['quantity']}]" if p.get("quantity") else ""),
             f"  per {p['basis'] if p.get('basis') else '100 g or 100 ml (unknown)'}: "
             + nutrient_text,
             f"  serving: {serving['value']:g} {serving['unit']}" if serving else
             "  serving: unknown"]
    if p.get("implausible"):
        lines.append(f"  ! implausible: {p['implausible']}")
    lines += [f"  ! {w}" for w in p.get("warnings", [])]
    lines.append(f"  {'cached' if p.get('cached') else 'fetched'} {p.get('fetched_at')} - "
                 f"source: {p['attribution']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_status(a, led):
    counts = {}
    if LEDGER.exists():
        db = led.db
        counts = {
            "entries": db.execute("SELECT COUNT(*) FROM entries WHERE state = 'active'").fetchone()[0],
            "pending": db.execute("SELECT COUNT(*) FROM proposals WHERE state = 'pending'").fetchone()[0],
            "staged_images": db.execute("SELECT COUNT(*) FROM images "
                                        "WHERE discarded_at IS NULL").fetchone()[0]}
    out = {"enabled": cc.enabled("nutrition_log"), "dry_run": dry_run(),
           "ledger": str(LEDGER), "ledger_exists": LEDGER.exists(), "provider_cache": str(CACHE),
           "image_inbox": str(INBOX), "country": cc.get("nutrition.country"),
           "language": cc.get("nutrition.language"), "offline": bool(cc.get("nutrition.offline")),
           "zbarimg": bool(shutil.which("zbarimg")),
           "providers": {fp.OpenFoodFacts.name: fp.OpenFoodFacts.attribution}, **counts}
    if a.json:
        return out
    return "\n".join([
        f"nutrition log {'on' if out['enabled'] else 'off (integrations.nutrition_log)'}"
        + (" - dry run" if out["dry_run"] else ""),
        f"ledger    {out['ledger']}" + ("" if out["ledger_exists"] else " (not created yet)"),
        "          " + ", ".join(f"{k} {v}" for k, v in counts.items()) if counts else "",
        f"cache     {out['provider_cache']}",
        f"lookups   Open Food Facts, country {out['country']}, language {out['language']}"
        + (", offline (cache only)" if out["offline"] else ""),
        f"zbarimg   {'installed' if out['zbarimg'] else 'not installed (optional)'}",
    ]).replace("\n\n", "\n")


def cmd_schema(a, led):
    return ENTRY_SCHEMA


def cmd_add(a, led):
    raw = read_json(a.entry, "entry")
    try:
        return led.add(raw)
    except core.MissingInfo as gap:
        if not a.or_propose:
            raise
        return led.propose(raw, questions=[gap.question])
    except core.LookupFailed as err:
        if not a.or_propose:
            raise
        return led.propose(raw, questions=[f"{err} - confirming later retries the lookup"])


def cmd_propose(a, led):
    return led.propose(read_json(a.entry, "entry"), questions=a.question)


def cmd_pending(a, led):
    rows = led.pending()
    return rows if a.json else "\n".join(text_proposal(p) for p in rows) or "no pending proposals"


def cmd_confirm(a, led):
    patch = read_json(a.patch, "patch") if a.patch else {}
    if getattr(a, "amount", None):
        patch = store.merge_patch(patch, {"items": {str(a.item): {"amount": a.amount}}})
    if getattr(a, "source_event", None):
        patch["source_events"] = a.source_event
    try:
        return led.confirm(a.proposal_id, patch)
    except (core.MissingInfo, core.LookupFailed):
        # Keep a quantity answer even if the next gap is product selection or
        # a provider outage. Never create a second proposal for the reply.
        return led.update_proposal(a.proposal_id, patch)


def cmd_choose(a, led):
    item = {"barcode": a.barcode, "bls_code": a.bls_code,
            "nutrients": None, "basis": None, "provenance": None}
    if a.amount:
        item["amount"] = a.amount
    patch = {"items": {str(a.item): item}}
    if a.source_event:
        patch["source_events"] = a.source_event
    return led.update_proposal(a.proposal_id, patch)


def cmd_search(a, led):
    out = led.lookups.search(a.query, source=a.source, limit=a.limit)
    if a.json:
        return out
    lines = ["Candidates (select identity/variant, never automatically the first):"]
    lines += [f"  {r['provider']} {r['code']} {r.get('name') or '?'} "
              f"{r.get('brand') or ''} [{r.get('quantity') or r.get('preparation') or 'size unknown'}]"
              for r in out["results"]]
    lines += [f"  ! {source}: {err['message']}" for source, err in out.get("errors", {}).items()]
    if not out["results"]:
        lines.append("No match; ask for barcode or nutrition label. No estimated fallback.")
    return "\n".join(lines)


def cmd_cancel(a, led):
    p = led.cancel(a.proposal_id)
    return p if a.json else text_proposal(p)


def cmd_get(a, led):
    e = led.get(a.entry_id, history=a.history)
    return e if a.json else text_entry(e, "entry")


def cmd_list(a, led):
    rows = led.list_entries(a.date_from, a.date_to, include_deleted=a.deleted)
    if a.json:
        return rows
    return "\n".join(text_entry(e, "entry") for e in rows) or "no entries"


def cmd_correct(a, led):
    e = led.correct(a.entry_id, read_json(a.patch, "patch"), reason=a.reason,
                    expect_rev=a.expect_rev)
    return e if a.json else text_entry(e, "corrected")


def cmd_delete(a, led):
    e = led.delete(a.entry_id, reason=a.reason, erase=a.erase, expect_rev=a.expect_rev)
    return e if a.json else f"{e['id']} {e['state']}"


def cmd_day(a, led):
    d = led.day(a.date or datetime.now(led.tz).date().isoformat())
    return d if a.json else text_day(d)


def cmd_mark(a, led):
    d = led.mark_day(a.date, a.status, note=a.note)
    return d if a.json else text_day(d)


def cmd_days(a, led):
    r = led.days(a.date_from, a.date_to)
    return r if a.json else text_days(r)


def cmd_lookup(a, led):
    if getattr(a, "source", "off") == "bls":
        snap = led.lookups.lookup_bls(a.barcode)
        if a.json:
            return snap
        nutrients = ", ".join(f"{k}: {n.get('value')} ({n.get('qualifier')})"
                              for k, n in snap["nutrients"].items())
        return f"BLS {snap['source_id']} {snap['name']} per 100g\n{nutrients}\n" + str(snap["automatic_use"]) + "\n" + snap["attribution"]
    p = led.lookups.lookup(a.barcode, refresh=a.refresh, offline=a.offline)
    if core.restricted_circulation(p["code"]):
        p.setdefault("warnings", []).append("in-store code (weighed goods): it may not "
                                            "identify one product")
    return p if a.json else text_product(p)


def cmd_image(a, led):
    if a.image_cmd == "stage":
        r = led.stage_image(a.path, source_event=a.source_event)
        return r if a.json else (f"dry run - not staged; read {r['path']}" if r.get("dry_run")
                                 else f"staged {r['token']} {r['format']} {r['bytes']} bytes "
                                      f"sha256 {r['sha256'][:12]}...: read {r['path']}")
    if a.image_cmd == "discard":
        r = led.discard_image(a.token)
        return r if a.json else f"{r['token']} {r['status']}"
    if a.image_cmd == "purge":
        r = led.purge_images(older_than_hours=a.older_than_hours)
        if a.json:
            return r
        if r["would_discard"]:
            return f"would discard {len(r['would_discard'])} staged image(s)"
        return f"discarded {len(r['discarded'])} staged image(s)" + (
            f", {len(r['failed'])} left alone" if r["failed"] else "")
    r = led.scan_image(a.token)
    if a.json:
        return r
    if not r["available"]:
        return r["note"]
    return ("codes: " + (", ".join(r["codes"]) or "none")
            + "".join(f"\n  rejected {x['text']}: {x['reason']}" for x in r["rejected"]))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def parser():
    p = argparse.ArgumentParser(prog="nutrition.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("schema")
    s = sub.add_parser("add")
    s.add_argument("entry", help="JSON, @file or -")
    s.add_argument("--or-propose", action="store_true",
                   help="park it as a proposal if information is missing")
    s = sub.add_parser("propose")
    s.add_argument("entry")
    s.add_argument("--question", action="append", help="what to ask the athlete")
    sub.add_parser("pending")
    s = sub.add_parser("confirm")
    s.add_argument("proposal_id")
    s.add_argument("patch", nargs="?", help="JSON merge patch with the answer")
    s.add_argument("--amount", help="simple quantity answer, e.g. 50 g or 0,33 l")
    s.add_argument("--item", type=int, default=0)
    s.add_argument("--source-event", action="append")
    s = sub.add_parser("choose", help="select product on an existing pending proposal")
    s.add_argument("proposal_id")
    choice = s.add_mutually_exclusive_group(required=True)
    choice.add_argument("--barcode")
    choice.add_argument("--bls-code")
    s.add_argument("--amount")
    s.add_argument("--item", type=int, default=0)
    s.add_argument("--source-event", action="append")
    s = sub.add_parser("search", help="find product candidates; never logs a meal")
    s.add_argument("query")
    s.add_argument("--source", choices=("off", "bls", "both"), default="both")
    s.add_argument("--limit", type=int, choices=range(1, 6), default=5)
    s = sub.add_parser("cancel")
    s.add_argument("proposal_id")
    s = sub.add_parser("get")
    s.add_argument("entry_id")
    s.add_argument("--history", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--deleted", action="store_true", help="include tombstones")
    s = sub.add_parser("correct")
    s.add_argument("entry_id")
    s.add_argument("patch")
    s.add_argument("--reason")
    s.add_argument("--expect-rev", type=int)
    s = sub.add_parser("delete")
    s.add_argument("entry_id")
    s.add_argument("--reason")
    s.add_argument("--erase", action="store_true", help="also blank every revision's content")
    s.add_argument("--expect-rev", type=int)
    s = sub.add_parser("day")
    s.add_argument("date", nargs="?")
    s = sub.add_parser("mark")
    s.add_argument("date")
    s.add_argument("status", choices=("complete", "partial"))
    s.add_argument("--note")
    s = sub.add_parser("days")
    s.add_argument("--from", dest="date_from", required=True)
    s.add_argument("--to", dest="date_to", required=True)
    s = sub.add_parser("lookup")
    s.add_argument("barcode")
    s.add_argument("--source", choices=("off", "bls"), default="off")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--offline", action="store_true", help="cache only")
    s = sub.add_parser("image")
    isub = s.add_subparsers(dest="image_cmd", required=True)
    x = isub.add_parser("stage")
    x.add_argument("path", help="the incoming image, e.g. the channel's image_path")
    x.add_argument("--source-event")
    x = isub.add_parser("discard")
    x.add_argument("token", help="a stage token, never a path")
    x = isub.add_parser("purge")
    x.add_argument("--older-than-hours", default="24")
    x = isub.add_parser("scan")
    x.add_argument("token")
    return p


COMMANDS = {"status": cmd_status, "schema": cmd_schema, "add": cmd_add, "propose": cmd_propose,
            "pending": cmd_pending, "confirm": cmd_confirm, "cancel": cmd_cancel, "get": cmd_get,
            "list": cmd_list, "correct": cmd_correct, "delete": cmd_delete, "day": cmd_day,
            "mark": cmd_mark, "days": cmd_days, "lookup": cmd_lookup, "image": cmd_image,
            "search": cmd_search, "choose": cmd_choose}
WRITES = {"add", "propose", "confirm", "choose", "search", "cancel", "correct", "delete", "mark", "lookup", "image"}


def main(argv=None):
    a = parser().parse_args(argv)
    name = a.image_cmd if a.cmd == "image" else a.cmd
    led = None
    try:
        if name in NEEDS_OPT_IN and not cc.enabled("nutrition_log"):
            raise Disabled()
        led = open_ledger(write=a.cmd in WRITES, create=name in NEEDS_OPT_IN)
        out = COMMANDS[a.cmd](a, led)
    except Disabled:
        print("nutrition logging is off - set [integrations] nutrition_log = true in "
              "athlete/config.toml to opt in", file=sys.stderr)
        return 4
    except core.MissingInfo as gap:
        print(json.dumps({"status": "missing", "field": gap.field, "question": gap.question})
              if a.json else f"missing {gap.field}: {gap.question}\n"
              "nothing saved - ask, or use add --or-propose / propose to park it")
        return 3
    except core.InvalidInput as e:
        print(f"invalid: {e} - nothing saved", file=sys.stderr)
        return 2
    except (store.NotFound, store.Conflict) as e:
        print(f"{e}", file=sys.stderr)
        return 5
    except core.LookupFailed as e:
        print(f"lookup failed ({e.kind}): {e} - nothing saved, no fallback values",
              file=sys.stderr)
        return 6
    finally:
        if led is not None:
            led.lookups.close()
            led.close()
    note = dry_run() and a.cmd in WRITES
    if isinstance(out, (dict, list)) and (a.json or a.cmd == "schema"):
        if note and isinstance(out, dict):
            out = {**out, "dry_run": True}
        print(json.dumps(out, ensure_ascii=False, indent=None if a.json else 2))
    elif isinstance(out, dict) and "status" in out and ("entry" in out or "proposal" in out):
        print(text_result(out))
    else:
        print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2))
    if note and not a.json:
        print("dry run - nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
