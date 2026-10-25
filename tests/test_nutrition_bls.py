"""Offline tests for the generic BLS provider: synthetic XLSX/ZIP fixtures built inline,
disposable databases, no network, no athlete data.

    python3 -m unittest discover -s tests -p test_nutrition_bls.py -v
"""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import nutrition_bls as bls  # noqa: E402

# (code, German name, unit) in the order of the official workbook.
NUTRIENT_COLUMNS = [
    ("ENERCJ", "Energie (Kilojoule)", "kJ"),
    ("ENERCC", "Energie (Kilokalorien)", "kcal"),
    ("WATER", "Wasser", "g"),
    ("PROT625", "Protein (Nx6,25)", "g"),
    ("FAT", "Fett", "g"),
    ("CHO", "Kohlenhydrate, verfügbar", "g"),
    ("FIBT", "Ballaststoffe, gesamt", "g"),
    ("ALC", "Alkohol (Ethanol)", "g"),
    ("OA", "Organische Säuren, gesamt", "g"),
    ("NACL", "Salz (Natriumchlorid)", "g"),
    ("NA", "Natrium", "mg"),
    ("POLYL", "Zuckeralkohole, gesamt", "g"),
    ("SUGAR", "Zucker (Mono- und Disaccharide), gesamt", "g"),
    ("OLSAC", "Oligosaccharide, verfügbar", "g"),
]


def header_row(columns=NUTRIENT_COLUMNS, note=True):
    row = ["BLS Code", "Lebensmittelbezeichnung", "Food name"]
    for code, name, unit in columns:
        row += [f"{code} {name} [{unit}/100g]", f"{code} Datenherkunft", f"{code} Referenz"]
    if note:
        row.append("Hinweis")
    return [("s", text) for text in row]


def food(code, name, english="", columns=NUTRIENT_COLUMNS, note=None, **values):
    """One data row. A value is a number (numeric cell), a str (text cell such as 'TR'),
    or a missing key (no cell at all). `<CODE>_origin` / `<CODE>_ref` set the provenance."""
    row = [("s", code), ("s", name), ("s", english)]
    for nutrient, _, _ in columns:
        value = values.get(nutrient)
        if value is None:
            row += [None, None, None]
        elif isinstance(value, str):
            row += [("s", value)]
            row += [("s", values.get(f"{nutrient}_origin", "Analyse")),
                    ("s", values.get(f"{nutrient}_ref", "-"))]
        else:
            row += [("n", repr(value))]
            row += [("s", values.get(f"{nutrient}_origin", "Analyse")),
                    ("s", values.get(f"{nutrient}_ref", "-"))]
    row.append(("s", note) if note else None)
    return row


def col_ref(index):
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def make_xlsx(rows, sheet_name="BLS_4_0_Daten_2025_DE", inline=False, extra_members=None,
              sheet_xml=None, shared_xml=None, workbook_xml=None):
    """A minimal but structurally real workbook: rows is a list of cell lists, a cell being
    None, ('n', text) or ('s', text); None cells are simply not written."""
    shared, shared_index, sheet_rows = [], {}, []
    for r, cells in enumerate(rows, start=1):
        parts = []
        for c, cell in enumerate(cells):
            if cell is None:
                continue
            kind, text = cell
            ref = f"{col_ref(c)}{r}"
            if kind == "n":
                parts.append(f'<c r="{ref}"><v>{text}</v></c>')
            elif kind == "f":  # (formula, cached value or None): the formula must never be run
                formula, cached = text
                v = f"<v>{cached}</v>" if cached is not None else ""
                parts.append(f'<c r="{ref}"><f>{escape(formula)}</f>{v}</c>')
            elif inline:
                parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(text)}</t></is></c>')
            else:
                if text not in shared_index:
                    shared_index[text] = len(shared)
                    shared.append(text)
                parts.append(f'<c r="{ref}" t="s"><v>{shared_index[text]}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(parts)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = sheet_xml or (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                          f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    sst = shared_xml or (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         f'<sst {ns} count="{len(shared)}" uniqueCount="{len(shared)}">'
                         + "".join(f"<si><t>{escape(t)}</t></si>" for t in shared) + "</sst>")
    workbook = workbook_xml or (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook {ns} '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        if not inline:
            z.writestr("xl/sharedStrings.xml", sst)
        for name, data in (extra_members or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def oats(**over):
    values = dict(ENERCJ=1465, ENERCC=348, WATER=10.07, PROT625=13.22, FAT=6.65, CHO=53.3,
                  FIBT=10.983, ALC=0, OA=0, NACL=0.00495, NA=1.98, POLYL=0, SUGAR=0.74,
                  ENERCC_origin="Formelberechnung", FAT_ref="Synthetic reference; 2008")
    values.update(over)
    return food("C133000", "Hafer Flocken", "Oat flakes", **values)


def synthetic_rows(extra=()):
    return [header_row(), oats(), *extra]


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "providers" / "bls.sqlite3"

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def workbook(self, rows=None, name="bls.xlsx", **kw):
        return self.write(name, make_xlsx(rows if rows is not None else synthetic_rows(), **kw))

    def load(self, rows=None, **kw):
        bls.import_bls(self.workbook(rows, **kw), self.db)

    def snapshot(self, code="C133000"):
        return bls.get(code, self.db)


class ImportAndMappingTests(TempCase):
    def test_import_reports_count_version_hash_and_attribution(self):
        path = self.workbook(synthetic_rows([food("C352032", "Reis poliert, gekocht", "Rice, cooked",
                                                  ENERCC=117, PROT625=3.3)]))
        summary = bls.import_bls(path, self.db)
        self.assertEqual(summary["foods"], 2)
        self.assertEqual(summary["bls_version"], "4.0")
        self.assertEqual(summary["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertIn("Max Rubner-Institut", summary["attribution"])
        self.assertIn("CC BY 4.0", summary["attribution"])

    def test_get_maps_each_nutrient_by_header_code_not_position(self):
        self.load()
        snap = self.snapshot()
        self.assertEqual(snap["provider"], "bls")
        self.assertEqual(snap["source_id"], "C133000")
        self.assertEqual(snap["name"], "Hafer Flocken")
        self.assertEqual(snap["name_en"], "Oat flakes")
        self.assertEqual(snap["basis"], {"quantity": 100, "unit": "g", "per": "100g"})
        n = snap["nutrients"]
        self.assertEqual({k: n[k]["value"] for k in (
            "energy_kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugars_g", "salt_g", "sodium_mg")},
            {"energy_kcal": 348, "protein_g": 13.22, "carbs_g": 53.3, "fat_g": 6.65,
             "fiber_g": 10.983, "sugars_g": 0.74, "salt_g": 0.00495, "sodium_mg": 1.98})
        self.assertEqual(n["sodium_mg"]["unit"], "mg")
        self.assertEqual(n["energy_kcal"]["unit"], "kcal")
        self.assertEqual(n["protein_g"]["source_code"], "PROT625")

    def test_column_order_and_extra_columns_do_not_matter(self):
        shuffled = list(reversed(NUTRIENT_COLUMNS)) + [("VITC", "Vitamin C", "mg")]
        rows = [header_row(shuffled), food("C133000", "Hafer Flocken", "Oat flakes", columns=shuffled,
                                           ENERCC=348, PROT625=13.22, FAT=6.65, CHO=53.3, FIBT=10.983,
                                           NACL=0.01, NA=4, SUGAR=0.74, VITC=9)]
        self.load(rows)
        n = self.snapshot()["nutrients"]
        self.assertEqual(n["protein_g"]["value"], 13.22)
        self.assertEqual(n["fat_g"]["value"], 6.65)
        self.assertEqual(n["sodium_mg"]["value"], 4)

    def test_provenance_keeps_origin_and_reference_and_hinweis(self):
        rows = synthetic_rows()
        rows[1] = oats()
        rows[1][-1] = ("s", "Enthält Maltitol (1 g/100g)")
        self.load(rows)
        snap = self.snapshot()
        fat = snap["nutrients"]["fat_g"]
        self.assertEqual(fat["provenance"], {"origin": "Analyse", "reference": "Synthetic reference; 2008"})
        self.assertEqual(snap["nutrients"]["energy_kcal"]["provenance"]["origin"], "Formelberechnung")
        self.assertIsNone(snap["nutrients"]["protein_g"]["provenance"]["reference"])  # '-' means none
        self.assertEqual(snap["source_note"], "Enthält Maltitol (1 g/100g)")
        self.assertEqual(snap["source_version"], "4.0")
        self.assertEqual(snap["license_id"], "CC-BY-4.0")
        self.assertIn("Max Rubner-Institut", snap["attribution"])

    def test_zip_containing_main_workbook_is_imported_ignoring_other_members(self):
        inner = make_xlsx(synthetic_rows())
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("BLS_4_0_2025_DE/BLS_4_0_Daten_2025_DE.xlsx", inner)
            z.writestr("BLS_4_0_2025_DE/BLS_4_0_Components_DE_EN.xlsx", b"not the data")
            z.writestr("BLS_4_0_2025_DE/BLS_4_0_Dokumentation_DE.pdf", b"%PDF")
        path = self.write("bls.zip", outer.getvalue())
        summary = bls.import_bls(path, self.db)
        self.assertEqual(summary["foods"], 1)
        self.assertEqual(summary["source_member"], "BLS_4_0_2025_DE/BLS_4_0_Daten_2025_DE.xlsx")
        self.assertEqual(summary["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_inline_strings_are_read_too(self):
        self.load(inline=True)
        self.assertEqual(self.snapshot()["name"], "Hafer Flocken")

    def test_get_unknown_code_is_not_found(self):
        self.load()
        with self.assertRaises(bls.NotFound):
            bls.get("ZZZZZZZ", self.db)

    def test_missing_database_is_a_clear_error(self):
        with self.assertRaises(bls.NoDatabase):
            bls.get("C133000", self.dir / "nope.sqlite3")


def column(code):
    """Index of a nutrient's value cell in the default layout."""
    return 3 + 3 * [c for c, _, _ in NUTRIENT_COLUMNS].index(code)


class QualifiedValueTests(TempCase):
    def nutrient(self, key="fat_g", **values):
        self.load([header_row(), oats(**values)])
        return self.snapshot()["nutrients"][key]

    def test_markers_are_qualifiers_with_null_value_never_zero(self):
        cases = {"TR": "trace", "<LOD": "lt_LOD", "<LOQ": "lt_LOQ", "<LOD or <LOQ": "lt_LOD_or_LOQ",
                 "-": "missing"}
        for raw, qualifier in cases.items():
            with self.subTest(raw=raw):
                got = self.nutrient(FAT=raw)
                self.assertIsNone(got["value"])
                self.assertEqual(got["qualifier"], qualifier)
                self.assertEqual(got["raw"], raw)

    def test_numeric_limit_keeps_the_limit_but_no_value(self):
        got = self.nutrient("fiber_g", FIBT="< 0,5")
        self.assertIsNone(got["value"])
        self.assertEqual((got["qualifier"], got["limit"]), ("lt_limit", 0.5))

    def test_absent_cell_is_missing_and_a_real_zero_stays_zero(self):
        rows = [header_row(), oats(FAT=None, FIBT=0)]
        self.load(rows)
        n = self.snapshot()["nutrients"]
        self.assertEqual((n["fat_g"]["value"], n["fat_g"]["qualifier"]), (None, "missing"))
        self.assertEqual((n["fiber_g"]["value"], n["fiber_g"]["qualifier"]), (0, "exact"))

    def test_decimal_comma_text_numbers_are_accepted(self):
        self.assertEqual(self.nutrient("sugars_g", SUGAR="1,5")["value"], 1.5)

    def test_non_finite_negative_and_garbage_numbers_reject_the_whole_import(self):
        for i, bad in enumerate((float("nan"), float("inf"), -1, 1e999, "banana", "NaN", "inf", "1,2,3")):
            with self.subTest(bad=bad):
                rows = [header_row(), oats(), food("C999999", "Bad", "Bad", ENERCC=1, PROT625=bad)]
                fresh = self.dir / f"fresh{i}.sqlite3"
                with self.assertRaises(bls.ImportRejected):
                    bls.import_bls(self.workbook(rows), fresh)
                self.assertFalse(fresh.exists())

    def test_formulas_are_never_evaluated_only_cached_values_are_read(self):
        rows = [header_row(), oats()]
        rows[1][column("FAT")] = ("f", ("__import__('os').system('touch pwned')", "6.65"))
        rows[1][column("PROT625")] = ("f", ("SUM(", None))
        marker = self.dir / "pwned"
        os.chdir(self.dir)
        self.addCleanup(os.chdir, ROOT)
        self.load(rows)
        n = self.snapshot()["nutrients"]
        self.assertEqual(n["fat_g"]["value"], 6.65)
        self.assertEqual(n["protein_g"]["qualifier"], "missing")
        self.assertFalse(marker.exists())


# Hand-computed: 4*10 + 9*5 + 4*(50-2) + 2*3 + 7*0 + 3*1 + 2.4*2 = 290.8 kcal. The old (buggy)
# workbook energy adds OLSAC a second time: 8 kcal for 4 g -> 298.8, published as 299.
AFFECTED = dict(PROT625=10, FAT=5, CHO=50, POLYL=2, FIBT=3, ALC=0, OA=1, OLSAC=4, ENERCC=299)
FORMULA = "4*PROT625 + 9*FAT + 4*(CHO-POLYL) + 2*FIBT + 7*ALC + 3*OA + 2.4*POLYL"


class ErratumTests(TempCase):
    def energy(self, code="C000001", sheet="BLS_4_0_Daten_2025_DE", **values):
        row = food(code, "Synthetic", "Synthetic", **values)
        self.load([header_row(), row], sheet_name=sheet)
        return self.snapshot(code)

    def test_inconsistent_polyols_never_create_or_autoallow_invalid_energy(self):
        for cho, poly in ((0, 100), (1, 2)):
            with self.subTest(cho=cho):
                snap = self.energy(PROT625=0, FAT=0, CHO=cho, POLYL=poly, FIBT=0,
                                   ALC=0, OA=0, OLSAC=1, ENERCC=10)
                energy = snap["nutrients"]["energy_kcal"]
                self.assertEqual(snap["automatic_use"]["status"], "review")
                self.assertEqual((energy["value"], energy["qualifier"]), (10, "exact"))
                self.assertFalse(energy["errata"]["corrected"])
                self.assertIn("CHO", " ".join(snap["automatic_use"]["reasons"]))

    def test_formula_handles_invalid_inputs_and_decimal_overflow(self):
        from decimal import Decimal
        base = {key: 0 for key in bls.ERRATUM_INPUTS}
        for value in (-1, float("nan"), float("inf"), "garbage", Decimal("1e9999999")):
            with self.subTest(value=value):
                self.assertIsNone(bls.corrected_energy_kcal({**base, "FAT": value}))
        snap = self.energy(**{**AFFECTED, "FAT": "1e308"})
        self.assertEqual(snap["automatic_use"]["status"], "review")
        self.assertFalse(snap["nutrients"]["energy_kcal"]["errata"]["corrected"])

    def test_correction_uses_original_decimal_text_with_provenance(self):
        raw = "0.1249999999999999999999999999999999999999"
        snap = self.energy(PROT625=raw, FAT=0, CHO=0, POLYL=0, FIBT=0,
                           ALC=0, OA=0, OLSAC=1, ENERCC=2, PROT625_ref="Synthetic precision")
        energy = snap["nutrients"]["energy_kcal"]
        self.assertEqual(energy["value"], 0)
        original = energy["errata"]["input_sources"]["PROT625"]
        self.assertEqual(original["raw"], raw)
        self.assertEqual(original["provenance"]["reference"], "Synthetic precision")

    def test_double_counted_olsac_energy_is_recomputed_without_the_olsac_term(self):
        snap = self.energy(**AFFECTED)
        kcal = snap["nutrients"]["energy_kcal"]
        self.assertEqual(kcal["value"], 291)
        self.assertEqual(kcal["qualifier"], "corrected")
        self.assertEqual(kcal["raw_value"], 299)
        self.assertEqual(kcal["raw"], "299")
        errata = kcal["errata"]
        self.assertTrue(errata["affected"] and errata["corrected"])
        self.assertEqual(errata["formula"], FORMULA)
        self.assertAlmostEqual(errata["unrounded"], 290.8)
        self.assertEqual(errata["inputs"], {"PROT625": 10, "FAT": 5, "CHO": 50, "POLYL": 2,
                                           "FIBT": 3, "ALC": 0, "OA": 1})
        self.assertNotIn("OLSAC", errata["inputs"])
        self.assertIn("Erratum_BLS_4_0_DE.pdf", errata["source"])
        self.assertEqual(snap["automatic_use"]["status"], "ok")
        self.assertTrue(any("erratum" in w.lower() for w in snap["warnings"]))

    def test_corrected_energy_rounds_half_up_to_whole_kcal(self):
        # 4*10 + 9*0.5 = 44.5 -> 45 (banker's rounding would give 44)
        snap = self.energy(PROT625=10, FAT=0.5, CHO=0, POLYL=0, FIBT=0, ALC=0, OA=0, OLSAC=1, ENERCC=47)
        self.assertEqual(snap["nutrients"]["energy_kcal"]["value"], 45)

    def test_unknown_input_means_no_correction_and_no_zero_substitution(self):
        for missing in ("PROT625", "FAT", "CHO", "POLYL", "FIBT", "ALC", "OA"):
            for marker in (None, "-", "TR", "<LOQ"):
                with self.subTest(missing=missing, marker=marker):
                    snap = self.energy(**{**AFFECTED, missing: marker})
                    kcal = snap["nutrients"]["energy_kcal"]
                    self.assertEqual(kcal["value"], 299)          # source value kept as is
                    self.assertEqual(kcal["qualifier"], "exact")
                    self.assertNotIn("raw_value", kcal)
                    self.assertTrue(kcal["errata"]["affected"])
                    self.assertFalse(kcal["errata"]["corrected"])
                    self.assertEqual(kcal["errata"]["unknown_inputs"], [missing])
                    self.assertEqual(snap["automatic_use"]["status"], "review")
                    self.assertIn(missing, " ".join(snap["automatic_use"]["reasons"]))
                    self.assertTrue(any("erratum" in w.lower() for w in snap["warnings"]))

    def test_rows_without_olsac_are_untouched(self):
        for olsac in (None, "-", 0):
            with self.subTest(olsac=olsac):
                snap = self.energy(**{**AFFECTED, "OLSAC": olsac, "ENERCC": 291})
                kcal = snap["nutrients"]["energy_kcal"]
                self.assertEqual((kcal["value"], kcal["qualifier"]), (291, "exact"))
                self.assertNotIn("errata", kcal)
                self.assertEqual(snap["automatic_use"], {"status": "ok", "reasons": []})
                self.assertEqual(snap["warnings"], [])

    def test_errata_apply_only_to_bls_4_0(self):
        snap = self.energy(sheet="BLS_4_1_Daten_2026_DE", **AFFECTED)
        self.assertEqual(snap["source_version"], "4.1")
        kcal = snap["nutrients"]["energy_kcal"]
        self.assertEqual((kcal["value"], kcal["qualifier"]), (299, "exact"))
        self.assertNotIn("errata", kcal)

    def test_anchovy_t104100_is_blocked_for_automatic_use_without_invented_values(self):
        snap = self.energy("T104100", PROT625=20.1, FAT=13.7, CHO=0, POLYL=0, FIBT=0, ALC=0, OA=0,
                           ENERCC=204)
        self.assertEqual(snap["automatic_use"]["status"], "blocked")
        self.assertIn("T104100", " ".join(snap["automatic_use"]["reasons"]))
        n = snap["nutrients"]
        self.assertEqual((n["fat_g"]["value"], n["energy_kcal"]["value"]), (13.7, 204))
        self.assertTrue(n["fat_g"]["unreliable"] and n["energy_kcal"]["unreliable"])
        self.assertNotIn("unreliable", n["protein_g"])

    def test_blocked_flag_is_in_the_search_result_too(self):
        self.energy("T104100", PROT625=20.1, FAT=13.7, CHO=0, POLYL=0, FIBT=0, ALC=0, OA=0, ENERCC=204)
        self.assertEqual(bls.search("T104100", self.db)["results"][0]["automatic_use"], "blocked")


class SaltDerivationTests(TempCase):
    def nutrients(self, **values):
        self.load([header_row(), food("C000002", "Synthetic", "Synthetic", ENERCC=1, **values)])
        return self.snapshot("C000002")["nutrients"]

    def test_salt_is_derived_from_sodium_only_when_the_source_has_no_salt(self):
        n = self.nutrients(NA=40)
        salt = n["salt_g"]
        self.assertAlmostEqual(salt["value"], 0.1)            # 40 mg * 2.5 / 1000
        self.assertEqual(salt["qualifier"], "derived")
        self.assertTrue(salt["derived"])
        self.assertEqual(salt["derived_from"], ["sodium_mg"])
        self.assertIn("2.5", salt["provenance"]["method"])
        self.assertIsNone(salt["raw"])
        self.assertEqual(n["sodium_mg"]["value"], 40)         # sodium itself stays what it was

    def test_source_salt_is_never_replaced_or_flagged_derived(self):
        n = self.nutrients(NA=40, NACL=0.2)
        self.assertEqual((n["salt_g"]["value"], n["salt_g"]["qualifier"]), (0.2, "exact"))
        self.assertNotIn("derived", n["salt_g"])

    def test_qualified_salt_is_not_replaced_by_a_derivation(self):
        n = self.nutrients(NA=40, NACL="<LOQ")
        self.assertEqual((n["salt_g"]["value"], n["salt_g"]["qualifier"]), (None, "lt_LOQ"))

    def test_no_sodium_no_salt_stays_missing(self):
        n = self.nutrients()
        self.assertEqual((n["salt_g"]["value"], n["salt_g"]["qualifier"]), (None, "missing"))

    def test_qualified_sodium_cannot_be_turned_into_salt(self):
        n = self.nutrients(NA="<LOD")
        self.assertEqual(n["salt_g"]["qualifier"], "missing")


def catalogue():
    plain = dict(ENERCC=100, PROT625=5, FAT=1, CHO=10, FIBT=1, NA=10)
    names = [
        ("C133000", "Hafer Flocken", "Oat flakes"),
        ("C133032", "Hafer Flocken, gekocht", "Oat flakes, cooked"),
        ("C660000", "Haferdrink ungesüßt", "Oat drink, unsweetened"),
        ("C352000", "Reis poliert, roh", "Rice polished, raw"),
        ("C352032", "Reis poliert, gekocht", "Rice polished, cooked"),
        ("G880001", "Preiselbeeren roh", "Cranberry raw"),
        ("M110000", "Speisequark Magerstufe", "Curd cheese, low fat"),
        ("Y880002", "Quarkspeise mit Erdbeeren", "Quark dessert with strawberries"),
        ("B900001", "Müsli, Früchte", "Muesli, fruit"),
        ("K100001", "Süßkartoffel gedünstet", "Sweet potato steamed"),
        ("S100001", "Rohschinken geräuchert", "Cured ham, smoked"),
        ("F100001", "Hähnchenbrust gebraten", "Chicken breast fried"),
    ]
    return [header_row()] + [food(c, n, e, **plain) for c, n, e in names]


class SearchTests(TempCase):
    def setUp(self):
        super().setUp()
        self.load(catalogue())

    def codes(self, query, **kw):
        return [r["code"] for r in bls.search(query, self.db, **kw)["results"]]

    def test_fold_removes_case_diacritics_and_sharp_s(self):
        self.assertEqual(bls.fold("Süßkartoffel"), "susskartoffel")
        self.assertEqual(bls.fold("Süßkartoffel", digraph=True), "suesskartoffel")
        self.assertEqual(bls.fold("  Hafer Flocken, GEKOCHT! "), "hafer flocken gekocht")
        # a hyphen between letters makes one compound word; between numbers it is just a separator
        self.assertEqual(bls.fold("Quark-Plunder"), "quarkplunder")
        self.assertEqual(bls.fold("Mehl Type 405-550"), "mehl type 405 550")
        self.assertEqual(bls.fold("Crème brûlée"), "creme brulee")

    def test_compound_query_finds_the_two_word_bls_name(self):
        found = self.codes("Haferflocken")
        self.assertEqual(found, ["C133000", "C133032"])      # not Haferdrink

    def test_all_words_must_match_and_case_does_not_matter(self):
        self.assertEqual(self.codes("HAFERFLOCKEN gekocht"), ["C133032"])
        self.assertEqual(self.codes("hafer FLOCKEN"), ["C133000", "C133032"])

    def test_umlauts_match_with_or_without_digraphs(self):
        for query in ("Müsli", "musli", "MUESLI"):
            with self.subTest(query=query):
                self.assertEqual(self.codes(query), ["B900001"])
        for query in ("süßkartoffel", "Susskartoffel", "suesskartoffel"):
            with self.subTest(query=query):
                self.assertEqual(self.codes(query), ["K100001"])

    def test_word_matches_rank_before_mid_word_matches(self):
        results = bls.search("quark", self.db)["results"]
        # German name decides: Speisequark (head noun) before Quarkspeise, whose English name
        # "Quark dessert" must not lift it
        self.assertEqual([(r["code"], r["match"], r["matched_in"]) for r in results],
                         [("M110000", "suffix", "de"), ("Y880002", "prefix", "de")])
        reis = bls.search("reis", self.db)["results"]
        self.assertEqual([r["code"] for r in reis][:2], ["C352000", "C352032"])
        self.assertEqual(reis[-1]["code"], "G880001")        # Preiselbeeren: weakest, last

    def test_english_names_are_a_fallback_ranked_after_german_matches(self):
        self.load([header_row(),
                   food("A1", "Reis poliert, roh", "Rice polished, raw", ENERCC=1),
                   food("A2", "Milchreis", "Rice pudding", ENERCC=1),
                   food("A3", "Weizen Vollkorn", "Whole wheat, rice flavoured", ENERCC=1)])
        results = bls.search("rice", self.db)["results"]
        self.assertEqual([(r["code"], r["matched_in"]) for r in results],
                         [("A2", "en"), ("A1", "en"), ("A3", "en")])   # shorter English name first
        german = bls.search("reis", self.db)["results"]
        self.assertEqual([(r["code"], r["matched_in"]) for r in german], [("A1", "de"), ("A2", "de")])

    def test_compound_query_ranks_the_plain_food_before_longer_compounds(self):
        # real BLS: "Hafer Flocken" is the plain food, "Haferflockenplätzchen" are cookies
        self.load(catalogue()[:3] + [food("D701600", "Haferflockenplätzchen", "Oat cookies", ENERCC=448),
                                     food("D701700", "Haferflocken-Nussplätzchen", "Oat nut cookies", ENERCC=517),
                                     food("X454663", "Haferflockensuppe geröstet", "Oat soup", ENERCC=36)])
        results = bls.search("Haferflocken", self.db)["results"]
        self.assertEqual([(r["code"], r["match"]) for r in results],
                         [("C133000", "exact"), ("C133032", "word"),
                          ("D701600", "prefix"), ("D701700", "prefix"), ("X454663", "prefix")])

    def test_german_head_noun_compounds_rank_before_compounds_that_merely_start_with_it(self):
        # In German the last part names the thing: Speisequark IS quark, Quarkcreme is a cream.
        self.load([header_row(),
                   food("Y880160", "Quarkcreme", "Cream", ENERCC=127),
                   food("Y880853", "Quarkauflauf", "Bake", ENERCC=232),
                   food("M713100", "Speisequark Magerstufe, Magerquark < 10 % Fett i. Tr.", "Curd cheese", ENERCC=67),
                   food("Y771510", "Kräuterquark (aus Magerquark)", "Herb curd", ENERCC=90),
                   food("G880001", "Preiselbeeren roh", "Cranberry raw", ENERCC=50)])
        results = bls.search("quark", self.db)["results"]
        self.assertEqual([(r["code"], r["match"]) for r in results],
                         [("Y771510", "suffix"), ("M713100", "suffix"),   # same tier: shorter name first
                          ("Y880160", "prefix"), ("Y880853", "prefix")])
        self.assertEqual([r["match"] for r in bls.search("reis", self.db)["results"]], ["substring"])

    def test_exact_name_ranks_first(self):
        self.assertEqual(self.codes("hafer flocken")[0], "C133000")
        self.assertEqual(bls.search("Hafer Flocken", self.db)["results"][0]["match"], "exact")

    def test_english_names_and_exact_codes_are_searchable(self):
        self.assertEqual(self.codes("oat flakes cooked"), ["C133032"])
        self.assertEqual(bls.search("oat flakes cooked", self.db)["results"][0]["matched_in"], "en")
        self.assertEqual(self.codes("c133000"), ["C133000"])

    def test_titles_and_fields_distinguish_raw_from_cooked(self):
        by_code = {r["code"]: r for r in bls.search("reis", self.db)["results"]}
        raw, cooked = by_code["C352000"], by_code["C352032"]
        self.assertEqual((raw["preparation"], cooked["preparation"]), ("raw", "cooked"))
        self.assertIn("[raw]", raw["title"])
        self.assertIn("[cooked]", cooked["title"])
        self.assertIn("BLS C352032", cooked["title"])
        self.assertIn("Reis poliert, gekocht", cooked["title"])
        other = {r["code"]: r["preparation"] for r in bls.search("haferflocken", self.db)["results"]}
        self.assertEqual(other["C133000"], "unspecified")      # the name says neither
        self.assertIn("[unspecified]", [r for r in bls.search("hafer flocken", self.db)["results"]
                                         if r["code"] == "C133000"][0]["title"])

    def test_preparation_words_are_whole_words_and_include_cooking_methods(self):
        prep = {c: bls.search(c, self.db)["results"][0]["preparation"]
                for c in ("S100001", "K100001", "F100001")}
        self.assertEqual(prep, {"S100001": "unspecified",     # Rohschinken is not "roh"
                                "K100001": "cooked", "F100001": "cooked"})

    def test_preparation_filter(self):
        self.assertEqual(self.codes("reis", preparation="raw"), ["C352000", "G880001"])
        self.assertEqual(self.codes("reis", preparation="cooked"), ["C352032"])

    def test_results_are_a_list_to_choose_from_never_an_automatic_pick(self):
        out = bls.search("reis", self.db)
        self.assertTrue(out["selection_required"])
        self.assertFalse({"best", "selected", "chosen", "match_id"} & set(out))
        self.assertIn("Max Rubner-Institut", out["attribution"])
        self.assertEqual(out["bls_version"], "4.0")

    def test_no_fuzzy_matching(self):
        for typo in ("Haferflockn", "Rais", "Qarks", "oatmeal"):
            with self.subTest(typo=typo):
                self.assertEqual(self.codes(typo), [])

    def test_limit_reports_truncation_and_total(self):
        out = bls.search("hafer", self.db, limit=2)
        self.assertEqual((len(out["results"]), out["total"], out["truncated"]), (2, 3, True))
        full = bls.search("hafer", self.db)
        self.assertFalse(full["truncated"])

    def test_result_carries_a_per_100g_preview_and_the_use_status(self):
        first = bls.search("c133000", self.db)["results"][0]
        self.assertEqual(first["preview"], {"energy_kcal": 100, "protein_g": 5, "carbs_g": 10, "fat_g": 1})
        self.assertEqual(first["automatic_use"], "ok")

    def test_unsearchable_queries_are_errors_not_match_everything(self):
        for bad in ("", "   ", "%", "_%_", "!!!", "x" * 500):
            with self.subTest(bad=bad), self.assertRaises(bls.BlsError):
                bls.search(bad, self.db)

    def test_sql_wildcards_and_quotes_are_inert(self):
        # '%' and '_' are not LIKE wildcards: the query behaves like its folded words
        self.assertEqual(self.codes("r_is"), self.codes("r is"))
        self.assertEqual(self.codes("rei%"), self.codes("rei"))
        self.assertEqual(self.codes("reis%%%%%"), self.codes("reis"))
        for hostile in ("'; DROP TABLE foods; --", 'reis" OR 1=1', "x') OR ('1'='1"):
            self.assertEqual(self.codes(hostile), [])
        self.assertEqual(self.codes("reis"), ["C352000", "C352032", "G880001"])


NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def wrap_zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


class HostileInputTests(TempCase):
    def rejects(self, data, name="evil.xlsx", **kw):
        path = self.write(name, data)
        with self.assertRaises(bls.ImportRejected) as ctx:
            bls.import_bls(path, self.db, **kw)
        self.assertFalse(self.db.exists())
        return str(ctx.exception)

    BILLION_LAUGHS = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>')

    def test_entity_declarations_are_refused_in_every_xml_part(self):
        good = synthetic_rows()
        sheet = (self.BILLION_LAUGHS + f'<worksheet {NS}><sheetData><row r="1"><c r="A1" t="inlineStr">'
                 '<is><t>&lol3;</t></is></c></row></sheetData></worksheet>')
        self.assertIn("DOCTYPE", self.rejects(make_xlsx(good, sheet_xml=sheet)))
        sst = self.BILLION_LAUGHS + f'<sst {NS}><si><t>&lol3;</t></si></sst>'
        self.assertIn("DOCTYPE", self.rejects(make_xlsx(good, shared_xml=sst)))
        book = (self.BILLION_LAUGHS + f'<workbook {NS} xmlns:r="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships"><sheets><sheet name="BLS_4_0_Daten" sheetId="1" '
                'r:id="rId1"/></sheets></workbook>')
        self.assertIn("DOCTYPE", self.rejects(make_xlsx(good, workbook_xml=book)))

    def test_a_doctype_split_across_read_chunks_is_still_found(self):
        for pad in range(16384 - 40, 16384 + 4, 3):
            with self.subTest(pad=pad):
                sheet = ('<?xml version="1.0"?><!--' + "a" * pad + '--><!DOCTYPE x [<!ENTITY e "v">]>'
                         f'<worksheet {NS}><sheetData/></worksheet>')
                self.assertIn("DOCTYPE", self.rejects(make_xlsx(synthetic_rows(), sheet_xml=sheet)))

    def test_non_utf8_xml_cannot_smuggle_a_doctype_past_the_scan(self):
        sheet = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "v">]>'
                 f'<worksheet {NS}><sheetData/></worksheet>').encode("utf-16")
        self.assertIn("UTF-8", self.rejects(make_xlsx(synthetic_rows(), sheet_xml=sheet)))

    def test_member_larger_than_the_limit_is_refused_before_it_is_read(self):
        limits = bls.Limits(max_member_bytes=10_000)
        sheet = f'<worksheet {NS}><sheetData>' + " " * 200_000 + '</sheetData></worksheet>'
        self.assertIn("too large", self.rejects(make_xlsx(synthetic_rows(), sheet_xml=sheet), limits=limits))

    def test_extreme_compression_ratio_is_refused(self):
        limits = bls.Limits(max_ratio=50, ratio_floor_bytes=10_000)
        sheet = f'<worksheet {NS}><sheetData>' + " " * 3_000_000 + '</sheetData></worksheet>'
        self.assertIn("ratio", self.rejects(make_xlsx(synthetic_rows(), sheet_xml=sheet), limits=limits))

    def test_total_size_and_member_count_are_capped(self):
        data = make_xlsx(synthetic_rows())
        self.assertIn("total", self.rejects(data, limits=bls.Limits(max_total_bytes=500)))
        crowded = make_xlsx(synthetic_rows(), extra_members={f"junk/{i}.txt": "x" for i in range(30)})
        self.assertIn("members", self.rejects(crowded, limits=bls.Limits(max_members=10)))

    def test_row_and_column_caps(self):
        many_rows = synthetic_rows([food(f"C{i:06d}", f"Food {i}", "Food", ENERCC=1) for i in range(20)])
        self.assertIn("rows", self.rejects(make_xlsx(many_rows), limits=bls.Limits(max_rows=10)))
        self.assertIn("columns", self.rejects(make_xlsx(synthetic_rows()), limits=bls.Limits(max_columns=20)))

    def test_sheet_part_must_be_a_plain_worksheet_not_a_traversal(self):
        good = make_xlsx(synthetic_rows())
        for target in ("../../etc/passwd", "worksheets/../evil.xml", "/xl/worksheets/sheet1.xml",
                       "http://example.com/sheet1.xml"):
            with self.subTest(target=target):
                with zipfile.ZipFile(io.BytesIO(good)) as z:
                    members = {n: z.read(n) for n in z.namelist()}
                members["xl/_rels/workbook.xml.rels"] = members["xl/_rels/workbook.xml.rels"].replace(
                    b"worksheets/sheet1.xml", target.encode())
                self.assertIn("sheet part", self.rejects(wrap_zip(members)))

    def test_a_zip_without_exactly_one_data_workbook_is_refused(self):
        inner = make_xlsx(synthetic_rows())
        for members in ({}, {"a/BLS_4_0_Components_DE_EN.xlsx": inner},
                        {"a/BLS_4_0_Daten_2025_DE.xlsx": inner, "b/BLS_4_0_Daten_2026_DE.xlsx": inner}):
            with self.subTest(members=list(members)):
                self.assertIn("exactly one", self.rejects(wrap_zip(members), "x.zip"))

    def test_traversal_member_names_are_refused(self):
        inner = make_xlsx(synthetic_rows())
        for name in ("../BLS_4_0_Daten_2025_DE.xlsx", "/abs/BLS_4_0_Daten_2025_DE.xlsx"):
            with self.subTest(name=name):
                self.assertIn("unsafe", self.rejects(wrap_zip({name: inner}), "x.zip"))

    def test_unrelated_members_are_never_opened(self):
        # a corrupt bomb sitting next to the data in the official zip must not matter
        inner = make_xlsx(synthetic_rows())
        outer = wrap_zip({"BLS_4_0_2025_DE/BLS_4_0_Daten_2025_DE.xlsx": inner,
                          "BLS_4_0_2025_DE/other.bin": b"\0" * 5_000_000})
        path = self.write("x.zip", outer)
        self.assertEqual(bls.import_bls(path, self.db, limits=bls.Limits(max_total_bytes=1_000_000))["foods"], 1)

    def test_garbage_truncated_and_corrupt_files_are_clean_rejections(self):
        good = make_xlsx(synthetic_rows())
        flipped = bytearray(good)
        flipped[len(flipped) // 3] ^= 0xFF
        for name, data in {"empty": b"", "text": b"hello", "truncated": good[:len(good) // 2],
                           "corrupt": bytes(flipped)}.items():
            with self.subTest(case=name):
                self.rejects(data, f"{name}.xlsx")

    def test_malformed_xml_is_a_clean_rejection(self):
        self.rejects(make_xlsx(synthetic_rows(), sheet_xml=f'<worksheet {NS}><sheetData><row'))

    def test_a_workbook_without_the_bls_header_or_sheet_is_refused(self):
        self.assertIn("header", self.rejects(make_xlsx([[("s", "Name"), ("s", "Kcal")], [("s", "Apple"), ("n", "52")]])))
        self.assertIn("BLS data sheet", self.rejects(make_xlsx(synthetic_rows(), sheet_name="Sheet1")))
        no_unit = [header_row([c[:2] + ("g",) if c[0] == "NA" else c for c in NUTRIENT_COLUMNS]), oats()]
        self.assertIn("NA", self.rejects(make_xlsx(no_unit)))
        missing = [header_row([c for c in NUTRIENT_COLUMNS if c[0] != "FAT"]), oats()]
        self.assertIn("FAT", self.rejects(make_xlsx(missing)))

    def test_duplicate_codes_and_empty_data_are_refused(self):
        self.assertIn("duplicate", self.rejects(make_xlsx(synthetic_rows([oats()]))))
        self.assertIn("no foods", self.rejects(make_xlsx([header_row()])))

    def test_guarded_reader_never_returns_more_than_asked_and_caps_bytes(self):
        payload = b"<a>" + b"x" * 300_000 + b"</a>"
        for ask in (1, 16 * 1024, 65_536, 200_000):
            with self.subTest(ask=ask):
                reader = bls._GuardedReader(io.BytesIO(payload), 10 ** 7, "part")
                self.assertLessEqual(len(reader.read(ask)), ask)
        small = bls._GuardedReader(io.BytesIO(payload), 1000, "part")
        with self.assertRaises(bls.ImportRejected):
            while small.read(65_536):
                pass

    def test_import_memory_does_not_grow_with_the_sheet(self):
        import tracemalloc
        rows = synthetic_rows([food(f"C{i:06d}", f"Food {i}", "Food", ENERCC=100, PROT625=5, FAT=1, CHO=10,
                                    FIBT=1, NA=10, SUGAR=1, NACL=0.1, WATER=70, POLYL=0, ALC=0, OA=0)
                               for i in range(3000)])
        path = self.write("big.xlsx", make_xlsx(rows))
        tracemalloc.start()
        try:
            summary = bls.import_bls(path, self.db)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(summary["foods"], 3001)
        self.assertLess(peak, 12 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB for {3001 * 40} cells")


class TransactionTests(TempCase):
    def bytes_and_files(self):
        return self.db.read_bytes(), sorted(p.name for p in self.db.parent.iterdir())

    def test_invalid_input_leaves_the_existing_database_byte_for_byte(self):
        self.load()
        before = self.bytes_and_files()
        bad_rows = synthetic_rows([food("C999999", "Bad", "Bad", ENERCC=float("nan"))])
        for label, data in {"nan": make_xlsx(bad_rows), "garbage": b"nope",
                            "no header": make_xlsx([[("s", "x")]]),
                            "bomb": make_xlsx(synthetic_rows(), sheet_xml="<worksheet><!DOCTYPE x></worksheet>")}.items():
            with self.subTest(label), self.assertRaises(bls.ImportRejected):
                bls.import_bls(self.write(f"{label}.xlsx", data), self.db)
            self.assertEqual(self.bytes_and_files(), before)
        with self.assertRaises(bls.ImportRejected):
            bls.import_bls(self.dir / "missing.xlsx", self.db)
        self.assertEqual(self.bytes_and_files(), before)
        self.assertEqual(self.snapshot()["name"], "Hafer Flocken")

    def test_a_failure_in_the_last_row_still_keeps_the_old_data(self):
        self.load()
        before = self.bytes_and_files()
        rows = synthetic_rows([food(f"C{i:06d}", f"Food {i}", "F", ENERCC=1) for i in range(200)]
                              + [food("C999999", "Bad", "Bad", ENERCC=-5)])
        with self.assertRaises(bls.ImportRejected):
            bls.import_bls(self.workbook(rows, "late.xlsx"), self.db)
        self.assertEqual(self.bytes_and_files(), before)

    def test_a_crash_at_the_swap_leaves_old_data_and_no_staging_file(self):
        self.load()
        before = self.bytes_and_files()
        with mock.patch.object(bls.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                bls.import_bls(self.workbook(name="new.xlsx"), self.db)
        self.assertEqual(self.bytes_and_files(), before)

    def test_a_successful_import_replaces_everything_rather_than_merging(self):
        self.load(catalogue())
        self.assertEqual(bls.info(self.db)["foods"], 12)
        self.load([header_row(), food("C777777", "Nur dieses", "Only this", ENERCC=10)])
        self.assertEqual(bls.info(self.db)["foods"], 1)
        with self.assertRaises(bls.NotFound):
            bls.get("C133000", self.db)
        self.assertEqual(bls.get("C777777", self.db)["name"], "Nur dieses")

    def test_database_is_private_and_independent_of_other_stores(self):
        self.load()
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.db.parent.stat().st_mode & 0o777, 0o700)
        tables = {r[0] for r in sqlite3.connect(self.db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"meta", "foods"})      # no meal ledger tables, no OFF tables

    def test_pre_hardening_database_is_refused_until_reimport(self):
        self.load()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        with self.assertRaises(bls.StaleDatabase):
            bls.get("C133000", self.db)

    def test_a_database_from_another_index_version_must_be_reimported(self):
        self.load()
        with sqlite3.connect(self.db) as raw:
            raw.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        for call in (lambda: bls.search("hafer", self.db), lambda: bls.get("C133000", self.db),
                     lambda: bls.info(self.db)):
            with self.assertRaises(bls.StaleDatabase) as ctx:
                call()
            self.assertIn("import", str(ctx.exception))
        self.load()                                   # re-import repairs it
        self.assertEqual(self.codes_for("hafer"), ["C133000"])

    def codes_for(self, query):
        return [r["code"] for r in bls.search(query, self.db)["results"]]

    def test_info_reports_provenance(self):
        path = self.workbook()
        bls.import_bls(path, self.db)
        info = bls.info(self.db)
        self.assertEqual((info["foods"], info["bls_version"], info["license_id"]), (1, "4.0", "CC-BY-4.0"))
        self.assertEqual(info["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertTrue(info["imported_at"].endswith("+00:00"))
        self.assertNotIn(str(self.dir), json.dumps(info))        # no personal paths in the metadata


class CommandLineTests(TempCase):
    def setUp(self):
        super().setUp()
        self.home = self.dir / "home"
        self.env = {**os.environ, "COACH_HOME": str(self.home), "HOME": str(self.dir)}
        for name in ("RUNNING_COACH_HOME",):
            self.env.pop(name, None)
        self.default_db = self.home / "athlete" / "state" / "nutrition-providers" / "bls.sqlite3"

    def run_cli(self, *args, expect=0):
        done = subprocess.run([sys.executable, str(SCRIPTS / "nutrition_bls.py"), *args],
                              capture_output=True, text=True, env=self.env, cwd=self.dir, timeout=120)
        self.assertEqual(done.returncode, expect, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        return done

    def imported(self):
        return self.run_cli("import", str(self.workbook(catalogue(), name="c.xlsx")))

    def test_import_defaults_to_the_private_state_directory_and_prints_attribution(self):
        out = self.imported().stdout
        self.assertTrue(self.default_db.is_file())
        self.assertIn("12 foods", out)
        self.assertIn("BLS 4.0", out)
        self.assertRegex(out, r"sha256\s+[0-9a-f]{64}")
        self.assertIn("Max Rubner-Institut", out)
        self.assertIn("CC BY 4.0", out)
        self.assertEqual(sorted(p.name for p in self.home.rglob("*") if p.is_file()), ["bls.sqlite3"])

    def test_search_prints_selectable_titles_and_attribution(self):
        self.imported()
        out = self.run_cli("search", "Reis").stdout
        self.assertIn("Reis poliert, roh [raw] · BLS C352000", out)
        self.assertIn("Reis poliert, gekocht [cooked] · BLS C352032", out)
        self.assertIn("choose", out.lower())
        self.assertIn("Max Rubner-Institut", out)

    def test_search_json_and_filters(self):
        self.imported()
        data = json.loads(self.run_cli("search", "reis", "--json", "--prep", "cooked", "--limit", "5").stdout)
        self.assertEqual([r["code"] for r in data["results"]], ["C352032"])
        self.assertIn("attribution", data)

    def test_get_prints_per_100g_values_units_and_flags(self):
        self.imported()
        out = self.run_cli("get", "C133000").stdout
        for needle in ("Hafer Flocken", "per 100 g", "energy_kcal", "kcal", "sodium_mg", "mg",
                       "Max Rubner-Institut"):
            self.assertIn(needle, out)
        snap = json.loads(self.run_cli("get", "c133000", "--json").stdout)
        self.assertEqual(snap["source_id"], "C133000")
        self.assertEqual(snap["nutrients"]["energy_kcal"]["unit"], "kcal")

    def test_unknown_code_and_missing_database_are_plain_errors(self):
        done = self.run_cli("get", "C133000", expect=1)
        self.assertIn("no BLS database", done.stderr)
        self.imported()
        self.assertIn("no BLS food", self.run_cli("get", "ZZZZZZZ", expect=1).stderr)
        self.assertIn("nothing to search", self.run_cli("search", "%%", expect=1).stderr)

    def test_bad_import_fails_cleanly_and_keeps_the_database(self):
        self.imported()
        before = self.default_db.read_bytes()
        bad = self.write("bad.xlsx", b"not a workbook")
        self.assertIn("not a zip", self.run_cli("import", str(bad), expect=1).stderr)
        self.assertEqual(self.default_db.read_bytes(), before)

    def test_explicit_db_path_overrides_the_default(self):
        other = self.dir / "elsewhere.sqlite3"
        self.run_cli("import", str(self.workbook()), "--db", str(other))
        self.assertTrue(other.is_file() and not self.default_db.exists())
        self.assertIn("C133000", self.run_cli("search", "hafer", "--db", str(other)).stdout)

    def test_a_closed_pipe_is_not_an_error(self):
        self.imported()
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "nutrition_bls.py"), "search", "reis"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, cwd=self.dir)
        proc.stdout.readline()
        proc.stdout.close()
        stderr = proc.stderr.read().decode()
        proc.wait(timeout=30)
        proc.stderr.close()
        self.assertNotIn("Traceback", stderr)

    def test_info_command(self):
        self.imported()
        out = self.run_cli("info").stdout
        self.assertIn("12 foods", out)
        self.assertIn("CC-BY-4.0", out)


class SafeStagingTests(TempCase):
    def test_download_does_not_follow_predictable_part_symlink(self):
        victim = self.write("victim", b"untouched")
        trap = self.dir / f".BLS_4_0_2025_DE.zip.{os.getpid()}.part"
        trap.symlink_to(victim)
        bls.download(self.dir, opener=lambda *a, **k: DownloadTests.Response(b"PK\x03\x04payload"))
        self.assertEqual(victim.read_bytes(), b"untouched")
        self.assertTrue(trap.is_symlink())

    def test_sqlite_staging_is_exclusive_private_before_population(self):
        source = self.workbook()
        self.db.parent.mkdir(mode=0o700)
        trap = self.db.with_name(f".{self.db.name}.{os.getpid()}.new")
        trap.write_bytes(b"not ours")
        connect = sqlite3.connect
        def checked(path, *args, **kwargs):
            self.assertTrue(Path(path).is_file(), "staging must exist before SQLite opens it")
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(Path(path), trap)
            return connect(path, *args, **kwargs)
        with mock.patch.object(bls.sqlite3, "connect", side_effect=checked):
            bls.import_bls(source, self.db)
        self.assertEqual(trap.read_bytes(), b"not ours")

    def test_unsafe_output_directories_are_rejected_without_chmod(self):
        source = self.workbook()
        for kind in ("broad", "symlink", "writable_ancestor", "foreign_owner"):
            with self.subTest(kind=kind):
                parent = self.dir / kind
                parent.mkdir(mode=0o700)
                if kind == "broad":
                    parent.chmod(0o755)
                elif kind == "symlink":
                    link = self.dir / "link"
                    link.symlink_to(parent, target_is_directory=True)
                    parent = link
                elif kind == "writable_ancestor":
                    parent.chmod(0o777)
                    parent = parent / "private"
                    parent.mkdir(mode=0o700)
                owner_patch = mock.patch.object(bls.os, "geteuid", return_value=os.geteuid() + 1) if kind == "foreign_owner" else mock.patch.object(bls.os, "geteuid", wraps=os.geteuid)
                before = parent.stat().st_mode
                with owner_patch:
                    with self.assertRaises(bls.BlsError):
                        bls.import_bls(source, parent / "db")
                    with self.assertRaises(bls.BlsError):
                        bls.download(parent, opener=lambda *a, **k: DownloadTests.Response(b"PK\x03\x04"))
                self.assertEqual(parent.stat().st_mode, before)


class DownloadTests(TempCase):
    URL = bls.DOWNLOAD_URL

    class Response(io.BytesIO):
        def __init__(self, data, url=None, length=True):
            super().__init__(data)
            self._url = url
            self.headers = {"Content-Length": str(len(data))} if length else {}

        def geturl(self):
            return self._url

    def opener(self, data, url=None, length=True):
        calls = []

        def open_(request, timeout=None):
            calls.append(request.full_url if hasattr(request, "full_url") else request)
            return self.Response(data, url or self.URL, length)
        open_.calls = calls
        return open_

    def zipped(self):
        return wrap_zip({"BLS_4_0_2025_DE/BLS_4_0_Daten_2025_DE.xlsx": make_xlsx(synthetic_rows())})

    def test_real_http_redirect_is_refused_before_target_request(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from urllib.parse import urlsplit
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.headers.get("Host"), self.path))
                if self.path == "/source.zip":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/target.zip")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"PK\x03\x04payload")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_port}/source.zip"
        def local_only(candidate):
            if candidate != url:
                raise bls.BlsError("foreign redirect")
            return urlsplit(candidate)
        try:
            with mock.patch.object(bls, "_check_download_url", side_effect=local_only):
                with self.assertRaises(bls.BlsError):
                    bls.download(self.dir, url=url)
            self.assertEqual(requests, [(f"127.0.0.1:{server.server_port}", "/source.zip")])
            self.assertFalse((self.dir / "source.zip").exists())
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()

    def test_official_url_is_the_pinned_https_blsdb_download(self):
        self.assertEqual(self.URL, "https://blsdb.de/assets/uploads/BLS_4_0_2025_DE.zip")

    def test_download_writes_a_private_zip_and_reports_its_hash(self):
        data = self.zipped()
        opener = self.opener(data)
        result = bls.download(self.dir / "cache", opener=opener)
        self.assertEqual(Path(result["path"]).read_bytes(), data)
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(opener.calls, [self.URL])
        self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(bls.import_bls(result["path"], self.db)["foods"], 1)   # and it imports

    def test_download_refuses_other_hosts_schemes_redirects_size_and_non_zips(self):
        for url in ("http://blsdb.de/assets/uploads/BLS_4_0_2025_DE.zip", "https://evil.example/x.zip",
                    "https://blsdb.de.evil.example/x.zip", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(bls.BlsError):
                bls.download(self.dir / "cache", url=url, opener=self.opener(self.zipped()))
        with self.assertRaises(bls.BlsError):          # redirected somewhere else
            bls.download(self.dir / "cache", opener=self.opener(self.zipped(), url="https://evil.example/x.zip"))
        with self.assertRaises(bls.BlsError):          # too big, announced
            bls.download(self.dir / "cache", opener=self.opener(self.zipped()), max_bytes=100)
        with self.assertRaises(bls.BlsError):          # too big, not announced
            bls.download(self.dir / "cache", opener=self.opener(self.zipped(), length=False), max_bytes=100)
        with self.assertRaises(bls.BlsError):          # not a zip
            bls.download(self.dir / "cache", opener=self.opener(b"<html>login</html>"))
        self.assertEqual(list((self.dir / "cache").glob("*")) if (self.dir / "cache").exists() else [], [])

    def test_cli_download_needs_an_explicit_yes_and_touches_no_network_without_it(self):
        env = {**os.environ, "COACH_HOME": str(self.dir / "home"), "HOME": str(self.dir)}
        done = subprocess.run([sys.executable, str(SCRIPTS / "nutrition_bls.py"), "download"],
                              capture_output=True, text=True, env=env, cwd=self.dir, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("--yes", done.stderr)
        self.assertIn(self.URL, done.stderr)
        self.assertFalse((self.dir / "home").exists())


if __name__ == "__main__":
    unittest.main()
