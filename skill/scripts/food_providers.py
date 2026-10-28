"""Food data providers and their cache. Standard library only.

A provider turns a GTIN or local BLS reference into product data; text search
returns candidates whose identity must be checked before booking.
It never invents one: not found, an outage or a rate limit is an error the
caller has to handle - by asking the athlete for a label photo, or waiting -
and never a generic fallback with made-up values.

Here: read-only OFF v3.6 with an explicitly selected legacy v2 parser, official
Search-a-licious text search, and adaptation of the standalone local BLS
module. The application explicitly selects v3.6; the old Python constructor
default remains v2 for existing callers. No invented data or database copy.

The provider cache is its own SQLite file, separate from the private food
ledger: it holds public product data under the provider's licence, and it
can be deleted at any time.
"""

import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
import urllib.error
import urllib.parse
import urllib.request

import nutrition_core as core

MAX_BODY = 2 * 1024 * 1024
MAX_PER_MINUTE = 10          # our own ceiling, well under OFF's published limit
FOUND_TTL = 30 * 86400
NOT_FOUND_TTL = 86400
USER_AGENT = "ClaudeRunningCoach-Nutrition/1.0"


class ProviderError(core.LookupFailed):
    """kind: not_found | rate_limited | unavailable | bad_response | offline"""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could point anywhere; the request is pinned to one host."""

    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(url, headers, timeout):
    """GET url -> (status, headers, body). Never follows redirects, reads at
    most MAX_BODY + 1 bytes. Network failures propagate as OSError."""
    handlers = [_NoRedirect()]
    if urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1"):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        body = e.read(MAX_BODY + 1) if e.fp else b""
        return e.code, dict(e.headers or {}), body


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------


class Provider:
    """The interface every food data source implements."""

    name = ""
    attribution = ""

    def lookup_barcode(self, code):
        """Normalised product dict for a GTIN, or ProviderError."""
        raise NotImplementedError


def _pinned_base(url, official):
    """The official https origin, or a loopback http origin for tests. No
    path, credentials, query or other host - incoming data never picks it."""
    u = urllib.parse.urlsplit(url)
    if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        raise ValueError(f"provider base URL must be a bare origin: {url!r}")
    if u.scheme == "https" and u.netloc == official:
        return f"https://{official}"
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost") and u.port:
        return f"http://{u.hostname}:{u.port}"
    raise ValueError(f"provider base URL must be https://{official} (or loopback for tests)")


OFF_FIELDS = ("code,product_name,product_name_{lc},generic_name,generic_name_{lc},brands,"
              "quantity,product_quantity,product_quantity_unit,serving_size,serving_quantity,"
              "serving_quantity_unit,nutrition_data_per,nutriments")
# OFF nutriment name -> canonical (or core input-only) key.
OFF_NUTRIMENTS = (("energy-kcal", "energy_kcal"), ("energy-kj", "energy_kj"),
                  ("proteins", "protein_g"), ("carbohydrates", "carbohydrate_g"),
                  ("sugars", "sugars_g"), ("fat", "fat_g"), ("fiber", "fiber_g"),
                  ("salt", "salt_g"), ("sodium", "sodium_g"))
QTY_TOKEN = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|mg|g|ml|cl|dl|l)\b", re.I)


class OpenFoodFacts(Provider):
    """Open Food Facts product API v2, read-only.

    GET {base}/api/v2/product/{gtin}?fields=...&cc=de&lc=de with a custom
    User-Agent (OFF asks every app to identify itself) and a finite timeout.
    """

    name = "openfoodfacts"
    host = "world.openfoodfacts.org"
    attribution = ("Open Food Facts (https://world.openfoodfacts.org): database under the "
                   "Open Database License (ODbL 1.0), contents under the Database Contents "
                   "License (DbCL 1.0)")

    def __init__(self, country="de", language="de", contact="", timeout=10,
                 transport=None, base_url=None, api_version="v2"):
        if not isinstance(country, str) or not re.fullmatch(r"[a-z]{2}|world", country):
            raise ValueError("country: a lowercase ISO 3166-1 alpha-2 code, e.g. 'de', or 'world'")
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
            raise ValueError("language: a lowercase ISO 639-1 code, e.g. 'de'")
        if not isinstance(contact, str) or len(contact) > 100 or not contact.isprintable():
            raise ValueError("contact: one printable line, at most 100 characters")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= 30):
            raise ValueError("timeout: seconds, more than 0 and at most 30")
        if api_version not in ("v2", "v3.6"):
            raise ValueError("api_version must be v3.6 or explicit legacy v2")
        self.api_version = api_version
        self.country, self.language, self.timeout = country, language, float(timeout)
        self.user_agent = USER_AGENT + (f" ({contact})" if contact else " (self-hosted)")
        self.transport = transport or urllib_transport
        self.base = _pinned_base(base_url or f"https://{self.host}", self.host)

    def url(self, code):
        fields = OFF_FIELDS.format(lc=self.language)
        if self.api_version == "v3.6":
            fields = (f"code,product_name,product_name_{self.language},brands,quantity,"
                      "serving_size,nutrition,schema_version,data_quality_errors_tags")
        query = urllib.parse.urlencode({"fields": fields, "cc": self.country, "lc": self.language})
        suffix = ".json" if self.api_version == "v3.6" else ""
        return f"{self.base}/api/{self.api_version}/product/{code}{suffix}?{query}"

    def search(self, query, limit=5):
        """Official Search-a-licious GET /search. Hits are candidates, not meals."""
        query = core._text(query, "query", 200, required=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise core.InvalidInput("search limit must be 1..5")
        countries = {"de": "germany", "at": "austria", "ch": "switzerland",
                     "fr": "france", "gb": "united-kingdom", "us": "united-states"}
        if self.country != "world" and self.country not in countries:
            raise core.InvalidInput("OFF search country not mapped; use world or a supported country")
        # Literal terms: Search-a-licious matches nothing for any quoted phrase
        # (observed Oct 2026), so escape every query-syntax character instead
        # and lowercase the operator words.
        literal = re.sub(r'[+\-=&|><!(){}\[\]^"~*?:\\/]', r'\\\g<0>', query)
        literal = re.sub(r'\b(AND|OR|NOT)\b', lambda m: m[0].lower(), literal)
        if self.country != "world":
            literal += f' countries_tags:"en:{countries[self.country]}"'
        params = {"q": literal, "langs": ",".join(dict.fromkeys((self.language, "en"))),
                  "page_size": limit, "page": 1,
                  "fields": f"code,product_name,product_name_{self.language},quantity,brands,countries_tags"}
        url = "https://search.openfoodfacts.org/search?" + urllib.parse.urlencode(params)
        try:
            status, headers, body = self.transport(url, {"User-Agent": self.user_agent,
                                                       "Accept": "application/json"}, self.timeout)
        except (OSError, http.client.HTTPException) as e:
            raise ProviderError("unavailable", f"OFF search unreachable: {e}") from None
        if status == 429:
            raise ProviderError("rate_limited", "OFF search rate limited; try later")
        if status != 200:
            raise ProviderError("unavailable", f"OFF search answered HTTP {status}; ask for barcode or label")
        if len(body) > MAX_BODY:
            raise ProviderError("bad_response", "OFF search response too large")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("bad_response", "OFF search sent no JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise ProviderError("bad_response", "OFF search response has no hits array")
        if data.get("timed_out"):
            raise ProviderError("unavailable", "OFF search timed out; incomplete results")
        results = []
        for hit in data["hits"][:limit]:
            if not isinstance(hit, dict):
                raise ProviderError("bad_response", "OFF search malformed hit")
            try:
                code = core.gtin(hit.get("code"))
            except core.InvalidInput:
                continue
            brands = hit.get("brands") or []
            if isinstance(brands, str):
                brands = brands.split(",")
            brand = ", ".join(b.strip() for b in brands if isinstance(b, str) and b.strip())
            results.append({"provider": self.name, "code": code,
                            "name": hit.get(f"product_name_{self.language}") or hit.get("product_name"),
                            "brand": brand, "quantity": hit.get("quantity"),
                            "attribution": self.attribution})
        return {"query": query, "results": results, "count": data.get("count"),
                "is_count_exact": data.get("is_count_exact"), "selection_required": True,
                "attribution": self.attribution}

    def lookup_barcode(self, code):
        code = core.gtin(code)
        try:
            status, headers, body = self.transport(
                self.url(code), {"User-Agent": self.user_agent, "Accept": "application/json"},
                self.timeout)
        except (OSError, http.client.HTTPException) as e:
            raise ProviderError("unavailable", f"Open Food Facts unreachable: {e}") from None
        if len(body) > MAX_BODY:
            raise ProviderError("bad_response", "Open Food Facts response too large")
        if status == 429:
            wait = {k.lower(): v for k, v in headers.items()}.get("retry-after", "later")
            raise ProviderError("rate_limited", f"Open Food Facts rate limit - retry after {wait}")
        if status == 404:
            raise ProviderError("not_found", f"{code} is not in Open Food Facts")
        if status != 200:
            raise ProviderError("unavailable", f"Open Food Facts answered HTTP {status}")
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("bad_response", "Open Food Facts sent no JSON") from None
        if not isinstance(data, dict):
            raise ProviderError("bad_response", "Open Food Facts sent unexpected JSON")
        product = data.get("product")
        if data.get("status") == 0 or not isinstance(product, dict):
            raise ProviderError("not_found", f"{code} is not in Open Food Facts")
        returned = str(data.get("code") or product.get("code") or "")
        if returned.lstrip("0") != code.lstrip("0"):
            raise ProviderError("bad_response", f"asked for {code}, got {returned or 'no code'}")
        if self.api_version == "v3.6":
            return self.normalise_v36(code, product)
        return self.normalise(code, product)

    def normalise_v36(self, code, p):
        """Only one coherent as-sold packaging set, never ingredient estimates.

        v3.6 aggregate source_index points into input_sets. Do not combine
        preparation states, source sets, bases or a packaging and estimate row.
        """
        nutrition = p.get("nutrition")
        if not isinstance(nutrition, dict):
            raise ProviderError("bad_response", "OFF v3.6 nutrition structure missing; no v2 fallback")
        agg = nutrition.get("aggregated_set") or {}
        if not isinstance(agg, dict) or not isinstance(agg.get("nutrients", {}), dict):
            raise ProviderError("bad_response", "OFF v3.6 malformed aggregate")
        basis = agg.get("per") if agg.get("per") in ("100g", "100ml") else None
        raw, warnings, selected = {}, [], None
        if agg.get("preparation") == "as_sold" and basis:
            for off, key in OFF_NUTRIMENTS:
                n = agg.get("nutrients", {}).get(off)
                if not isinstance(n, dict) or n.get("source") != "packaging":
                    continue
                if n.get("source_per") != basis or n.get("modifier") not in (None, "", "<"):
                    continue
                index = n.get("source_index")
                if selected is None:
                    selected = index
                if index != selected:
                    warnings.append(f"ignored {off} from different packaging set")
                    continue
                expected = "kcal" if key == "energy_kcal" else "kJ" if key == "energy_kj" else "g"
                unit = n.get("unit")
                if unit != expected and not (expected == "g" and unit == "mg"):
                    warnings.append(f"ignored {off}: unexpected unit {unit!r}")
                    continue
                try:
                    value = core.parse_number(n.get("value"), off)
                    if unit == "mg":
                        value /= 1000
                    raw[key] = ("<" if n.get("modifier") == "<" else "") + str(value)
                except core.InvalidInput:
                    warnings.append(f"ignored invalid {off}")
        else:
            warnings.append("no supported as-sold packaging basis")
        # Reuse metadata/serving validation, not the legacy nutrient parser.
        meta = self.normalise(code, {k: v for k, v in p.items() if k != "nutriments"})
        # Convert qualified input-only nutrients before canonicalization:
        # parse_nutrients' numeric zero is only a lower bound for '<' values.
        for source, target, factor in (("sodium_g", "salt_g", core.SALT_PER_SODIUM),
                                       ("energy_kj", "energy_kcal", 1 / core.KJ_PER_KCAL)):
            if target not in raw and str(raw.get(source, "")).startswith("<"):
                raw[target] = "<" + str(core.parse_number(raw[source][1:], source) * factor)
        vals, _ = core.parse_nutrients(raw, warnings)
        implausible = None
        if basis:
            try:
                core.check_plausible(basis, vals, warnings)
            except core.InvalidInput as e:
                implausible = str(e)
        meta.update(basis=basis, basis_inferred=None, schema_version="v3.6",
                    nutrients={k: raw[k] if k in raw and str(raw[k]).startswith("<") else
                               None if v is None else core._f(v) for k, v in vals.items()},
                    nutrients_serving=None, warnings=warnings, implausible=implausible)
        return meta

    def normalise(self, code, p):
        warnings = []
        lc = self.language
        name = next((v.strip() for v in (p.get(f"product_name_{lc}"), p.get("product_name"),
                                         p.get(f"generic_name_{lc}"), p.get("generic_name"))
                     if isinstance(v, str) and v.strip()), None)
        brands = p.get("brands") if isinstance(p.get("brands"), str) else ""
        brand = next((b.strip() for b in brands.split(",") if b.strip()), None)
        quantity = p.get("quantity") if isinstance(p.get("quantity"), str) else None

        basis, inferred = None, None
        unit = p.get("product_quantity_unit")
        if unit in ("g", "ml"):
            basis, inferred = f"100{unit}", f"product_quantity_unit '{unit}'"
        else:
            dims = {core.dimension(u.lower()) for _, u in QTY_TOKEN.findall(quantity or "")}
            if len(dims) == 1:
                basis = "100g" if dims == {"mass"} else "100ml"
                inferred = f"package quantity '{quantity}'"
        serving = None
        sq, su = p.get("serving_quantity"), p.get("serving_quantity_unit")
        if su not in ("g", "ml"):
            su = basis[3:] if basis else None
        try:
            if sq not in (None, "", 0, "0") and su:
                serving = {"value": float(core.parse_number(sq, "serving_quantity")), "unit": su}
        except core.InvalidInput:
            serving = None
        if serving is None and isinstance(p.get("serving_size"), str):
            m = QTY_TOKEN.search(p["serving_size"])
            if m and core.dimension(m.group(2).lower()) != "count":
                v, u = core.parse_quantity(f"{m.group(1)} {m.group(2).lower()}", "serving")
                serving = {"value": float(v), "unit": u}

        nutriments = p.get("nutriments") if isinstance(p.get("nutriments"), dict) else {}
        per100 = self._nutrients(nutriments, "_100g", warnings)
        per_serving = self._nutrients(nutriments, "_serving", warnings)
        implausible = None
        if per100 is not None and basis:
            try:
                core.check_plausible(basis, {k: None if v is None else Decimal(repr(v))
                                             for k, v in per100.items()}, warnings)
            except core.InvalidInput as e:
                implausible = str(e)
        return {
            "provider": self.name, "code": code, "name": name and name[:120],
            "brand": brand and brand[:120], "quantity": quantity, "basis": basis,
            "basis_inferred": inferred and f"per 100 g/ml from {inferred}"[:200],
            "serving": serving, "nutrients": per100 or dict.fromkeys(core.NUTRIENTS),
            "nutrients_serving": per_serving, "warnings": warnings,
            "implausible": implausible, "attribution": self.attribution,
            "url": f"https://{self.host}/product/{code}",
        }

    @staticmethod
    def _nutrients(nutriments, suffix, warnings):
        raw = {}
        for off, key in OFF_NUTRIMENTS:
            v = nutriments.get(off + suffix)
            if v is None and off == "energy-kj":
                v = nutriments.get("energy" + suffix)  # legacy: always kJ
            if v is None or v == "":
                continue
            try:
                core.parse_number(v, off)
                raw[key] = v
            except core.InvalidInput:
                warnings.append(f"ignored invalid {off}{suffix}: {v!r}")
        if not raw:
            return None
        try:
            vals, _ = core.parse_nutrients(raw, warnings)
        except core.InvalidInput as e:
            warnings.append(f"ignored {suffix[1:]} values: {e}")
            return None
        return {k: None if v is None else core._f(v) for k, v in vals.items()}


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------


class ProviderCache:
    """Product lookups through a small SQLite cache, kept apart from the
    private ledger. Found products are reused for 30 days, not-found for a
    day; transient errors are never cached. Dry run reads, never writes."""

    def __init__(self, path, provider, dry_run=False, clock=time.time):
        self.path, self.provider, self.dry_run, self.clock = Path(path), provider, dry_run, clock
        self._db = None

    @property
    def db(self):
        if self._db is None:
            if self.dry_run and not self.path.exists():
                self._db = sqlite3.connect(":memory:", isolation_level=None)
            else:
                if not self.path.exists():
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    os.chmod(self.path.parent, 0o700)
                    os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                self._db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            self._db.execute("CREATE TABLE IF NOT EXISTS products(provider TEXT NOT NULL, "
                             "code TEXT NOT NULL, fetched_at REAL NOT NULL, status TEXT NOT NULL, "
                             "payload TEXT, PRIMARY KEY(provider, code))")
            self._db.execute("CREATE TABLE IF NOT EXISTS requests(provider TEXT, ts REAL)")
        return self._db

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None

    def _write(self, sql, args):
        if not self.dry_run:
            self.db.execute(sql, args)

    def search(self, query, limit=5):
        now, bucket = self.clock(), self.provider.name + ":search"
        recent = self.db.execute("SELECT COUNT(*) FROM requests WHERE provider = ? AND ts > ?",
                                 (bucket, now - 60)).fetchone()[0]
        if recent >= MAX_PER_MINUTE:
            raise ProviderError("rate_limited", "OFF text search budget 10/minute; wait")
        self._write("DELETE FROM requests WHERE ts < ?", (now - 3600,))
        self._write("INSERT INTO requests VALUES (?, ?)", (bucket, now))
        return self.provider.search(query, limit=limit)

    def lookup(self, code, refresh=False, offline=False):
        code = core.gtin(code)
        name, now = self.provider.name, self.clock()
        # Versioned products, stable provider name for attribution and budgets.
        if getattr(self.provider, "api_version", "v2") != "v2":
            name += ":" + self.provider.api_version
        row = self.db.execute("SELECT * FROM products WHERE provider = ? AND code = ?",
                              (name, code)).fetchone()
        if row and not refresh:
            fetched, status, payload = row[2], row[3], row[4]
            ttl = FOUND_TTL if status == "found" else NOT_FOUND_TTL
            if now - fetched < ttl:
                if status == "found":
                    return dict(json.loads(payload), cached=True)
                raise ProviderError("not_found", f"{code} is not in {name} (checked "
                                    f"{_iso(fetched)})")
        if offline:
            raise ProviderError("offline", f"{code} is not cached and lookups are offline")
        recent = self.db.execute("SELECT COUNT(*) FROM requests WHERE provider = ? AND ts > ?",
                                 (name, now - 60)).fetchone()[0]
        if recent >= MAX_PER_MINUTE:
            raise ProviderError("rate_limited", f"more than {MAX_PER_MINUTE} lookups in a "
                                "minute - wait a moment")
        self._write("DELETE FROM requests WHERE ts < ?", (now - 3600,))
        self._write("INSERT INTO requests VALUES (?, ?)", (name, now))
        try:
            product = self.provider.lookup_barcode(code)
        except ProviderError as e:
            if e.kind == "not_found":
                self._write("INSERT OR REPLACE INTO products VALUES (?, ?, ?, 'not_found', NULL)",
                            (name, code, now))
            raise
        product["fetched_at"] = _iso(now)
        self._write("INSERT OR REPLACE INTO products VALUES (?, ?, ?, 'found', ?)",
                    (name, code, now, json.dumps(product)))
        return dict(product, cached=False)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# barcode-only items
# --------------------------------------------------------------------------


def validate_snapshot_identity(raw):
    """A provider snapshot belongs to one identity; overrides are independent."""
    if not isinstance(raw, dict) or raw.get("nutrients") is None:
        return
    provenance = raw.get("provenance") or {}
    if not isinstance(provenance, dict) or provenance.get("kind") != "provider":
        return
    key = {"openfoodfacts": "barcode", "bls": "bls_code"}.get(provenance.get("provider"))
    if key and raw.get(key) != provenance.get("product_code"):
        raise core.InvalidInput("provider identity changed: replace the entire item snapshot "
                                "(or remove nutrients and provenance to resolve it again)")


def resolve_bls_item(raw, lookup):
    """Adapt the standalone BLS snapshot without merging other sources."""
    validate_snapshot_identity(raw)
    if not isinstance(raw, dict) or raw.get("bls_code") is None or raw.get("nutrients") is not None:
        return raw
    code = raw["bls_code"]
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][0-9]{6}", code):
        raise core.InvalidInput("bls_code: one uppercase letter and six digits")
    snap = lookup(code)
    guard = snap.get("automatic_use", {})
    if guard.get("status") != "ok":
        raise core.MissingInfo("bls_code", f"BLS {code}: {guard.get('status', 'unknown')} - "
                               + "; ".join(guard.get("reasons", [])) + "; use a verified label instead")
    nutrients, qualifiers = dict.fromkeys(core.NUTRIENTS), []
    for source_key, n in snap["nutrients"].items():
        key = "carbohydrate_g" if source_key == "carbs_g" else source_key
        if key not in nutrients:
            continue
        qualifier = n.get("qualifier")
        if qualifier in ("exact", "derived", "corrected") and not n.get("unreliable"):
            nutrients[key] = n.get("value")
        else:
            qualifiers.append(f"{key}: {qualifier} {n.get('raw') or ''}".strip())
    return {"name": raw.get("name") or snap["name"], "query": raw.get("query"),
            "bls_code": code, "amount": raw.get("amount"), "basis": "100g",
            "nutrients": nutrients, "note": raw.get("note"),
            "provenance": {"kind": "provider", "provider": "bls", "product_code": code,
                           "source_version": snap["source_version"],
                           "attribution": snap["attribution"],
                           "notes": "; ".join(qualifiers)[:300] or "BLS per 100 g edible portion"}}


def resolve_item(raw, lookup):
    """An item with a barcode and no nutrients, filled in from the provider.
    Items that carry their own values (a label read off the pack) are
    returned untouched: the label in hand beats the database. A product the
    provider lacks, or lacks values for, is a question for the athlete."""
    validate_snapshot_identity(raw)
    if not isinstance(raw, dict) or raw.get("barcode") is None or raw.get("nutrients") is not None:
        return raw
    code = core.gtin(raw["barcode"])
    try:
        product = lookup(code)
    except ProviderError as e:
        if e.kind == "not_found":
            raise core.MissingInfo("nutrients", "That barcode isn't in Open Food Facts - "
                                   "a photo of the nutrition label is needed.") from None
        raise
    if product.get("implausible"):
        raise core.MissingInfo("nutrients", "Open Food Facts values for this product look "
                               f"wrong ({product['implausible']}) - a label photo is needed.")
    per100, per_serving = product.get("nutrients") or {}, product.get("nutrients_serving") or {}
    if any(v is not None for v in per100.values()):
        if not product.get("basis"):
            raise core.MissingInfo("basis", "Open Food Facts doesn't say whether its values are "
                                   "per 100 g or per 100 ml - what does the label say?")
        per, nutrients = product["basis"], per100
    elif any(v is not None for v in per_serving.values()):
        per, nutrients = "serving", per_serving
    else:
        raise core.MissingInfo("nutrients", "Open Food Facts has no nutrition values for "
                               "this product - a photo of the label is needed.")
    basis = raw.get("basis") if isinstance(raw.get("basis"), dict) else {}
    serving = basis.get("serving") or product.get("serving")
    provenance = {"kind": "provider", "provider": product["provider"], "product_code": code,
                  "fetched_at": product.get("fetched_at"), "attribution": product["attribution"],
                  "basis_inferred": product.get("basis_inferred")}
    item = {
        "name": raw.get("name") or product.get("name") or f"product {code}",
        "query": raw.get("query"),
        "brand": raw.get("brand") or product.get("brand"),
        "barcode": code,
        "amount": raw.get("amount"),
        "basis": {"per": per, "serving": serving},
        "nutrients": {k: v for k, v in nutrients.items() if v is not None},
        "density_g_per_ml": raw.get("density_g_per_ml"),
        "provenance": {k: v for k, v in provenance.items() if v is not None},
        "note": raw.get("note"),
    }
    return {k: v for k, v in item.items() if v is not None}
