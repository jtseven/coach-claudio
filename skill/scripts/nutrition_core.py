"""Deterministic nutrition maths for the food log.

Standard library only. No I/O: the ledger, the providers and the CLI all
call into this module, and the tests drive it directly.

The model *recognises* food - it reads a label, a barcode, a plate - and
writes down what it saw. Everything numeric after that happens here, in
Decimal: unit conversion, scaling a per-100 g label to the eaten amount,
kJ to kcal, sodium to salt, totals. A value nobody knows stays None; it is
never quietly a zero.

Canonical nutrients, all for the stated basis or the eaten amount:

    energy_kcal      kcal (1 kcal = 4.184 kJ, EU Reg. 1169/2011 Annex XIV)
    protein_g
    carbohydrate_g   *available* carbohydrate, EU style: fibre not included
    sugars_g         a subset of carbohydrate_g
    fat_g
    fiber_g          dietary fibre, declared separately (EU)
    salt_g           salt = sodium x 2.5 (EU Reg. 1169/2011 Annex I)

Accepted only as input, converted explicitly: energy_kj, sodium_g,
sodium_mg, carbohydrate_total_g (US/Canada "Total Carbohydrate", which
includes fibre).
"""

from decimal import Decimal, InvalidOperation
import re

NUTRIENTS = ("energy_kcal", "protein_g", "carbohydrate_g", "sugars_g",
             "fat_g", "fiber_g", "salt_g")
INPUT_ONLY = ("energy_kj", "sodium_g", "sodium_mg", "carbohydrate_total_g")
MASS_KEYS = ("protein_g", "carbohydrate_g", "fat_g", "fiber_g", "salt_g")

KJ_PER_KCAL = Decimal("4.184")
SALT_PER_SODIUM = Decimal("2.5")
# Atwater general factors; EU Annex XIV gives fibre 2 kcal/g.
KCAL_PER_G = {"protein_g": 4, "carbohydrate_g": 4, "fat_g": 9, "fiber_g": 2}

MASS_G = {"mg": Decimal("0.001"), "g": Decimal(1), "kg": Decimal(1000),
          "oz": Decimal("28.349523125"), "lb": Decimal("453.59237")}
VOLUME_ML = {"ml": Decimal(1), "cl": Decimal(10), "dl": Decimal(100),
             "l": Decimal(1000)}
COUNT = ("serving",)
UNIT_ALIASES = {"gram": "g", "grams": "g", "gr": "g", "kilogram": "kg",
                "kilograms": "kg", "milliliter": "ml", "millilitre": "ml",
                "liter": "l", "litre": "l", "servings": "serving",
                "portion": "serving", "portions": "serving"}
# A single item beyond these is a typo, not a meal.
MAX_G = Decimal(20000)
MAX_ML = Decimal(20000)
MAX_SERVINGS = Decimal(100)
MAX_NUMBER = Decimal(1000000)

BASES = ("100g", "100ml", "serving", "total")
PROVENANCE_KINDS = ("label", "provider", "estimate", "user")
PROVENANCE_KEYS = {"kind", "confidence", "uncertainty", "notes", "provider",
                   "product_code", "fetched_at", "attribution", "image_sha256",
                   "basis_inferred", "source_version"}
ITEM_KEYS = {"name", "brand", "barcode", "amount", "basis", "nutrients",
             "density_g_per_ml", "provenance", "note", "bls_code", "query"}

NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
QUANTITY_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([A-Za-z]+)\s*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidInput(ValueError):
    """The data is wrong: refuse it, nothing is saved."""


class LookupFailed(RuntimeError):
    """A provider could not answer (outage, rate limit). Never a reason to
    fall back to made-up values; food_providers.ProviderError is one."""

    kind = "unavailable"


class MissingInfo(ValueError):
    """The data is incomplete: a question to the athlete can fix it."""

    def __init__(self, field, question):
        super().__init__(f"missing {field}: {question}")
        self.field = field
        self.question = question


# --------------------------------------------------------------------------
# numbers and quantities
# --------------------------------------------------------------------------


def parse_number(v, what="value"):
    """A finite, non-negative Decimal. Accepts int, float, Decimal or a string
    with a dot or comma decimal ("4,5"). Rejects bools, NaN, infinities,
    exponents, signs and anything else."""
    if isinstance(v, bool) or v is None:
        raise InvalidInput(f"{what}: expected a number, got {v!r}")
    if isinstance(v, int):
        d = Decimal(v)
    elif isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise InvalidInput(f"{what}: {v!r} is not a finite number")
        d = Decimal(repr(v))
    elif isinstance(v, Decimal):
        if not v.is_finite():
            raise InvalidInput(f"{what}: {v!r} is not a finite number")
        d = v
    elif isinstance(v, str):
        s = v.strip()
        if not NUMBER_RE.match(s):
            raise InvalidInput(f"{what}: {v!r} is not a plain decimal number")
        try:
            d = Decimal(s.replace(",", "."))
        except InvalidOperation:
            raise InvalidInput(f"{what}: {v!r} is not a number") from None
    else:
        raise InvalidInput(f"{what}: expected a number, got {type(v).__name__}")
    if d < 0:
        raise InvalidInput(f"{what}: {v!r} is negative")
    if d > MAX_NUMBER:
        raise InvalidInput(f"{what}: {v!r} is implausibly large")
    return d


def parse_nutrient_value(v, what="nutrient"):
    """(Decimal or None, trace). For compatibility 0 is the calculation's
    lower bound, NOT measured zero. compute_item preserves the upper bound
    and receipts render the interval; consumers must honor nutrient_bounds."""
    if v is None:
        return None, False
    if isinstance(v, str) and v.strip().startswith("<"):
        parse_number(v.strip()[1:], what)
        return Decimal(0), True
    return parse_number(v, what), False


def canonical_unit(unit):
    if not isinstance(unit, str):
        raise InvalidInput(f"unit: expected a string, got {unit!r}")
    u = unit.strip()
    u = UNIT_ALIASES.get(u.lower(), u.lower())
    if u not in MASS_G and u not in VOLUME_ML and u not in COUNT:
        raise InvalidInput(f"unit {unit!r}: use g, kg, mg, oz, lb, ml, cl, dl, l or serving")
    return u


def parse_quantity(q, what="amount"):
    """(Decimal > 0, canonical unit) from "250 g", "0,5 l" or
    {"value": 250, "unit": "g"}."""
    if isinstance(q, str):
        m = QUANTITY_RE.match(q)
        if not m:
            raise InvalidInput(f"{what}: {q!r} is not '<number> <unit>'")
        value, unit = m.group(1), m.group(2)
    elif isinstance(q, dict):
        if set(q) - {"value", "unit"} or "value" not in q or "unit" not in q:
            raise InvalidInput(f"{what}: needs exactly 'value' and 'unit'")
        value, unit = q["value"], q["unit"]
    else:
        raise InvalidInput(f"{what}: expected '<number> <unit>' or an object, got {q!r}")
    d = parse_number(value, what)
    u = canonical_unit(unit)
    if d == 0:
        raise InvalidInput(f"{what}: must be greater than zero")
    if ((u in MASS_G and d * MASS_G[u] > MAX_G)
            or (u in VOLUME_ML and d * VOLUME_ML[u] > MAX_ML)
            or (u in COUNT and d > MAX_SERVINGS)):
        raise InvalidInput(f"{what}: {d} {u} is implausibly large for one item")
    return d, u


def dimension(unit):
    return "mass" if unit in MASS_G else "volume" if unit in VOLUME_ML else "count"


# --------------------------------------------------------------------------
# GTIN / EAN barcodes
# --------------------------------------------------------------------------


def gtin(code):
    """The validated GTIN-8/12/13/14 as a digit string, leading zeros kept.
    Spaces and hyphens from a hand-typed or visually read code are removed.
    Integers are refused: they would already have lost leading zeros."""
    if not isinstance(code, str):
        raise InvalidInput("barcode: pass the digits as a string")
    digits = re.sub(r"[\s-]", "", code)
    if not digits.isascii() or not digits.isdigit():
        raise InvalidInput(f"barcode {code!r}: digits only")
    if len(digits) not in (8, 12, 13, 14):
        raise InvalidInput(f"barcode {code!r}: a GTIN has 8, 12, 13 or 14 digits")
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    if (10 - total % 10) % 10 != check:
        raise InvalidInput(f"barcode {code!r}: check digit does not match - misread?")
    return digits


def restricted_circulation(code):
    """EAN-13 prefixes 02 and 20-29 are in-store codes (weighed goods, price
    embedded): they identify nothing in a global product database."""
    return (len(code) == 13 and (code.startswith("02") or code[:2] in
            {str(n) for n in range(20, 30)})) or (len(code) == 12 and code[0] in "24")


# --------------------------------------------------------------------------
# one item
# --------------------------------------------------------------------------


def _text(v, what, limit, required=False):
    if v is None:
        if required:
            raise InvalidInput(f"{what} is required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise InvalidInput(f"{what}: expected non-empty text")
    v = " ".join(v.split())
    if len(v) > limit:
        raise InvalidInput(f"{what}: longer than {limit} characters")
    return v


def parse_provenance(p):
    if not isinstance(p, dict):
        raise InvalidInput("provenance: an object with at least 'kind' is required")
    unknown = set(p) - PROVENANCE_KEYS
    if unknown:
        raise InvalidInput(f"provenance: unknown keys {sorted(unknown)}")
    kind = p.get("kind")
    if kind not in PROVENANCE_KINDS:
        raise InvalidInput(f"provenance.kind must be one of {', '.join(PROVENANCE_KINDS)}")
    out = {"kind": kind}
    if p.get("confidence") is not None:
        c = parse_number(p["confidence"], "provenance.confidence")
        if c > 1:
            raise InvalidInput("provenance.confidence is between 0 and 1")
        out["confidence"] = float(c)
    elif kind == "estimate":
        raise InvalidInput("provenance.confidence (0-1) is required for an estimate")
    for key, limit in (("uncertainty", 200), ("notes", 300), ("provider", 40),
                       ("fetched_at", 40), ("attribution", 300), ("basis_inferred", 200),
                       ("source_version", 40)):
        if p.get(key) is not None:
            out[key] = _text(p[key], f"provenance.{key}", limit)
    if p.get("product_code") is not None:
        if p.get("provider") == "bls":
            code = p["product_code"]
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z][0-9]{6}", code):
                raise InvalidInput("BLS product_code: one uppercase letter and six digits")
            out["product_code"] = code
        else:
            out["product_code"] = gtin(p["product_code"])
    if p.get("image_sha256") is not None:
        hashes = p["image_sha256"]
        hashes = [hashes] if isinstance(hashes, str) else hashes
        if not isinstance(hashes, list) or not all(
                isinstance(h, str) and SHA256_RE.match(h) for h in hashes):
            raise InvalidInput("provenance.image_sha256: lowercase hex SHA-256 digest(s)")
        out["image_sha256"] = sorted(set(hashes))
    return out


def parse_basis(b):
    if isinstance(b, str):
        b = {"per": b}
    if not isinstance(b, dict) or set(b) - {"per", "serving"}:
        raise InvalidInput("basis: {'per': '100g'|'100ml'|'serving'|'total', 'serving': '30 g'}")
    per = b.get("per")
    if per not in BASES:
        raise InvalidInput(f"basis.per must be one of {', '.join(BASES)}")
    serving = None
    if b.get("serving") is not None:
        serving = parse_quantity(b["serving"], "basis.serving")
        if dimension(serving[1]) == "count":
            raise InvalidInput("basis.serving is a size in g or ml, not a count")
    return per, serving


def parse_nutrients(n, warnings):
    """Canonical basis nutrients (Decimal or None) and the trace keys."""
    if n is None:
        n = {}
    if not isinstance(n, dict):
        raise InvalidInput("nutrients: expected an object")
    unknown = set(n) - set(NUTRIENTS) - set(INPUT_ONLY)
    if unknown:
        raise InvalidInput(f"nutrients: unknown keys {sorted(unknown)} "
                           f"(use {', '.join(NUTRIENTS + INPUT_ONLY)})")
    vals, trace = {}, []
    for key in NUTRIENTS + INPUT_ONLY:
        vals[key], is_trace = parse_nutrient_value(n.get(key), f"nutrients.{key}")
        if is_trace:
            trace.append(key)
    out = {k: vals[k] for k in NUTRIENTS}

    kj = vals["energy_kj"]
    if kj is not None:
        from_kj = kj / KJ_PER_KCAL
        if out["energy_kcal"] is None:
            out["energy_kcal"] = from_kj
        elif abs(out["energy_kcal"] - from_kj) > max(Decimal(2), out["energy_kcal"] * Decimal("0.03")):
            warnings.append(f"energy: {out['energy_kcal']} kcal and {kj} kJ disagree "
                            f"({_r(from_kj, 0)} kcal); kept the kcal value")

    sodium = vals["sodium_g"]
    if vals["sodium_mg"] is not None:
        mg_as_g = vals["sodium_mg"] / 1000
        if sodium is not None and abs(sodium - mg_as_g) > Decimal("0.001"):
            raise InvalidInput("nutrients: sodium_g and sodium_mg disagree")
        sodium = mg_as_g
    if sodium is not None:
        from_sodium = sodium * SALT_PER_SODIUM
        if out["salt_g"] is None:
            out["salt_g"] = from_sodium
            warnings.append("salt derived from sodium x 2.5")
        elif abs(out["salt_g"] - from_sodium) > max(Decimal("0.05"), out["salt_g"] * Decimal("0.1")):
            warnings.append(f"salt {out['salt_g']} g vs sodium {sodium} g x 2.5 = "
                            f"{_r(from_sodium, 2)} g; kept the salt value")

    total = vals["carbohydrate_total_g"]
    if total is not None:
        fiber = out["fiber_g"]
        if out["carbohydrate_g"] is not None:
            if fiber is not None and abs(total - fiber - out["carbohydrate_g"]) > 1:
                warnings.append("carbohydrate_total_g minus fibre does not match "
                                "carbohydrate_g; kept carbohydrate_g")
        elif "fiber_g" in trace or "carbohydrate_total_g" in trace:
            warnings.append("qualified total carbohydrate/fibre: available carbohydrate remains unknown")
        elif fiber is not None:
            if fiber > total:
                raise InvalidInput("nutrients: fibre exceeds total carbohydrate")
            out["carbohydrate_g"] = total - fiber
        else:
            warnings.append("total carbohydrate includes fibre; available carbohydrate "
                            "is unknown without the fibre value")

    # A qualified alternative does not make an independently stated exact
    # canonical value a trace. Only mark conversions actually used.
    trace = [t for t in trace if not (
        (t == "energy_kj" and vals["energy_kcal"] is not None) or
        (t.startswith("sodium") and vals["salt_g"] is not None) or
        (t == "carbohydrate_total_g" and out["carbohydrate_g"] is None))]
    trace_out = sorted({"energy_kcal" if t == "energy_kj" else
                        "salt_g" if t.startswith("sodium") else
                        "carbohydrate_g" if t == "carbohydrate_total_g" else t
                        for t in trace})
    return out, trace_out


def check_plausible(per, basis_vals, warnings):
    carbs, sugars = basis_vals["carbohydrate_g"], basis_vals["sugars_g"]
    if carbs is not None and sugars is not None and sugars > carbs + Decimal("0.5"):
        raise InvalidInput(f"sugars ({sugars} g) cannot exceed carbohydrate ({carbs} g): "
                           "sugars are part of it - misread?")
    if per == "100g":
        mass = sum(v for k, v in basis_vals.items() if k in MASS_KEYS and v is not None)
        if mass > 101:
            raise InvalidInput(f"per 100 g, protein+carbohydrate+fat+fibre+salt is {mass} g: "
                               "more than 100 g - misread?")
    energy = basis_vals["energy_kcal"]
    if per in ("100g", "100ml") and energy is not None and energy > 950:
        raise InvalidInput(f"{energy} kcal per {per} is above pure fat (~900): "
                           "was a kJ value read as kcal?")
    if energy is not None and all(basis_vals[k] is not None for k in
                                  ("protein_g", "carbohydrate_g", "fat_g")):
        expected = sum(basis_vals[k] * f for k, f in KCAL_PER_G.items()
                       if basis_vals[k] is not None)
        if abs(energy - expected) > max(Decimal(15), Decimal("0.15") * max(energy, expected)):
            warnings.append(f"energy {_r(energy, 0)} kcal vs {_r(expected, 0)} kcal by "
                            "4/4/9 (+2 fibre): label kept - alcohol, polyols or a "
                            "misread can explain the gap")


def _to(amount, unit, target, serving, density):
    """Convert (amount, unit) to grams ('mass') or millilitres ('volume'),
    or None when it would need a density or serving size nobody gave."""
    dim = dimension(unit)
    if dim == "count":
        if serving is None:
            return None
        size = _to(serving[0], serving[1], target, None, density)
        return None if size is None else amount * size
    base = amount * (MASS_G[unit] if dim == "mass" else VOLUME_ML[unit])
    if dim == target:
        return base
    if density is None:
        return None
    return base * density if target == "mass" else base / density


def compute_item(raw):
    """Validate one item and scale it to what was eaten. Raises InvalidInput
    for wrong data and MissingInfo for a gap a question can fill."""
    if not isinstance(raw, dict):
        raise InvalidInput("item: expected an object")
    unknown = set(raw) - ITEM_KEYS
    if unknown:
        raise InvalidInput(f"item: unknown keys {sorted(unknown)}")
    warnings = []
    query = _text(raw.get("query"), "item.query", 200)
    name = _text(raw.get("name") or query, "item.name", 120, required=True)
    if raw.get("nutrients") is None:
        raise MissingInfo("product", f"Choose the matching product for {query or name}; "
                          "if unavailable, send its barcode or nutrition label.")
    brand = _text(raw.get("brand"), "item.brand", 120)
    note = _text(raw.get("note"), "item.note", 300)
    barcode = gtin(raw["barcode"]) if raw.get("barcode") is not None else None
    provenance = parse_provenance(raw.get("provenance"))
    per, serving = parse_basis(raw.get("basis"))
    basis_vals, trace = parse_nutrients(raw.get("nutrients"), warnings)
    if all(v is None for v in basis_vals.values()):
        raise MissingInfo("nutrients", "No nutrition values yet - a label photo, the "
                          "barcode, or an estimate is needed.")
    check_plausible(per, basis_vals, warnings)

    density = None
    if raw.get("density_g_per_ml") is not None:
        density = parse_number(raw["density_g_per_ml"], "density_g_per_ml")
        if not Decimal("0.2") <= density <= Decimal(3):
            raise InvalidInput("density_g_per_ml must be between 0.2 and 3")

    amount = None
    if raw.get("amount") is not None:
        amount = parse_quantity(raw["amount"])
    elif per != "total":
        raise MissingInfo("amount", "How much was it - grams, millilitres or servings?")

    grams = ml = None
    if amount:
        grams = _to(amount[0], amount[1], "mass", serving, density)
        ml = _to(amount[0], amount[1], "volume", serving, density)

    if per == "total":
        factor = Decimal(1)
    elif per in ("100g", "100ml"):
        have = grams if per == "100g" else ml
        if have is None:
            if amount[1] == "serving":
                raise MissingInfo("basis.serving", "How big is one serving (g or ml)?")
            want = "grams" if per == "100g" else "millilitres"
            raise MissingInfo("density_g_per_ml", f"The label is per 100 {per[3:]} but the "
                              f"amount is in {amount[1]}: how many {want} was it?")
        factor = have / 100
    else:  # per serving
        if amount[1] == "serving":
            factor = amount[0]
        elif serving is None:
            raise MissingInfo("basis.serving", "How big is one serving (g or ml)?")
        else:
            target = dimension(serving[1])
            have = grams if target == "mass" else ml
            size = _to(serving[0], serving[1], target, None, density)
            if have is None:
                raise MissingInfo("density_g_per_ml", "The serving and the amount use "
                                  "different units: how many grams or millilitres was it?")
            factor = have / size

    if grams is not None and grams > 2000:
        warnings.append(f"{_r(grams, 0)} g is a lot for one item - check the amount")

    bounds = {}
    if trace:
        upper_input = {k: v.strip()[1:] if isinstance(v, str) and v.strip().startswith("<")
                       else v for k, v in raw["nutrients"].items()}
        upper, _ = parse_nutrients(upper_input, [])
        for key in trace:
            if upper[key] is not None:
                bounds[key] = {"lower": float((basis_vals[key] or Decimal(0)) * factor),
                               "upper": float(upper[key] * factor), "upper_exclusive": True}
    return {
        "name": name,
        "brand": brand,
        "barcode": barcode,
        "note": note,
        "amount": None if amount is None else {"value": _f(amount[0]), "unit": amount[1]},
        "amount_g": None if grams is None else _f(grams),
        "amount_ml": None if ml is None else _f(ml),
        "basis": {"per": per, "serving": None if serving is None else
                  {"value": _f(serving[0]), "unit": serving[1]}},
        "density_g_per_ml": None if density is None else _f(density),
        "basis_nutrients": {k: None if v is None else _f(v) for k, v in basis_vals.items()},
        "nutrients": {k: None if v is None else _f(v * factor) for k, v in basis_vals.items()},
        "trace": trace,
        "nutrient_bounds": bounds,
        "warnings": warnings,
        "provenance": provenance,
        "estimate": provenance["kind"] == "estimate",
    }


# --------------------------------------------------------------------------
# totals and receipts
# --------------------------------------------------------------------------


def totals(items):
    """Per nutrient: the sum of known values, whether every item knew it, and
    how many did not. With nothing known the value is None, never 0."""
    out = {}
    for key in NUTRIENTS:
        known = [Decimal(repr(i["nutrients"][key])) for i in items
                 if i["nutrients"][key] is not None]
        unknown = len(items) - len(known)
        out[key] = {"value": _f(sum(known)) if known else None,
                    "complete": bool(items) and unknown == 0,
                    "unknown_items": unknown}
        bounded = [i.get("nutrient_bounds", {}).get(key) for i in items]
        if any(bounded):
            lower = sum(Decimal(str(b["lower"])) if b else Decimal(str(i["nutrients"][key] or 0))
                        for i, b in zip(items, bounded))
            upper = None if unknown else sum(Decimal(str(b["upper"])) if b else
                                              Decimal(str(i["nutrients"][key]))
                                              for i, b in zip(items, bounded))
            out[key]["bounds"] = {"lower": float(lower), "upper": None if upper is None else float(upper),
                                  "upper_exclusive": upper is not None}
    return out


LABELS = (("energy_kcal", "kcal", 0), ("protein_g", "P", 1), ("carbohydrate_g", "C", 1),
          ("fat_g", "F", 1), ("fiber_g", "fibre", 1), ("sugars_g", "sugars", 1),
          ("salt_g", "salt", 2))


def receipt(tot, estimate=False):
    """`~650 kcal · P 25 g · C 80 g · F 20 g · fibre ≥4 g · sugars ? · salt 2.1 g`.
    `~` marks estimates, `≥` a sum missing some items, `?` nothing known."""
    approx = "~" if estimate else ""
    parts = []
    for key, label, digits in LABELS:
        t = tot[key]
        bounds = t.get("bounds")
        if bounds:
            # Do not round a bound down: 0.004 g is not '<0 g'.
            def show(value):
                text = format(Decimal(str(value)), "f")
                return text.rstrip("0").rstrip(".") if "." in text else text
            lo, hi = bounds["lower"], bounds["upper"]
            if hi is None:
                interval = "≥" + show(lo) + " (trace; incomplete)"
            else:
                interval = "<" + show(hi) if lo == 0 else show(lo) + "–<" + show(hi)
            parts.append(f"{interval} kcal" if key == "energy_kcal" else f"{label} {interval} g")
            continue
        if t["value"] is None:
            parts.append(f"{label} ?")
            continue
        num = _r(Decimal(repr(t["value"])), digits)
        mark = approx if t["complete"] else "≥"
        parts.append(f"{mark}{num} kcal" if key == "energy_kcal" else f"{label} {mark}{num} g")
    return " · ".join(parts)


def _f(d):
    """Decimal to a JSON float, two decimals, no -0.0."""
    v = float(d.quantize(Decimal("0.01")))
    return v + 0.0


def _r(d, digits):
    q = Decimal(1).scaleb(-digits)
    s = str(d.quantize(q))
    return s.rstrip("0").rstrip(".") if "." in s else s
