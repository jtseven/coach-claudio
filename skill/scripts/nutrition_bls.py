#!/usr/bin/env python3
"""Generic BLS (Bundeslebensmittelschlüssel) nutrient provider. Standard library only.

An opt-in lookup of German generic foods (oat flakes, quark, rice, ...) from the
official BLS 4.0 workbook, kept in its own SQLite file - never in the meal ledger and
never mixed with Open Food Facts rows:

    nutrition_bls.py import BLS_4_0_2025_DE.zip      # explicit, one-off
    nutrition_bls.py search haferflocken
    nutrition_bls.py get C133000

The database lives in athlete/state/nutrition-providers/bls.sqlite3 (private, ignored by
git). Nothing downloads or imports on its own. See docs/nutrition-bls.md.
"""
import argparse
import copy
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
import zlib
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import zipfile

import coachconf as cc

# Bump whenever the stored search index or snapshot layout changes: older databases are refused.
SCHEMA_VERSION = 3
LICENSE_ID = "CC-BY-4.0"
DOI = {"4.0": "10.25826/Data20251217-134202-0"}

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# canonical key -> (BLS component code, unit the header must declare)
TARGETS = {
    "energy_kcal": ("ENERCC", "kcal"),
    "protein_g": ("PROT625", "g"),
    "carbs_g": ("CHO", "g"),
    "fat_g": ("FAT", "g"),
    "fiber_g": ("FIBT", "g"),
    "sugars_g": ("SUGAR", "g"),
    "salt_g": ("NACL", "g"),
    "sodium_mg": ("NA", "mg"),
}
# extra components the energy erratum formula needs
ERRATA_INPUTS = {"POLYL": "g", "ALC": "g", "OA": "g", "OLSAC": "g"}
REQUIRED = {code: unit for code, unit in TARGETS.values()} | ERRATA_INPUTS


# BLS 4.0 erratum (valid until BLS 4.1 ships the fix): 411 foods had the available
# oligosaccharides (OLSAC) counted a second time in the energy. The source workbook's own
# component formula still contains that term, so formulas are never executed - the
# corrected energy is recomputed here, from known numbers only.
ERRATUM_URL = "https://blsdb.de/assets/uploads/Erratum_BLS_4_0_DE.pdf"
ERRATUM_FORMULA = "4*PROT625 + 9*FAT + 4*(CHO-POLYL) + 2*FIBT + 7*ALC + 3*OA + 2.4*POLYL"
ERRATUM_INPUTS = ("PROT625", "FAT", "CHO", "POLYL", "FIBT", "ALC", "OA")
ERRATUM_VERSION = "4.0"
DOWNLOAD_URL = "https://blsdb.de/assets/uploads/BLS_4_0_2025_DE.zip"
DOWNLOAD_HOST = "blsdb.de"
DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
# The erratum calls these values too high but names no exact replacement - never invented.
BLOCKED_FOODS = {
    "T104100": "Sardelle roh: fat and energy are too high according to the BLS 4.0 erratum, "
               "which gives no exact replacement value",
}


class BlsError(Exception):
    """Base class; the CLI prints these without a traceback."""


class ImportRejected(BlsError):
    """The input is not an acceptable BLS workbook. The existing database is untouched."""


class NoDatabase(BlsError):
    """No imported BLS database yet."""


class StaleDatabase(BlsError):
    """The database was built by another version of this importer; import again."""


class NotFound(BlsError):
    """No such BLS code."""


def default_db_path():
    return cc.STATE / "nutrition-providers" / "bls.sqlite3"


def attribution(version):
    doi = f" DOI: {DOI[version]}." if version in DOI else ""
    return (f"Max Rubner-Institut (2025): Bundeslebensmittelschlüssel (BLS), Version {version} — "
            f"Deutsche Nährstoffdatenbank. Karlsruhe.{doi} "
            "Lizenz: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/deed.de). "
            "Änderungen: Normalisierung/Import und dokumentierte Errata-Korrekturen.")


# --- reading the workbook ---------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    """Ceilings for an untrusted workbook. The official BLS 4.0 file (14 MB zip, 100 MB sheet XML,
    7 140 rows x 418 columns, compression ratio about 7) is far inside all of them."""
    max_member_bytes: int = 512 * 1024 * 1024    # one XML part, uncompressed
    max_total_bytes: int = 768 * 1024 * 1024     # all XML parts read, uncompressed
    max_ratio: int = 200                         # uncompressed / compressed, for big parts
    ratio_floor_bytes: int = 1024 * 1024         # below this size the ratio is not checked
    max_members: int = 200                       # entries in an archive
    max_rows: int = 200_000
    max_columns: int = 2_000
    max_cell_chars: int = 65_536
    max_small_part_bytes: int = 1024 * 1024      # workbook.xml and its relationships


_DTD = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.I)
_UNSUPPORTED_BOM = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00", b"\x00<", b"<\x00")
_XML_ENCODING = re.compile(rb"""<\?xml[^>]*?encoding\s*=\s*["']([^"']+)""")


class _GuardedReader:
    """Read-only stream over one zip member: counts bytes against a cap, refuses any DOCTYPE or
    ENTITY declaration (no entity expansion is possible without a DTD) and anything but UTF-8."""

    def __init__(self, raw, cap, what):
        self._raw, self._cap, self._what = raw, cap, what
        self._seen, self._tail, self._first = 0, b"", True

    def read(self, size=-1):
        if size is None or size < 0:
            return self._read_all()
        chunk = self._raw.read(min(size, 1 << 20))
        if chunk:
            self._check(chunk)
        return chunk

    def _read_all(self):
        parts = []
        while True:
            chunk = self._raw.read(65_536)
            if not chunk:
                return b"".join(parts)
            self._check(chunk)
            parts.append(chunk)

    def _check(self, chunk):
        self._seen += len(chunk)
        if self._seen > self._cap:
            raise ImportRejected(f"{self._what} is too large (more than {self._cap} bytes uncompressed)")
        if self._first:
            self._first = False
            if chunk.startswith(_UNSUPPORTED_BOM):
                raise ImportRejected(f"{self._what} is not UTF-8 XML")
            declared = _XML_ENCODING.match(chunk.lstrip(b"\xef\xbb\xbf")[:300])
            if declared and declared.group(1).lower() not in (b"utf-8", b"utf8", b"us-ascii"):
                raise ImportRejected(f"{self._what} must be UTF-8 XML, not {declared.group(1).decode('latin-1')}")
        window = self._tail + chunk
        if _DTD.search(window):
            raise ImportRejected(f"{self._what} contains a DOCTYPE/ENTITY declaration; refused")
        self._tail = window[-16:]

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Budget:
    """Running total of the uncompressed bytes of the parts opened from one workbook."""

    def __init__(self, limits):
        self.limits, self.used = limits, 0

    def open(self, zf, name, cap=None):
        infos = [i for i in zf.infolist() if i.filename == name]
        if len(infos) != 1:
            raise ImportRejected(f"{name} is missing from the workbook" if not infos
                                 else f"{name} appears more than once in the archive")
        info, limits = infos[0], self.limits
        cap = limits.max_member_bytes if cap is None else cap
        if info.file_size > cap:
            raise ImportRejected(f"{name} is too large ({info.file_size} bytes, limit {cap})")
        if info.file_size > limits.ratio_floor_bytes and \
                info.file_size > limits.max_ratio * max(info.compress_size, 1):
            raise ImportRejected(f"{name} has a suspicious compression ratio "
                                 f"({info.file_size} from {info.compress_size} bytes)")
        self.used += info.file_size
        if self.used > limits.max_total_bytes:
            raise ImportRejected(f"total uncompressed size of the workbook parts exceeds "
                                 f"{limits.max_total_bytes} bytes")
        return _GuardedReader(zf.open(info), cap, name)

    def xml(self, zf, name):
        with self.open(zf, name, self.limits.max_small_part_bytes) as f:
            return ET.fromstring(f.read())


def _check_archive(zf, limits, what):
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise ImportRejected(f"{what} has more than {limits.max_members} members")
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or "\x00" in name or ".." in name.split("/") \
                or re.match(r"^[A-Za-z]:", name):
            raise ImportRejected(f"{what} has an unsafe member name {name!r}")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _column_index(ref, limits):
    match = re.fullmatch(r"([A-Z]{1,3})(\d+)", ref or "")
    if not match:
        raise ImportRejected(f"unreadable cell reference {ref!r}")
    index = 0
    for ch in match.group(1):
        index = index * 26 + ord(ch) - 64
    if index > limits.max_columns:
        raise ImportRejected(f"the sheet has more than {limits.max_columns} columns")
    return index - 1


def _rich_text(si):
    """Text of a shared string: its own <t> and the runs of rich text, not phonetic <rPh> hints."""
    parts = []
    for child in si:
        if child.tag == MAIN_NS + "t":
            parts.append(child.text or "")
        elif child.tag == MAIN_NS + "r":
            parts.extend(t.text or "" for t in child.findall(MAIN_NS + "t"))
    return "".join(parts)


def _shared_strings(xlsx, budget):
    strings = []
    if not any(i.filename == "xl/sharedStrings.xml" for i in xlsx.infolist()):
        return strings
    with budget.open(xlsx, "xl/sharedStrings.xml") as f:
        for _, el in ET.iterparse(f):
            if el.tag == MAIN_NS + "si":
                text = _rich_text(el)
                if len(text) > budget.limits.max_cell_chars:
                    raise ImportRejected("a shared string is longer than the cell text limit")
                strings.append(text)
                el.clear()
    return strings


def _find_sheet(xlsx, budget):
    """(sheet part name, BLS version) of the BLS data sheet."""
    book = budget.xml(xlsx, "xl/workbook.xml")
    rels = budget.xml(xlsx, "xl/_rels/workbook.xml.rels")
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(REL_NS + "Relationship")}
    for sheet in book.iter(MAIN_NS + "sheet"):
        title = sheet.get("name", "")
        match = re.match(r"BLS_(\d+)_(\d+)_Daten", title)
        if match:
            target = targets.get(sheet.get(DOC_REL_NS + "id"), "")
            if not re.fullmatch(r"worksheets/sheet\d+\.xml", target):
                raise ImportRejected(f"unexpected sheet part {target!r}")
            return "xl/" + target, f"{match.group(1)}.{match.group(2)}"
    raise ImportRejected("no BLS data sheet (expected a sheet named BLS_<major>_<minor>_Daten...)")


def _bounded(text, limits):
    if text is not None and len(text) > limits.max_cell_chars:
        raise ImportRejected("a cell is longer than the cell text limit")
    return text


def _cell_value(cell, shared, limits):
    """(kind, text) for one <c>; formulas are never evaluated, only a cached value is read."""
    kind = cell.get("t")
    if kind == "inlineStr":
        inline = cell.find(MAIN_NS + "is")
        text = "".join(t.text or "" for t in inline.iter(MAIN_NS + "t")) if inline is not None else None
        return "text", _bounded(text, limits)
    v = cell.find(MAIN_NS + "v")
    text = v.text if v is not None else None
    if text is None:
        return "text", None
    _bounded(text, limits)
    if kind == "s":
        try:
            return "text", shared[int(text)]
        except (ValueError, IndexError):
            raise ImportRejected(f"cell {cell.get('r')}: bad shared string index {text!r}") from None
    if kind in (None, "n"):
        return "number", text
    if kind == "str":
        return "text", text
    raise ImportRejected(f"cell {cell.get('r')}: unsupported cell type {kind!r}")


def _iter_rows(xlsx, sheet_part, shared, budget):
    """Yield {column index: (kind, text)} per non-empty row, one row in memory at a time."""
    limits = budget.limits
    rows = 0
    with budget.open(xlsx, sheet_part) as f:
        root = None
        for event, el in ET.iterparse(f, events=("start", "end")):
            if event == "start":
                if root is None:
                    root = el
                continue
            if el.tag == MAIN_NS + "row":
                rows += 1
                if rows > limits.max_rows:
                    raise ImportRejected(f"the sheet has more than {limits.max_rows} rows")
                cells = {}
                for cell in el.findall(MAIN_NS + "c"):
                    kind, text = _cell_value(cell, shared, limits)
                    if text is not None:
                        cells[_column_index(cell.get("r"), limits)] = (kind, text)
                el.clear()
                root.clear()
                if cells:
                    yield cells


HEADER_VALUE = re.compile(r"^(\S+) .+ \[([^/\]]+)/100\s?g\]$")
HEADER_ORIGIN = re.compile(r"^(\S+) Datenherkunft$")
HEADER_REF = re.compile(r"^(\S+) Referenz$")


def _map_header(cells):
    by_name, value, origin, ref = {}, {}, {}, {}
    units = {}
    note = None
    for index, (_, text) in cells.items():
        text = " ".join(text.split())
        if text in ("BLS Code", "Lebensmittelbezeichnung", "Food name"):
            by_name[text] = index
        elif text == "Hinweis":
            note = index
        elif (m := HEADER_ORIGIN.match(text)):
            origin[m.group(1)] = index
        elif (m := HEADER_REF.match(text)):
            ref[m.group(1)] = index
        elif (m := HEADER_VALUE.match(text)):
            if m.group(1) in value:
                raise ImportRejected(f"duplicate column for {m.group(1)}")
            value[m.group(1)] = index
            units[m.group(1)] = m.group(2)
    for needed in ("BLS Code", "Lebensmittelbezeichnung", "Food name"):
        if needed not in by_name:
            raise ImportRejected(f"header column {needed!r} not found")
    for code, unit in REQUIRED.items():
        if code not in value:
            raise ImportRejected(f"header column for component {code} not found")
        if units[code] != unit:
            raise ImportRejected(f"component {code} is in {units[code]}/100g, expected {unit}/100g")
    return {"code": by_name["BLS Code"], "de": by_name["Lebensmittelbezeichnung"],
            "en": by_name["Food name"], "note": note, "value": value, "origin": origin, "ref": ref}


MARKERS = {"TR": "trace", "<LOD": "lt_LOD", "<LOQ": "lt_LOQ", "<LOD or <LOQ": "lt_LOD_or_LOQ"}
DECIMAL = re.compile(r"\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
LIMIT = re.compile(r"<\s*(" + DECIMAL.pattern + ")")


def _number(text, where):
    """A finite, non-negative number; anything else makes the whole import invalid."""
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        raise ImportRejected(f"{where}: {text!r} is not a number") from None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        raise ImportRejected(f"{where}: {text!r} is not a finite non-negative number")
    return int(number) if number.is_integer() else number


def _read_cell(cell, where):
    """-> (qualifier, value, limit, raw). Markers keep their meaning and never become 0."""
    if cell is None:
        return "missing", None, None, None
    kind, raw = cell
    raw = raw.strip()
    if kind == "number":
        return "exact", _number(raw, where), None, raw
    squeezed = " ".join(raw.split())
    if squeezed in ("", "-"):
        return "missing", None, None, raw or None
    if squeezed in MARKERS:
        return MARKERS[squeezed], None, None, squeezed
    if (m := LIMIT.fullmatch(squeezed)):
        return "lt_limit", None, _number(m.group(1), where), squeezed
    if DECIMAL.fullmatch(squeezed):
        return "exact", _number(squeezed, where), None, raw
    raise ImportRejected(f"{where}: unrecognised value {raw!r}")


def _nutrient(code, unit, cols, cells, where):
    qualifier, value, limit, raw = _read_cell(cells.get(cols["value"][code]), f"{where} {code}")

    def text(table):
        cell = cells.get(table[code]) if code in table else None
        if cell is None or cell[1].strip() in ("", "-"):
            return None
        return cell[1].strip()
    entry = {"value": value, "unit": unit, "qualifier": qualifier, "raw": raw, "source_code": code,
             "provenance": {"origin": text(cols["origin"]), "reference": text(cols["ref"])}}
    if limit is not None:
        entry["limit"] = limit
    return entry


def corrected_energy_kcal(values):
    """Erratum energy from {code: number}; None unless every input is a known number.
    Decimal arithmetic on the numbers as written, no 0 for an unknown input."""
    try:
        d = {code: Decimal(str(values[code]).replace(",", ".")) for code in ERRATUM_INPUTS}
        if any(not v.is_finite() or v < 0 or abs(v.adjusted()) > 1000 for v in d.values()):
            return None
        if d["CHO"] < d["POLYL"]:
            return None
        with localcontext() as ctx:
            ctx.prec = max(50, sum(len(v.as_tuple().digits) for v in d.values()) + 32)
            exact = (4 * d["PROT625"] + 9 * d["FAT"] + 4 * (d["CHO"] - d["POLYL"])
                     + 2 * d["FIBT"] + 7 * d["ALC"] + 3 * d["OA"] + Decimal("2.4") * d["POLYL"])
        if not exact.is_finite() or exact < 0 or float(exact) == float("inf"):
            return None
        return exact
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def _plain(number):
    number = float(number)
    return int(number) if number.is_integer() else number


def _apply_erratum(source, energy, warnings, reasons):
    """Correct or flag the energy of a food that has OLSAC counted twice; -> 'ok' or 'review'."""
    olsac = source["OLSAC"]
    if olsac["qualifier"] != "exact" or not olsac["value"] > 0:
        return "ok"
    errata = {"affected": True, "corrected": False, "source": ERRATUM_URL, "formula": ERRATUM_FORMULA}
    energy["errata"] = errata
    known = {code: source[code]["value"] for code in ERRATUM_INPUTS if source[code]["qualifier"] == "exact"}
    unknown = [code for code in ERRATUM_INPUTS if code not in known]
    if unknown:
        errata["unknown_inputs"] = unknown
        warnings.append("BLS 4.0 erratum: this energy double-counts oligosaccharides and cannot be "
                        f"recomputed (unknown {', '.join(unknown)}); the source value is kept and "
                        "probably too high")
        reasons.append(f"energy not correctable: unknown {', '.join(unknown)}")
        return "review"
    input_sources = {code: copy.deepcopy(source[code]) for code in ERRATUM_INPUTS}
    errata["input_sources"] = input_sources
    exact = corrected_energy_kcal({code: entry["raw"] for code, entry in input_sources.items()})
    if exact is None:
        reason = "energy not correctable: invalid inputs (requires CHO >= POLYL) or non-finite result"
        errata["invalid_inputs"] = True
        warnings.append("BLS 4.0 erratum: " + reason + "; source energy kept, not for automatic use")
        reasons.append(reason)
        return "review"
    energy["raw_value"] = energy["value"]
    energy["value"] = int(exact.to_integral_value(rounding=ROUND_HALF_UP))
    energy["qualifier"] = "corrected"
    energy["provenance"] = dict(energy["provenance"], method="BLS 4.0 erratum formula without OLSAC")
    errata.update(corrected=True, inputs=known, unrounded=_plain(exact))
    warnings.append(f"BLS 4.0 erratum: energy recomputed to {energy['value']} kcal "
                    f"(source value {energy['raw_value']} kcal counted oligosaccharides twice)")
    return "ok"


def _derive_salt(nutrients):
    salt, sodium = nutrients["salt_g"], nutrients["sodium_mg"]
    if salt["qualifier"] != "missing" or sodium["qualifier"] != "exact":
        return
    value = Decimal(repr(sodium["value"])) * Decimal("2.5") / 1000
    nutrients["salt_g"] = {
        "value": _plain(value), "unit": "g", "qualifier": "derived", "raw": None,
        "source_code": "NACL", "derived": True, "derived_from": ["sodium_mg"],
        "provenance": {"origin": "derived from sodium", "reference": None,
                       "method": "sodium_mg / 1000 * 2.5 (salt = 2.5 x sodium; not an independent "
                                 "measurement)"}}


def _snapshot(record, version):
    source = record["source"]
    nutrients = {key: copy.deepcopy(source[code]) for key, (code, _) in TARGETS.items()}
    warnings, reasons, status = [], [], "ok"
    if version == ERRATUM_VERSION:
        status = _apply_erratum(source, nutrients["energy_kcal"], warnings, reasons)
        if record["code"] in BLOCKED_FOODS:
            status = "blocked"
            reasons.append(f"{record['code']}: {BLOCKED_FOODS[record['code']]}")
            warnings.append(reasons[-1])
            for key in ("fat_g", "energy_kcal"):
                nutrients[key]["unreliable"] = True
    _derive_salt(nutrients)
    return {"provider": "bls", "source_id": record["code"], "name": record["name"],
            "name_en": record["name_en"], "source_note": record["note"],
            "basis": {"quantity": 100, "unit": "g", "per": "100g"}, "nutrients": nutrients,
            "automatic_use": {"status": status, "reasons": reasons}, "warnings": warnings}


def _records(xlsx, sheet_part, shared, budget):
    cols = None
    seen = set()
    for number, cells in enumerate(_iter_rows(xlsx, sheet_part, shared, budget), start=1):
        if cols is None:
            cols = _map_header(cells)
            continue
        code = (cells.get(cols["code"]) or ("", ""))[1].strip()
        name = (cells.get(cols["de"]) or ("", ""))[1].strip()
        where = f"row {number}"
        if not code or not name:
            raise ImportRejected(f"{where}: BLS code and name are required")
        if code in seen:
            raise ImportRejected(f"{where}: duplicate BLS code {code}")
        seen.add(code)
        source = {code_: _nutrient(code_, REQUIRED[code_], cols, cells, f"{where} ({code})")
                  for code_ in REQUIRED}
        note = (cells.get(cols["note"]) or (None, None))[1] if cols["note"] is not None else None
        english = (cells.get(cols["en"]) or ("", ""))[1].strip()
        yield {"code": code, "name": name, "name_en": english, "note": note, "source": source}
    if cols is None:
        raise ImportRejected("the workbook has no header row")


# --- the provider database --------------------------------------------------------------

def _create_schema(db):
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE foods (
            code TEXT PRIMARY KEY, name_de TEXT NOT NULL, name_en TEXT NOT NULL,
            prep TEXT NOT NULL, status TEXT NOT NULL,
            hay_a TEXT NOT NULL, hay_b TEXT NOT NULL, snapshot TEXT NOT NULL);
    """)


def _private_parent(path):
    """Create missing directories privately; never chmod an existing user directory.

    Refuse symlinks anywhere in the path and untrusted writable ancestors. A root-owned
    sticky directory (e.g. /tmp) is allowed only as an ancestor, never as the output parent.
    Same-uid malicious processes are outside this filesystem permission boundary.
    """
    path = Path(os.path.abspath(path))
    uid = os.geteuid()
    for current in [*reversed(path.parents), path]:
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid) \
                or (info.st_mode & 0o022 and not sticky_root):
            raise BlsError(f"unsafe output directory {current}: ownership, permissions or symlink")
        if current == path and (info.st_uid != uid or info.st_mode & 0o077):
            raise BlsError(f"output directory {current} must be owned by you and private (0700)")
    return path


def _private_stage(parent, name, suffix):
    # mkstemp uses O_CREAT|O_EXCL and 0600: pre-existing symlinks are never opened.
    fd, path = tempfile.mkstemp(prefix=f".{name}.", suffix=suffix, dir=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise BlsError("staging file is not a private owned regular file")
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    return fd, Path(path)


def import_bls(source, db_path=None, limits=None):
    """Replace the BLS database with the contents of an official workbook (.xlsx, or the
    official .zip holding it). All-or-nothing: on any problem the old database stays."""
    source = Path(source)
    db_path = Path(db_path) if db_path else default_db_path()
    limits = limits or Limits()
    if not source.is_file():
        raise ImportRejected(f"{source} is not a file")
    sha = _sha256_file(source)
    parent = _private_parent(db_path.parent)
    fd, staging = _private_stage(parent, db_path.name, ".new")
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory(prefix="bls-import-") as scratch:
            xlsx_path, member = _locate_workbook(source, Path(scratch), limits)
            with zipfile.ZipFile(xlsx_path) as xlsx:
                _check_archive(xlsx, limits, "the workbook")
                budget = _Budget(limits)
                sheet_part, version = _find_sheet(xlsx, budget)
                shared = _shared_strings(xlsx, budget)
                db = sqlite3.connect(staging)
                try:
                    _create_schema(db)
                    count = 0
                    for record in _records(xlsx, sheet_part, shared, budget):
                        snap = _snapshot(record, version)
                        db.execute("INSERT INTO foods VALUES (?,?,?,?,?,?,?,?)",
                                   (record["code"], record["name"], record["name_en"],
                                    preparation_of(record["name"], record["name_en"]),
                                    snap["automatic_use"]["status"],
                                    _haystack(record, False), _haystack(record, True),
                                    json.dumps(snap, ensure_ascii=False)))
                        count += 1
                    if count == 0:
                        raise ImportRejected("the workbook holds no foods")
                    meta = {"schema_version": str(SCHEMA_VERSION), "bls_version": version,
                            "source_sha256": sha, "source_member": member or "", "foods": str(count),
                            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "license_id": LICENSE_ID, "attribution": attribution(version)}
                    db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
                    db.commit()
                finally:
                    db.close()
        os.replace(staging, db_path)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError,
            ET.ParseError) as exc:
        raise ImportRejected(f"{source.name} is not a readable workbook: {exc}") from exc
    finally:
        staging.unlink(missing_ok=True)
    return {"foods": count, "bls_version": version, "source_sha256": sha, "source_member": member,
            "attribution": attribution(version), "database": str(db_path)}


def _locate_workbook(source, scratch, limits):
    """(path of an .xlsx, name of the zip member it came from or None). The archive is never
    extracted: at most the one data workbook is copied, to a fixed name, under a size cap."""
    try:
        outer = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise ImportRejected(f"{source.name} is not a zip/xlsx file") from None
    with outer:
        _check_archive(outer, limits, source.name)
        names = outer.namelist()
        if "xl/workbook.xml" in names:
            return source, None
        candidates = [n for n in names if re.fullmatch(r"(?:[^/]+/)*BLS_\d+_\d+_Daten_[^/]*\.xlsx", n)]
        if len(candidates) != 1:
            raise ImportRejected("expected exactly one BLS data workbook (BLS_x_y_Daten_*.xlsx) "
                                 f"in the zip, found {len(candidates)}")
        budget = _Budget(limits)
        target = scratch / "data.xlsx"
        with budget.open(outer, candidates[0]) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return target, candidates[0]


def _connect(db_path):
    db_path = Path(db_path) if db_path else default_db_path()
    if not db_path.is_file():
        raise NoDatabase(f"no BLS database at {db_path}; import the official workbook first "
                         "(nutrition_bls.py import <BLS zip or xlsx>)")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        version = _meta(db).get("schema_version")
    except sqlite3.DatabaseError:
        version = None
    if version != str(SCHEMA_VERSION):
        db.close()
        raise StaleDatabase(f"{db_path} was built by another version of this importer; "
                            "import the BLS workbook again (nutrition_bls.py import <zip or xlsx>)")
    return db


def _meta(db):
    return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM meta")}


# --- searching --------------------------------------------------------------------------

def fold(text, digraph=False):
    """Lower-case, no diacritics, words separated by single spaces; a hyphen between letters joins
    them (German hyphen compounds are one word). ß is always 'ss'; with
    digraph=True ä/ö/ü become ae/oe/ue (how Germans type them without umlauts) instead of a/o/u."""
    text = re.sub(r"(?<=[^\W\d_])-(?=[^\W\d_])", "", text.casefold())
    if digraph:
        text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _haystack(record, digraph):
    """German name, its compound form (spaces removed), English name, its compound form."""
    de, en = fold(record["name"], digraph), fold(record["name_en"], digraph)
    return "|".join((de, de.replace(" ", ""), en, en.replace(" ", "")))


COOKED_WORDS = {"gekocht", "gedunstet", "gegart", "gebraten", "gebacken", "gedampft", "geschmort",
                "frittiert", "gegrillt", "blanchiert", "uberbacken", "pochiert", "gebruht",
                "cooked", "boiled", "steamed", "fried", "baked", "grilled", "roasted", "braised",
                "stewed", "poached", "blanched"}
RAW_WORDS = {"roh", "raw"}


def preparation_of(name_de, name_en=""):
    """'raw', 'cooked' or 'unspecified', from whole words of the BLS names (cooked wins)."""
    words = set(fold(name_de).split()) | set(fold(name_en).split())
    if words & COOKED_WORDS:
        return "cooked"
    if words & RAW_WORDS:
        return "raw"
    return "unspecified"


MAX_QUERY_TOKENS = 8
MAX_QUERY_CHARS = 120
TIERS = ("exact", "word", "suffix", "prefix", "substring")


def _word_level(token, words):
    """1 if the token is one whole word or several whole words run together ('haferflocken' for
    'hafer flocken'); 2 if it ends a compound word ('quark' in 'speisequark': in German the last
    part names the thing); 3 if it starts a word; 4 if it sits inside one; None if absent."""
    for i in range(len(words)):
        joined = ""
        for word in words[i:]:
            joined += word
            if joined == token:
                return 1
            if not token.startswith(joined):
                break
    if len(token) >= 3 and any(word.endswith(token) for word in words):
        return 2
    if any("".join(words[i:]).startswith(token) for i in range(len(words))):
        return 3
    return 4 if token in "".join(words) or token in " ".join(words) else None


def _tier(tokens, query, hay):
    """(German level, English level) of the query in one name variant; None = a word is missing."""
    de, de_compact, en, en_compact = hay.split("|")
    compact_query = query.replace(" ", "")
    levels = []
    for name, compact in ((de, de_compact), (en, en_compact)):
        if not name:
            levels.append(None)
        elif name == query or compact == compact_query:
            levels.append(0)
        else:
            per_token = [_word_level(token, name.split()) for token in tokens]
            levels.append(None if None in per_token else max(per_token))
    return tuple(levels)


def search(query, db_path=None, limit=25, preparation=None):
    """Candidate foods for a query - every word must match, nothing is fuzzy, and nothing is
    picked for the caller: they choose a code and pass it to get()."""
    if len(query) > MAX_QUERY_CHARS:
        raise BlsError(f"search text too long (limit {MAX_QUERY_CHARS} characters)")
    variants = [(fold(query), False), (fold(query, True), True)]
    tokens = variants[0][0].split()
    if not tokens:
        raise BlsError("nothing to search for: give at least one letter or digit")
    if len(tokens) > MAX_QUERY_TOKENS:
        raise BlsError(f"too many search words (limit {MAX_QUERY_TOKENS})")
    if preparation not in (None, "raw", "cooked", "unspecified"):
        raise BlsError("preparation must be raw, cooked or unspecified")
    db = _connect(db_path)
    try:
        meta = _meta(db)
        rows = db.execute("SELECT code, name_de, name_en, prep, status, hay_a, hay_b, snapshot FROM foods "
                          "WHERE (? IS NULL OR prep = ?)", (preparation, preparation)).fetchall()
    finally:
        db.close()
    hits = []
    for row in rows:
        best = [None, None]
        for (folded, _), hay in zip(variants, (row["hay_a"], row["hay_b"])):
            for i, level in enumerate(_tier(folded.split(), folded, hay)):
                if level is not None and (best[i] is None or level < best[i]):
                    best[i] = level
        if row["code"].casefold() == query.strip().casefold():
            best[0] = 0
        if best != [None, None]:
            hits.append((best, row))
    first = tokens[0]

    def rank(hit):
        (de, en), row = hit
        # the German name decides; an English-only match comes after every German one
        primary = de if de is not None else 10 + en
        compound = row["hay_a"].split("|")[1].startswith(first)
        length = len(row["name_de"] if de is not None else row["name_en"])
        return (primary, not compound, length, row["name_de"], row["code"])
    hits.sort(key=rank)
    results = []
    for (de, en), row in hits[:max(1, limit)]:
        nutrients = json.loads(row["snapshot"])["nutrients"]
        results.append({
            "code": row["code"], "name": row["name_de"], "name_en": row["name_en"],
            "preparation": row["prep"],
            "title": f"{row['name_de']} [{row['prep']}] · BLS {row['code']}",
            "match": TIERS[de if de is not None else en], "matched_in": "de" if de is not None else "en",
            "automatic_use": row["status"],
            "preview": {k: nutrients[k]["value"] for k in ("energy_kcal", "protein_g", "carbs_g", "fat_g")}})
    return {"query": query, "results": results, "total": len(hits), "truncated": len(hits) > len(results),
            "selection_required": True, "bls_version": meta["bls_version"],
            "license_id": meta["license_id"], "attribution": meta["attribution"]}


def get(code, db_path=None):
    """The canonical per-100 g snapshot of one BLS code (exact match only)."""
    db = _connect(db_path)
    try:
        row = db.execute("SELECT snapshot FROM foods WHERE code = ?", (str(code).strip().upper(),)).fetchone()
        if row is None:
            raise NotFound(f"no BLS food with code {code!r}")
        snap = json.loads(row["snapshot"])
        meta = _meta(db)
    finally:
        db.close()
    snap.update({"source_version": meta["bls_version"], "license_id": meta["license_id"],
                 "attribution": meta["attribution"],
                 "source": {"sha256": meta["source_sha256"], "imported_at": meta["imported_at"]}})
    return snap


def info(db_path=None):
    """What the database holds and where it came from."""
    db = _connect(db_path)
    try:
        meta = _meta(db)
    finally:
        db.close()
    meta["foods"] = int(meta["foods"])
    return meta


# --- the one network step, only on explicit request -----------------------------------------

def _check_download_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != DOWNLOAD_HOST \
            or parts.username or parts.password or parts.port not in (None, 443):
        raise BlsError(f"refusing to download from {url!r}: only https://{DOWNLOAD_HOST}/ is allowed")
    return parts


def download(dest_dir, url=DOWNLOAD_URL, opener=None, max_bytes=DOWNLOAD_MAX_BYTES, timeout=60):
    """Fetch the official BLS zip into dest_dir (private, created on demand). Nothing else is
    ever downloaded; the result is not imported - that is a separate explicit step."""
    parts = _check_download_url(url)
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "coach-claudio-bls-import"})
    if opener is None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise BlsError("BLS download redirects are refused; no redirect target was requested")
        opener = urllib.request.build_opener(NoRedirect()).open
    name = Path(parts.path).name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", name):
        raise BlsError(f"unexpected download file name {name!r}")
    dest_dir = _private_parent(dest_dir)
    fd, part = _private_stage(dest_dir, name, ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as out, opener(request, timeout=timeout) as response:
            _check_download_url(response.geturl() or url)
            announced = response.headers.get("Content-Length")
            if announced and announced.isdigit() and int(announced) > max_bytes:
                raise BlsError(f"download is {announced} bytes, more than the {max_bytes} byte limit")
            while chunk := response.read(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise BlsError(f"download exceeds the {max_bytes} byte limit")
                digest.update(chunk)
                out.write(chunk)
        with open(part, "rb") as check:
            if check.read(4) != b"PK\x03\x04":
                raise BlsError("the download is not a zip file (a login or error page?)")
        target = dest_dir / name
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return {"path": str(target), "sha256": digest.hexdigest(), "bytes": size, "url": url}


# --- command line ---------------------------------------------------------------------------

def _shown(entry):
    if entry["qualifier"] in ("exact", "corrected", "derived"):
        return f"{entry['value']} {entry['unit']}"
    if entry["qualifier"] == "missing":
        return "unknown"
    if entry["qualifier"] == "lt_limit":
        return f"< {entry['limit']} {entry['unit']}"
    return f"{entry['raw']} ({entry['qualifier']}, not zero)"


STATUS_TAG = {"ok": "", "review": "  [REVIEW: check warnings]", "blocked": "  [BLOCKED for automatic use]"}


def _print_search(out):
    print(f"BLS {out['bls_version']}: {out['total']} match(es) for {out['query']!r} - choose one by code; "
          "nothing is picked for you")
    for r in out["results"]:
        p = r["preview"]
        values = ", ".join(f"{k} {'n/a' if v is None else v}" for k, v in p.items())
        print(f"  {r['title']}{STATUS_TAG[r['automatic_use']]}\n      per 100 g: {values}")
    if out["truncated"]:
        print(f"  ... {out['total'] - len(out['results'])} more; narrow the search or raise --limit")
    if not out["results"]:
        print("  (no match - words must be contained in the BLS name; try another spelling or a part of the word)")
    print(f"\n{out['attribution']}")


def _print_food(snap):
    print(f"{snap['name']}  ({snap['name_en']})  - BLS {snap['source_id']}, version {snap['source_version']}")
    print("per 100 g edible portion")
    for key, entry in snap["nutrients"].items():
        extra = []
        if entry.get("raw_value") is not None:
            extra.append(f"source value {entry['raw_value']} {entry['unit']}")
        if entry.get("derived"):
            extra.append("derived from sodium")
        if entry.get("unreliable"):
            extra.append("UNRELIABLE per erratum")
        origin = entry["provenance"].get("origin")
        if origin:
            extra.append(origin)
        print(f"  {key:<11} {_shown(entry):<26} {'; '.join(extra)}")
    use = snap["automatic_use"]
    print(f"automatic use: {use['status']}" + (f" - {'; '.join(use['reasons'])}" if use["reasons"] else ""))
    for warning in snap["warnings"]:
        print(f"warning: {warning}")
    if snap.get("source_note"):
        print(f"BLS note: {snap['source_note']}")
    print(f"\n{snap['attribution']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nutrition_bls.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--db", help=f"database file (default {default_db_path()})")
        return p

    p = add("import", "replace the database with an official BLS workbook (.zip or .xlsx)")
    p.add_argument("source")
    p = add("search", "list BLS foods whose name contains every word")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--prep", choices=("raw", "cooked", "unspecified"))
    p.add_argument("--json", action="store_true")
    p = add("get", "the per-100 g record of one BLS code")
    p.add_argument("code")
    p.add_argument("--json", action="store_true")
    add("info", "what is imported")
    p = add("download", "fetch the official BLS zip from blsdb.de (explicit opt-in)")
    p.add_argument("--yes", action="store_true", help="confirm the ~14 MB download")
    p.add_argument("--import", dest="then_import", action="store_true", help="import it right away")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            r = import_bls(args.source, args.db)
            print(f"imported {r['foods']} foods - BLS {r['bls_version']}\nsha256  {r['source_sha256']}\n"
                  f"database  {r['database']}\n\n{r['attribution']}")
        elif args.command == "search":
            out = search(args.query, args.db, limit=args.limit, preparation=args.prep)
            print(json.dumps(out, ensure_ascii=False, indent=2)) if args.json else _print_search(out)
        elif args.command == "get":
            snap = get(args.code, args.db)
            print(json.dumps(snap, ensure_ascii=False, indent=2)) if args.json else _print_food(snap)
        elif args.command == "info":
            i = info(args.db)
            print(f"{i['foods']} foods - BLS {i['bls_version']} - {i['license_id']}\n"
                  f"imported {i['imported_at']}\nsha256  {i['source_sha256']}\n\n{i['attribution']}")
        elif args.command == "download":
            target = cc.STATE / "nutrition-providers" / "downloads"
            if not args.yes:
                raise BlsError(f"this would download {DOWNLOAD_URL} (about 14 MB) into {target}; "
                               "nothing was fetched. Re-run with --yes to confirm.")
            r = download(target)
            print(f"downloaded {r['bytes']} bytes\nsha256  {r['sha256']}\nfile  {r['path']}")
            if args.then_import:
                print(f"imported {import_bls(r['path'], args.db)['foods']} foods")
            else:
                print("not imported yet: nutrition_bls.py import " + r["path"])
        sys.stdout.flush()
    except BlsError as exc:
        print(f"nutrition_bls: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:     # `... | head`: the reader left, which is fine
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


if __name__ == "__main__":
    sys.exit(main())
