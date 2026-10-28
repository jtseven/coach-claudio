"""Offline nutrition-logging tests: synthetic fixtures, disposable homes, no network."""
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import nutrition_core as core  # noqa: E402
import nutrition_store as store  # noqa: E402
import food_providers as fp  # noqa: E402
import http.server  # noqa: E402
import hashlib  # noqa: E402
from unittest.mock import patch  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timezone, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 25, 13, 0, tzinfo=BERLIN)


def item(**over):
    base = {"name": "Synthetic yoghurt", "amount": "250 g", "basis": {"per": "100g"},
            "nutrients": {"energy_kcal": 60, "protein_g": 10, "carbohydrate_g": 4,
                          "sugars_g": 4, "fat_g": 0.2, "fiber_g": 0, "salt_g": 0.1},
            "provenance": {"kind": "label"}}
    base.update(over)
    return base


class NumberParsingTests(unittest.TestCase):
    def test_accepts_ints_floats_and_comma_or_dot_decimal_strings(self):
        self.assertEqual(core.parse_number(3), Decimal("3"))
        self.assertEqual(core.parse_number(2.5), Decimal("2.5"))
        self.assertEqual(core.parse_number("4,5"), Decimal("4.5"))
        self.assertEqual(core.parse_number(" 12.25 "), Decimal("12.25"))

    def test_rejects_bool_nan_inf_negative_and_garbage(self):
        for bad in (True, False, float("nan"), float("inf"), "-1", -0.5, "abc", "1,2,3",
                    "", None, [], {}, "1e400", "NaN", "Infinity"):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                core.parse_number(bad)

    def test_trace_values_are_zero_and_marked(self):
        self.assertEqual(core.parse_nutrient_value("<0,5"), (Decimal("0"), True))
        self.assertEqual(core.parse_nutrient_value("1,2"), (Decimal("1.2"), False))

    def test_quantities_must_be_strictly_positive(self):
        for bad in ("0 g", {"value": 0, "unit": "g"}, {"value": -3, "unit": "g"},
                    {"value": True, "unit": "g"}, "250", "250 bananas", {"value": 1}):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                core.parse_quantity(bad)
        self.assertEqual(core.parse_quantity("0,5 l"), (Decimal("0.5"), "l"))
        self.assertEqual(core.parse_quantity({"value": "2", "unit": "serving"}),
                         (Decimal("2"), "serving"))


class ConversionTests(unittest.TestCase):
    def compute(self, **over):
        return core.compute_item(item(**over))

    def test_per_100g_scales_by_grams_and_kg(self):
        out = self.compute(amount="250 g")
        self.assertEqual(out["nutrients"]["energy_kcal"], 150)
        self.assertEqual(out["nutrients"]["protein_g"], 25)
        self.assertEqual(out["amount_g"], 250)
        self.assertEqual(self.compute(amount="0,25 kg")["nutrients"]["protein_g"], 25)

    def test_per_100ml_scales_by_volume_units(self):
        drink = {"per": "100ml"}
        n = {"energy_kcal": 42, "carbohydrate_g": 10.6, "sugars_g": 10.6}
        self.assertEqual(self.compute(amount="0,33 l", basis=drink, nutrients=n)
                         ["nutrients"]["energy_kcal"], 138.6)
        self.assertEqual(self.compute(amount="33 cl", basis=drink, nutrients=n)
                         ["nutrients"]["energy_kcal"], 138.6)

    def test_volume_never_treated_as_grams_without_density(self):
        with self.assertRaises(core.MissingInfo) as cm:
            self.compute(amount="200 ml")
        self.assertIn("density_g_per_ml", str(cm.exception))
        self.assertIn("per 100 g", cm.exception.question)
        out = self.compute(amount="200 ml", density_g_per_ml=1.03)
        self.assertEqual(out["amount_g"], 206)
        self.assertEqual(out["nutrients"]["protein_g"], 20.6)

    def test_mass_against_per_100ml_needs_density(self):
        with self.assertRaises(core.MissingInfo):
            self.compute(amount="100 g", basis={"per": "100ml"})

    def test_per_serving_and_servings(self):
        basis = {"per": "serving", "serving": "30 g"}
        n = {"energy_kcal": 120, "protein_g": 3, "carbohydrate_g": 20, "fat_g": 3}
        self.assertEqual(self.compute(amount="2 serving", basis=basis, nutrients=n)
                         ["nutrients"]["energy_kcal"], 240)
        self.assertEqual(self.compute(amount="45 g", basis=basis, nutrients=n)
                         ["nutrients"]["energy_kcal"], 180)
        with self.assertRaises(core.MissingInfo):
            self.compute(amount="45 g", basis={"per": "serving"}, nutrients=n)

    def test_servings_against_per_100g_needs_serving_size(self):
        with self.assertRaises(core.MissingInfo):
            self.compute(amount="1 serving")
        out = self.compute(amount="1,5 serving", basis={"per": "100g", "serving": "40 g"})
        self.assertEqual(out["amount_g"], 60)

    def test_total_basis_is_the_eaten_amount(self):
        out = self.compute(amount=None, basis={"per": "total"},
                           nutrients={"energy_kcal": 650, "protein_g": 25},
                           provenance={"kind": "estimate", "confidence": 0.5})
        self.assertEqual(out["nutrients"]["energy_kcal"], 650)
        self.assertIsNone(out["nutrients"]["fat_g"])

    def test_missing_amount_is_missing_not_zero(self):
        with self.assertRaises(core.MissingInfo) as cm:
            self.compute(amount=None)
        self.assertIn("amount", str(cm.exception))

    def test_kj_converts_to_kcal_and_cross_checks(self):
        out = self.compute(nutrients={"energy_kj": 418.4, "protein_g": 25,
                                      "carbohydrate_g": 0, "fat_g": 0})
        self.assertEqual(out["basis_nutrients"]["energy_kcal"], 100)
        out = self.compute(nutrients={"energy_kj": 1000, "energy_kcal": 100})
        self.assertTrue(any("kJ" in w for w in out["warnings"]))

    def test_sodium_to_salt_factor_2_5(self):
        out = self.compute(nutrients={"sodium_g": 0.4})
        self.assertEqual(out["basis_nutrients"]["salt_g"], 1)
        out = self.compute(nutrients={"sodium_mg": 400})
        self.assertEqual(out["basis_nutrients"]["salt_g"], 1)
        self.assertTrue(any("sodium" in w for w in out["warnings"]))

    def test_unknown_nutrients_stay_null(self):
        out = self.compute(nutrients={"energy_kcal": 60, "protein_g": None})
        for key in ("protein_g", "carbohydrate_g", "sugars_g", "fat_g", "fiber_g", "salt_g"):
            self.assertIsNone(out["nutrients"][key], key)
        self.assertEqual(out["nutrients"]["energy_kcal"], 150)

    def test_trace_marked_and_zero(self):
        out = self.compute(nutrients={"energy_kcal": 60, "salt_g": "<0,01"})
        self.assertEqual(out["nutrients"]["salt_g"], 0)
        self.assertEqual(out["trace"], ["salt_g"])

    def test_us_total_carbohydrate_becomes_available_carbohydrate(self):
        out = self.compute(nutrients={"carbohydrate_total_g": 30, "fiber_g": 5})
        self.assertEqual(out["basis_nutrients"]["carbohydrate_g"], 25)
        out = self.compute(nutrients={"energy_kcal": 120, "carbohydrate_total_g": 30})
        self.assertIsNone(out["basis_nutrients"]["carbohydrate_g"])
        self.assertTrue(out["warnings"])


class PlausibilityTests(unittest.TestCase):
    def test_sugars_cannot_exceed_carbohydrate(self):
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(nutrients={"carbohydrate_g": 4, "sugars_g": 14}))
        # label rounding tolerance
        core.compute_item(item(nutrients={"carbohydrate_g": 0.4, "sugars_g": 0.5}))

    def test_per_100g_mass_cannot_exceed_100g(self):
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(nutrients={"protein_g": 45, "carbohydrate_g": 60, "fat_g": 5}))

    def test_energy_per_100g_rejects_kj_misread_as_kcal(self):
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(nutrients={"energy_kcal": 1600}))

    def test_atwater_mismatch_warns_but_keeps_label(self):
        # 40 % ABV spirits: label energy comes from alcohol, not 4/4/9.
        out = core.compute_item(item(basis={"per": "100ml"}, amount="40 ml",
                                     nutrients={"energy_kcal": 231, "protein_g": 0,
                                                "carbohydrate_g": 0, "fat_g": 0}))
        self.assertEqual(out["basis_nutrients"]["energy_kcal"], 231)
        self.assertTrue(any("4/4/9" in w for w in out["warnings"]))
        # fibre at 2 kcal/g keeps a high-fibre label consistent
        out = core.compute_item(item(nutrients={"energy_kcal": 340, "protein_g": 12,
                                                "carbohydrate_g": 55, "fat_g": 6,
                                                "fiber_g": 10}))
        self.assertFalse([w for w in out["warnings"] if "4/4/9" in w])

    def test_unknown_nutrient_keys_are_rejected(self):
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(nutrients={"fibre_g": 3}))

    def test_estimate_requires_confidence(self):
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(provenance={"kind": "estimate"}))
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(provenance={"kind": "estimate", "confidence": 1.5}))
        with self.assertRaises(core.InvalidInput):
            core.compute_item(item(provenance={"kind": "guess"}))


class TotalsTests(unittest.TestCase):
    def test_totals_keep_unknown_visible(self):
        a = core.compute_item(item())
        b = core.compute_item(item(name="b", nutrients={"energy_kcal": 100, "fiber_g": None}))
        tot = core.totals([a, b])
        self.assertEqual(tot["energy_kcal"]["value"], 400)
        self.assertTrue(tot["energy_kcal"]["complete"])
        self.assertFalse(tot["fiber_g"]["complete"])
        self.assertEqual(tot["fiber_g"]["unknown_items"], 1)
        self.assertEqual(tot["fiber_g"]["value"], 0)
        empty = core.totals([b])
        self.assertIsNone(empty["protein_g"]["value"])


def entry(**over):
    base = {"eaten_at": "2026-10-25T12:30:00+01:00", "meal": "lunch",
            "source_events": ["telegram:42:1001"], "items": [item()]}
    base.update(over)
    return base


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nutrition" / "ledger.sqlite3"
        self.led = store.Ledger(self.path, BERLIN, now=lambda: NOW)
        self.addCleanup(self.led.close)

    def test_add_get_with_stable_id_timezone_and_private_file(self):
        res = self.led.add(entry())
        self.assertEqual(res["status"], "logged")
        e = res["entry"]
        self.assertRegex(e["id"], r"^m_[0-9a-f]{12}$")
        self.assertEqual(e["rev"], 1)
        self.assertEqual(e["local_date"], "2026-10-25")
        self.assertEqual(e["eaten_at"], "2026-10-25T12:30:00+01:00")
        self.assertEqual(e["created_at"], "2026-10-25T13:00:00+01:00")
        self.assertEqual(e["totals"]["energy_kcal"]["value"], 150)
        self.assertIn("150 kcal", e["receipt"])
        self.assertEqual(self.led.get(e["id"])["items"][0]["name"], "Synthetic yoghurt")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_naive_time_uses_configured_zone_and_default_is_now(self):
        e = self.led.add(entry(eaten_at="2026-10-24T23:30:00", source_events=[]))["entry"]
        self.assertEqual(e["eaten_at"], "2026-10-24T23:30:00+02:00")
        self.assertEqual(e["local_date"], "2026-10-24")
        e = self.led.add(entry(eaten_at=None, source_events=[]))["entry"]
        self.assertEqual(e["eaten_at"], NOW.isoformat())

    def test_future_or_bad_timestamps_are_rejected(self):
        for bad in ("2026-10-26T12:00:00+01:00", "yesterday", "2026-02-30T10:00:00", 5):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                self.led.add(entry(eaten_at=bad, source_events=[]))

    def test_same_source_event_is_idempotent(self):
        first = self.led.add(entry())
        again = self.led.add(entry())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["entry"]["id"], first["entry"]["id"])
        self.assertEqual(len(self.led.list_entries()), 1)

    def test_extra_photo_of_same_meal_attaches_instead_of_duplicating(self):
        first = self.led.add(entry())["entry"]
        res = self.led.add(entry(source_events=["telegram:42:1001", "telegram:42:1002"]))
        self.assertEqual(res["status"], "duplicate")
        self.assertEqual(sorted(self.led.get(first["id"])["source_events"]),
                         ["telegram:42:1001", "telegram:42:1002"])
        self.assertEqual(self.led.add(entry(source_events=["telegram:42:1002"]))["status"],
                         "duplicate")

    def test_events_from_two_different_meals_conflict(self):
        self.led.add(entry())
        self.led.add(entry(source_events=["telegram:42:2000"]))
        with self.assertRaises(store.Conflict):
            self.led.add(entry(source_events=["telegram:42:1001", "telegram:42:2000"]))

    def test_same_product_or_photo_hash_twice_is_two_meals(self):
        sha = "a" * 64
        it = item(barcode="4006381333931", provenance={"kind": "label", "image_sha256": sha})
        a = self.led.add(entry(items=[it], source_events=["telegram:42:1"]))
        b = self.led.add(entry(items=[it], source_events=["telegram:42:2"]))
        c = self.led.add(entry(items=[it], source_events=[]))
        self.assertEqual([a["status"], b["status"], c["status"]], ["logged"] * 3)
        self.assertEqual(len(self.led.list_entries()), 3)

    def test_multi_item_meal_is_atomic(self):
        bad = entry(items=[item(), item(name="broken", nutrients={"sugars_g": 9,
                                                                  "carbohydrate_g": 1})])
        with self.assertRaises(core.InvalidInput):
            self.led.add(bad)
        self.assertEqual(self.led.list_entries(), [])
        # the failed event id is not burned either
        self.assertEqual(self.led.add(entry())["status"], "logged")

    def test_incomplete_add_raises_missing_and_saves_nothing(self):
        with self.assertRaises(core.MissingInfo):
            self.led.add(entry(items=[item(amount=None)]))
        self.assertEqual(self.led.list_entries(), [])

    def test_entry_shape_is_validated(self):
        for bad in ({"items": []}, {"items": "x"}, {"items": [item()], "colour": 1},
                    {"items": [item()], "meal": "elevenses"},
                    {"items": [item()], "source_events": ["no colon"]},
                    {"items": [item()], "source_events": "telegram:1:2"}):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                self.led.add(bad)

    def test_day_totals_sum_entries_and_are_partial_by_default(self):
        self.led.add(entry())
        self.led.add(entry(source_events=["telegram:42:1003"], meal="snack",
                           items=[item(amount="100 g")]))
        self.led.add(entry(source_events=["telegram:42:1004"],
                           eaten_at="2026-10-24T09:00:00+02:00"))
        day = self.led.day("2026-10-25")
        self.assertEqual(day["totals"]["energy_kcal"]["value"], 210)
        self.assertEqual(len(day["entries"]), 2)
        self.assertEqual(day["status"], "partial")
        self.assertIn("not total intake", day["note"])



class ProposalTests(LedgerTests.__base__):
    setUp = LedgerTests.setUp

    def test_propose_keeps_gaps_and_lists_questions(self):
        res = self.led.propose(entry(items=[item(amount=None)]),
                               questions=["How much oil went in?"])
        self.assertEqual(res["status"], "pending")
        p = res["proposal"]
        self.assertRegex(p["id"], r"^p_[0-9a-f]{12}$")
        self.assertEqual(p["missing"][0]["field"], "amount")
        self.assertIn("How much oil went in?", p["questions"])
        self.assertEqual([x["id"] for x in self.led.pending()], [p["id"]])
        self.assertEqual(self.led.list_entries(), [])
        self.assertEqual(self.led.day("2026-10-25")["pending_proposals"], 1)

    def test_confirm_with_answer_creates_one_entry_and_claims_events(self):
        p = self.led.propose(entry(items=[item(amount=None)]))["proposal"]
        with self.assertRaises(core.MissingInfo):
            self.led.confirm(p["id"])
        res = self.led.confirm(p["id"], {"items": {"0": {"amount": "180 g"}}})
        self.assertEqual(res["status"], "logged")
        self.assertEqual(res["entry"]["totals"]["protein_g"]["value"], 18)
        self.assertEqual(self.led.pending(), [])
        again = self.led.add(entry())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["entry"]["id"], res["entry"]["id"])
        with self.assertRaises(store.Conflict):
            self.led.confirm(p["id"])

    def test_redelivered_message_returns_the_open_proposal(self):
        p = self.led.propose(entry(items=[item(amount=None)]))["proposal"]
        again = self.led.propose(entry(items=[item(amount=None)]))
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["proposal"]["id"], p["id"])
        self.assertEqual(self.led.add(entry())["status"], "duplicate")

    def test_cancel_closes_without_an_entry(self):
        p = self.led.propose(entry(items=[item(amount=None)]))["proposal"]
        self.led.cancel(p["id"])
        self.assertEqual(self.led.pending(), [])
        self.assertEqual(self.led.list_entries(), [])
        with self.assertRaises(store.NotFound):
            self.led.cancel("p_000000000000")

    def test_cancelled_proposal_redelivery_stays_dropped(self):
        p = self.led.propose(entry(items=[item(amount=None)]))["proposal"]
        self.led.cancel(p["id"])
        self.assertEqual(self.led.add(entry())["status"], "duplicate")
        self.assertEqual(self.led.list_entries(), [])

    def test_new_message_after_cancel_logs_a_fresh_meal(self):
        p = self.led.propose(entry(items=[item(amount=None)]))["proposal"]
        self.led.cancel(p["id"])
        res = self.led.add(entry(source_events=["telegram:42:1001", "telegram:42:1002"]))
        self.assertEqual(res["status"], "logged")
        again = self.led.add(entry())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["entry"]["id"], res["entry"]["id"])

    def test_invalid_data_is_refused_even_as_proposal(self):
        with self.assertRaises(core.InvalidInput):
            self.led.propose(entry(items=[item(amount="-3 g")]))


class RevisionTests(LedgerTests.__base__):
    setUp = LedgerTests.setUp

    def test_correction_is_a_new_revision_with_history(self):
        e = self.led.add(entry())["entry"]
        c = self.led.correct(e["id"], {"items": {"0": {"amount": "300 g"}}}, reason="bigger bowl")
        self.assertEqual(c["rev"], 2)
        self.assertEqual(c["totals"]["energy_kcal"]["value"], 180)
        h = self.led.get(e["id"], history=True)["history"]
        self.assertEqual([(r["rev"], r["action"]) for r in h], [(1, "create"), (2, "correct")])
        self.assertIn("150 kcal", h[0]["receipt"])
        self.assertEqual(h[1]["reason"], "bigger bowl")

    def test_correction_can_move_day_and_add_source_events(self):
        e = self.led.add(entry())["entry"]
        c = self.led.correct(e["id"], {"eaten_at": "2026-10-24T20:00:00+02:00",
                                       "source_events": ["telegram:42:1001", "telegram:42:1009"]})
        self.assertEqual(c["local_date"], "2026-10-24")
        self.assertEqual(self.led.day("2026-10-25")["entries"], [])
        self.assertEqual(self.led.add(entry(source_events=["telegram:42:1009"]))["status"],
                         "duplicate")

    def test_stale_correction_is_refused(self):
        e = self.led.add(entry())["entry"]
        self.led.correct(e["id"], {"meal": "dinner"})
        with self.assertRaises(store.Conflict):
            self.led.correct(e["id"], {"meal": "snack"}, expect_rev=1)

    def test_invalid_correction_changes_nothing(self):
        e = self.led.add(entry())["entry"]
        with self.assertRaises(core.InvalidInput):
            self.led.correct(e["id"], {"items": {"0": {"amount": "0 g"}}})
        with self.assertRaises(core.InvalidInput):
            self.led.correct(e["id"], {"items": {"5": {"amount": "10 g"}}})
        self.assertEqual(self.led.get(e["id"])["rev"], 1)

    def test_delete_is_a_tombstone_and_keeps_dedupe(self):
        e = self.led.add(entry())["entry"]
        d = self.led.delete(e["id"], reason="logged twice")
        self.assertEqual(d["state"], "deleted")
        self.assertEqual(self.led.list_entries(), [])
        self.assertEqual(len(self.led.list_entries(include_deleted=True)), 1)
        self.assertEqual(self.led.day("2026-10-25")["receipt"], "nothing logged")
        self.assertEqual(self.led.add(entry())["status"], "duplicate")
        with self.assertRaises(store.Conflict):
            self.led.correct(e["id"], {"meal": "snack"})
        with self.assertRaises(store.Conflict):
            self.led.delete(e["id"])

    def test_erase_blanks_every_revision(self):
        e = self.led.add(entry(note="secret sandwich"))["entry"]
        self.led.correct(e["id"], {"meal": "dinner"})
        self.led.delete(e["id"], erase=True)
        self.led.close()
        raw = self.path.read_bytes()
        self.assertNotIn(b"secret sandwich", raw)
        self.assertNotIn(b"Synthetic yoghurt", raw)
        led = store.Ledger(self.path, BERLIN, now=lambda: NOW)
        self.addCleanup(led.close)
        got = led.get(e["id"], history=True)
        self.assertEqual(got["state"], "erased")
        self.assertNotIn("items", got)
        self.assertEqual(led.add(entry())["status"], "duplicate")

    def test_unknown_ids(self):
        for fn in (lambda: self.led.get("m_ffffffffffff"),
                   lambda: self.led.correct("m_ffffffffffff", {"meal": "snack"}),
                   lambda: self.led.delete("m_ffffffffffff")):
            with self.assertRaises(store.NotFound):
                fn()


class DayCompletenessTests(LedgerTests.__base__):
    setUp = LedgerTests.setUp

    def test_explicit_complete_and_reopen(self):
        self.led.add(entry())
        self.led.mark_day("2026-10-25", "complete")
        self.assertEqual(self.led.day("2026-10-25")["status"], "complete")
        self.led.mark_day("2026-10-25", "partial", note="forgot dinner")
        self.assertEqual(self.led.day("2026-10-25")["status"], "partial")
        with self.assertRaises(core.InvalidInput):
            self.led.mark_day("2026-10-25", "done")
        with self.assertRaises(core.InvalidInput):
            self.led.mark_day("2026-10-26", "complete")

    def test_cannot_mark_complete_with_open_proposals(self):
        self.led.propose(entry(items=[item(amount=None)]))
        with self.assertRaises(store.Conflict):
            self.led.mark_day("2026-10-25", "complete")

    def test_averages_use_complete_days_only(self):
        self.led.add(entry(source_events=[], eaten_at="2026-10-23T12:00:00+02:00"))
        self.led.add(entry(source_events=[], eaten_at="2026-10-24T12:00:00+02:00",
                           items=[item(amount="500 g")]))
        self.led.add(entry(source_events=[], eaten_at="2026-10-25T12:00:00+01:00",
                           items=[item(amount="50 g")]))
        self.led.mark_day("2026-10-23", "complete")
        self.led.mark_day("2026-10-24", "complete")
        r = self.led.days("2026-10-20", "2026-10-25")
        self.assertEqual(r["complete_days"], 2)
        self.assertEqual(r["average_complete_days"]["energy_kcal"], 225)
        self.assertEqual(len(r["days"]), 3)
        none = self.led.days("2026-10-25", "2026-10-25")
        self.assertIsNone(none["average_complete_days"])
        with self.assertRaises(core.InvalidInput):
            self.led.days("2026-10-25", "2026-10-20")


class DryRunLedgerTests(unittest.TestCase):
    def test_dry_run_never_creates_or_changes_the_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nutrition" / "ledger.sqlite3"
            dry = store.Ledger(path, BERLIN, dry_run=True, now=lambda: NOW)
            res = dry.add(entry())
            self.assertEqual(res["status"], "logged")
            dry.close()
            self.assertFalse(path.parent.exists())
            real = store.Ledger(path, BERLIN, now=lambda: NOW)
            eid = real.add(entry())["entry"]["id"]
            real.close()
            before = path.read_bytes()
            dry = store.Ledger(path, BERLIN, dry_run=True, now=lambda: NOW)
            dry.correct(eid, {"meal": "snack"})
            dry.add(entry(source_events=["telegram:42:9"]))
            dry.propose(entry(source_events=["telegram:42:10"], items=[item(amount=None)]))
            dry.mark_day("2026-10-25", "complete")
            dry.delete(eid, erase=True)
            dry.close()
            self.assertEqual(path.read_bytes(), before)



def with_check_digit(body):
    """Test-side GS1 mod-10, independent of the code under test."""
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


class GtinTests(unittest.TestCase):
    def test_valid_codes_keep_leading_zeros(self):
        for code in ("4006381333931", "036000291452", "73513537", "0036000291452",
                     with_check_digit("0001234567890")):
            with self.subTest(code=code):
                self.assertEqual(core.gtin(code), code)
        self.assertEqual(core.gtin("4 006381 333931"), "4006381333931")

    def test_invalid_codes(self):
        for bad in ("4006381333932", "400638133393", "abc", "", 4006381333931,
                    "12345", "4006381333931\n../x", "٤٠٠٦٣٨١٣٣٣٩٣١"):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                core.gtin(bad)

    def test_in_store_codes_are_flagged(self):
        self.assertTrue(core.restricted_circulation(with_check_digit("201234500000")))
        self.assertFalse(core.restricted_circulation("4006381333931"))


CODE = with_check_digit("400000000001")  # synthetic, not a real product
LIQUID = with_check_digit("400000000002")


def off_body(code=CODE, **product):
    base = {"product_name": "Synthetic muesli", "product_name_de": "Synthetisches Müsli",
            "brands": "Testmarke, Other", "quantity": "500 g", "serving_size": "45 g",
            "serving_quantity": 45, "nutriments": {
                "energy-kcal_100g": 380, "energy-kj_100g": 1590, "proteins_100g": 10,
                "carbohydrates_100g": 60, "sugars_100g": 12, "fat_100g": 8,
                "salt_100g": 0.05}}
    base.update(product)
    return json.dumps({"code": code, "status": 1, "status_verbose": "product found",
                       "product": base}).encode()


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class OpenFoodFactsTests(unittest.TestCase):
    def provider(self, *responses, **kw):
        self.transport = FakeTransport(*responses)
        return fp.OpenFoodFacts(transport=self.transport, **kw)

    def test_request_is_official_read_only_api_with_user_agent_and_timeout(self):
        off = self.provider((200, {}, off_body()))
        off.lookup_barcode(CODE)
        url, headers, timeout = self.transport.calls[0]
        from urllib.parse import urlsplit, parse_qs
        u = urlsplit(url)
        self.assertEqual((u.scheme, u.netloc, u.path),
                         ("https", "world.openfoodfacts.org", f"/api/v2/product/{CODE}"))
        q = parse_qs(u.query)
        self.assertEqual((q["cc"], q["lc"]), (["de"], ["de"]))
        self.assertIn("nutriments", q["fields"][0])
        self.assertIn("ClaudeRunningCoach", headers["User-Agent"])
        self.assertTrue(0 < timeout <= 30)

    def test_country_language_and_contact_are_configurable(self):
        off = self.provider((200, {}, off_body()), country="fr", language="fr",
                            contact="ops@example.org")
        off.lookup_barcode(CODE)
        url, headers, _ = self.transport.calls[0]
        self.assertIn("cc=fr", url)
        self.assertIn("ops@example.org", headers["User-Agent"])
        for bad in ({"country": "de/../x"}, {"language": "x" * 9}, {"timeout": 0},
                    {"timeout": float("nan")}, {"timeout": 600}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fp.OpenFoodFacts(transport=FakeTransport(), **bad)

    def test_product_is_normalised_with_nulls_and_attribution(self):
        p = self.provider((200, {}, off_body())).lookup_barcode(CODE)
        self.assertEqual(p["name"], "Synthetisches Müsli")
        self.assertEqual(p["brand"], "Testmarke")
        self.assertEqual(p["basis"], "100g")
        self.assertEqual(p["serving"], {"value": 45.0, "unit": "g"})
        n = p["nutrients"]
        self.assertEqual(n["energy_kcal"], 380)
        self.assertIsNone(n["fiber_g"])
        self.assertIn("ODbL", p["attribution"])
        self.assertIn("DbCL", p["attribution"])

    def test_kj_only_and_sodium_only_are_converted(self):
        body = off_body(nutriments={"energy-kj_100g": 836.8, "sodium_100g": 0.4,
                                    "proteins_100g": 3})
        n = self.provider((200, {}, body)).lookup_barcode(CODE)["nutrients"]
        self.assertEqual(n["energy_kcal"], 200)
        self.assertEqual(n["salt_g"], 1)

    def test_liquids_are_per_100ml_and_unknown_basis_stays_unknown(self):
        p = self.provider((200, {}, off_body(LIQUID, quantity="0,5 l", serving_size=None,
                                             serving_quantity=None))).lookup_barcode(LIQUID)
        self.assertEqual(p["basis"], "100ml")
        self.assertIsNone(p["serving"])
        p = self.provider((200, {}, off_body(quantity="", serving_size=None,
                                             serving_quantity=None))).lookup_barcode(CODE)
        self.assertIsNone(p["basis"])

    def test_errors_never_fabricate_a_product(self):
        cases = [((404, {}, json.dumps({"status": 0, "status_verbose": "product not found"}).encode()),
                  "not_found"),
                 ((200, {}, json.dumps({"status": 0}).encode()), "not_found"),
                 ((429, {"Retry-After": "60"}, b""), "rate_limited"),
                 ((503, {}, b"down"), "unavailable"),
                 ((200, {}, b"<html>"), "bad_response"),
                 ((200, {}, json.dumps({"status": 1, "product": {"code": "1"}, "code": "1"}).encode()),
                  "bad_response"),
                 (TimeoutError("timed out"), "unavailable"),
                 (OSError("no route"), "unavailable")]
        for response, kind in cases:
            with self.subTest(kind=kind, response=str(response)[:40]):
                with self.assertRaises(fp.ProviderError) as cm:
                    self.provider(response).lookup_barcode(CODE)
                self.assertEqual(cm.exception.kind, kind)

    def test_invalid_codes_never_reach_the_network(self):
        off = self.provider()
        for bad in ("4006381333932", "../../admin", "4006381333931?x=1"):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                off.lookup_barcode(bad)
        self.assertEqual(self.transport.calls, [])

    def test_base_url_is_pinned_to_official_or_loopback(self):
        for bad in ("http://169.254.169.254", "https://evil.example", "http://world.openfoodfacts.org",
                    "https://world.openfoodfacts.org.evil.example", "file:///etc/passwd",
                    "https://user@world.openfoodfacts.org"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fp.OpenFoodFacts(base_url=bad)
        fp.OpenFoodFacts(base_url="http://127.0.0.1:8123")


class LoopbackServer:
    def __init__(self, routes):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                status, headers, body = routes(self.path)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.seen = []
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class UrllibTransportTests(unittest.TestCase):
    def serve(self, routes):
        srv = LoopbackServer(routes)
        self.addCleanup(srv.close)
        return srv

    def test_real_transport_against_loopback(self):
        srv = self.serve(lambda path: (200, {"Content-Type": "application/json"}, off_body()))
        p = fp.OpenFoodFacts(base_url=srv.url).lookup_barcode(CODE)
        self.assertEqual(p["nutrients"]["protein_g"], 10)
        self.assertIn("ClaudeRunningCoach", srv.seen[0][1]["User-Agent"])

    def test_redirects_are_not_followed(self):
        # The target is live and would answer with a valid product, so only
        # a refused redirect makes this lookup fail.
        target = self.serve(lambda path: (200, {}, off_body()))
        srv = self.serve(lambda path: (302, {"Location": target.url + path}, b""))
        with self.assertRaises(fp.ProviderError) as cm:
            fp.OpenFoodFacts(base_url=srv.url).lookup_barcode(CODE)
        self.assertEqual(cm.exception.kind, "unavailable")
        self.assertEqual(len(srv.seen), 1)
        self.assertEqual(target.seen, [])

    def test_oversized_body_is_refused(self):
        srv = self.serve(lambda path: (200, {}, b"x" * (fp.MAX_BODY + 10)))
        with self.assertRaises(fp.ProviderError) as cm:
            fp.OpenFoodFacts(base_url=srv.url).lookup_barcode(CODE)
        self.assertEqual(cm.exception.kind, "bad_response")


class ProviderCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "nutrition" / "provider_cache.sqlite3"
        self.clock = [1_800_000_000.0]

    def cache(self, *responses, dry_run=False):
        self.transport = FakeTransport(*responses)
        off = fp.OpenFoodFacts(transport=self.transport)
        c = fp.ProviderCache(self.path, off, dry_run=dry_run, clock=lambda: self.clock[0])
        self.addCleanup(c.close)
        return c

    def test_found_products_are_cached_and_refreshable(self):
        c = self.cache((200, {}, off_body()), (200, {}, off_body(product_name="v2",
                                                                 product_name_de=None)))
        first = c.lookup(CODE)
        self.assertFalse(first["cached"])
        self.assertTrue(c.lookup(CODE)["cached"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(c.lookup(CODE, refresh=True)["name"], "v2")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_not_found_is_cached_briefly(self):
        nf = (404, {}, json.dumps({"status": 0}).encode())
        c = self.cache(nf, nf)
        for _ in range(2):
            with self.assertRaises(fp.ProviderError):
                c.lookup(CODE)
        self.assertEqual(len(self.transport.calls), 1)
        self.clock[0] += 2 * 86400
        with self.assertRaises(fp.ProviderError):
            c.lookup(CODE)
        self.assertEqual(len(self.transport.calls), 2)

    def test_transient_errors_are_not_cached(self):
        c = self.cache((503, {}, b""), (200, {}, off_body()))
        with self.assertRaises(fp.ProviderError):
            c.lookup(CODE)
        self.assertEqual(c.lookup(CODE)["name"], "Synthetisches Müsli")

    def test_offline_without_cache_fails_honestly(self):
        c = self.cache()
        with self.assertRaises(fp.ProviderError) as cm:
            c.lookup(CODE, offline=True)
        self.assertEqual(cm.exception.kind, "offline")

    def test_local_rate_limit(self):
        codes = [with_check_digit(f"4000000001{i:02d}") for i in range(fp.MAX_PER_MINUTE + 1)]
        c = self.cache(*[(200, {}, off_body(code)) for code in codes])
        for code in codes[:-1]:
            c.lookup(code)
        with self.assertRaises(fp.ProviderError) as cm:
            c.lookup(codes[-1])
        self.assertEqual(cm.exception.kind, "rate_limited")
        self.clock[0] += 61
        c.lookup(codes[-1])

    def test_dry_run_reads_but_never_writes_the_cache(self):
        c = self.cache((200, {}, off_body()), dry_run=True)
        self.assertEqual(c.lookup(CODE)["name"], "Synthetisches Müsli")
        c.close()
        self.assertFalse(self.path.exists())


class ResolveBarcodeItemTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def lookup(self, product):
        def fn(code):
            self.calls.append(code)
            if isinstance(product, Exception):
                raise product
            return product
        return fn

    def product(self, **over):
        transport = FakeTransport((200, {}, off_body(**over)))
        p = fp.OpenFoodFacts(transport=transport).lookup_barcode(CODE)
        p["fetched_at"] = "2026-10-25T12:00:00+01:00"
        return p

    def test_barcode_only_item_is_filled_from_provider(self):
        it = fp.resolve_item({"barcode": CODE, "amount": "60 g"}, self.lookup(self.product()))
        out = core.compute_item(it)
        self.assertEqual(out["nutrients"]["energy_kcal"], 228)
        self.assertIsNone(out["nutrients"]["fiber_g"])
        self.assertEqual(out["provenance"]["kind"], "provider")
        self.assertEqual(out["provenance"]["product_code"], CODE)
        self.assertIn("ODbL", out["provenance"]["attribution"])

    def test_servings_use_provider_serving_size(self):
        it = fp.resolve_item({"barcode": CODE, "amount": "2 serving"}, self.lookup(self.product()))
        self.assertEqual(core.compute_item(it)["amount_g"], 90)

    def test_missing_serving_size_or_amount_stays_missing(self):
        p = self.product(serving_size=None, serving_quantity=None)
        it = fp.resolve_item({"barcode": CODE, "amount": "1 serving"}, self.lookup(p))
        with self.assertRaises(core.MissingInfo):
            core.compute_item(it)
        it = fp.resolve_item({"barcode": CODE}, self.lookup(p))
        with self.assertRaises(core.MissingInfo):
            core.compute_item(it)

    def test_not_found_or_unknown_basis_is_missing_not_zero(self):
        with self.assertRaises(core.MissingInfo):
            fp.resolve_item({"barcode": CODE, "amount": "50 g"},
                            self.lookup(fp.ProviderError("not_found", "not in OFF")))
        p = self.product(quantity="", serving_size=None, serving_quantity=None)
        with self.assertRaises(core.MissingInfo):
            fp.resolve_item({"barcode": CODE, "amount": "50 g"}, self.lookup(p))
        p = self.product(nutriments={})
        with self.assertRaises(core.MissingInfo):
            fp.resolve_item({"barcode": CODE, "amount": "50 g"}, self.lookup(p))

    def test_outage_is_an_error_not_a_question(self):
        with self.assertRaises(fp.ProviderError):
            fp.resolve_item({"barcode": CODE, "amount": "50 g"},
                            self.lookup(fp.ProviderError("unavailable", "HTTP 503")))

    def test_label_values_win_and_skip_the_lookup(self):
        it = {"barcode": CODE, "name": "From label", "amount": "50 g", "basis": "100g",
              "nutrients": {"energy_kcal": 400}, "provenance": {"kind": "label"}}
        self.assertIs(fp.resolve_item(it, self.lookup(self.product())), it)
        self.assertEqual(self.calls, [])



JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-bytes" * 10
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-png" * 10


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.led = store.Ledger(self.root / "nutrition" / "ledger.sqlite3", BERLIN,
                                now=lambda: NOW)
        self.addCleanup(self.led.close)
        self.channel = self.root / "channel-inbox"
        self.channel.mkdir()
        self.photo = self.channel / "1700000000-abc.jpg"
        self.photo.write_bytes(JPEG)

    def test_stage_makes_a_private_copy_and_leaves_the_original(self):
        img = self.led.stage_image(str(self.photo), source_event="telegram:42:1001")
        self.assertRegex(img["token"], r"^i_[0-9a-f]{16}$")
        staged = Path(img["path"])
        self.assertEqual(staged.parent, self.root / "nutrition" / "inbox")
        self.assertEqual(staged.read_bytes(), JPEG)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual(img["sha256"], hashlib.sha256(JPEG).hexdigest())
        self.assertEqual(img["format"], "jpeg")
        self.assertTrue(self.photo.exists())

    def test_stage_refuses_non_images_links_and_oversize(self):
        secret = self.root / "notes.txt"
        secret.write_text("not an image")
        link = self.root / "link.jpg"
        link.symlink_to(self.photo)
        empty = self.root / "empty.jpg"
        empty.write_bytes(b"")
        for bad in (secret, link, empty, self.root, self.root / "missing.jpg"):
            with self.subTest(bad=bad), self.assertRaises(core.InvalidInput):
                self.led.stage_image(str(bad))
        with patch.object(store, "MAX_IMAGE_BYTES", 10), self.assertRaises(core.InvalidInput):
            self.led.stage_image(str(self.photo))
        self.assertFalse(list((self.root / "nutrition" / "inbox").glob("*"))
                         if (self.root / "nutrition" / "inbox").exists() else [])

    def test_discard_deletes_only_the_feature_copy(self):
        img = self.led.stage_image(str(self.photo))
        res = self.led.discard_image(img["token"])
        self.assertEqual(res["status"], "discarded")
        self.assertFalse(Path(img["path"]).exists())
        self.assertTrue(self.photo.exists())
        self.assertEqual(self.led.discard_image(img["token"])["status"], "already discarded")

    def test_discard_never_takes_a_path_or_follows_links(self):
        for bad in ("../../channel-inbox/1700000000-abc", str(self.photo), "i_zz", "i_" + "0" * 16):
            with self.subTest(bad=bad), self.assertRaises((core.InvalidInput, store.NotFound)):
                self.led.discard_image(bad)
        img = self.led.stage_image(str(self.photo))
        staged = Path(img["path"])
        staged.unlink()
        staged.symlink_to(self.photo)
        with self.assertRaises(store.Conflict):
            self.led.discard_image(img["token"])
        self.assertEqual(self.photo.read_bytes(), JPEG)

    def test_add_records_hash_and_discards_copy_after_commit(self):
        img = self.led.stage_image(str(self.photo))
        e = self.led.add(entry(images=[img["token"]]))["entry"]
        self.assertEqual(e["image_sha256"], [img["sha256"]])
        self.assertFalse(Path(img["path"]).exists())
        self.assertTrue(self.photo.exists())
        with self.assertRaises(store.Conflict):
            self.led.add(entry(source_events=["telegram:42:77"], images=[img["token"]]))

    def test_failed_add_keeps_the_copy_for_a_retry(self):
        img = self.led.stage_image(str(self.photo))
        with self.assertRaises(core.MissingInfo):
            self.led.add(entry(images=[img["token"]], items=[item(amount=None)]))
        self.assertTrue(Path(img["path"]).exists())

    def test_proposal_keeps_copy_until_confirm_or_cancel(self):
        a = self.led.stage_image(str(self.photo))
        p = self.led.propose(entry(images=[a["token"]], items=[item(amount=None)]))["proposal"]
        self.assertTrue(Path(a["path"]).exists())
        self.assertEqual(self.led.purge_images(older_than_hours=0)["discarded"], [])
        self.led.confirm(p["id"], {"items": {"0": {"amount": "100 g"}}})
        self.assertFalse(Path(a["path"]).exists())
        b = self.led.stage_image(str(self.photo))
        p = self.led.propose(entry(source_events=["telegram:42:5"], images=[b["token"]],
                                   items=[item(amount=None)]))["proposal"]
        self.led.cancel(p["id"])
        self.assertFalse(Path(b["path"]).exists())

    def test_purge_discards_stale_unlinked_copies(self):
        img = self.led.stage_image(str(self.photo))
        self.assertEqual(self.led.purge_images(older_than_hours=24)["discarded"], [])
        self.assertEqual(self.led.purge_images(older_than_hours=0)["discarded"], [img["token"]])
        self.assertFalse(Path(img["path"]).exists())

    def test_dry_run_neither_copies_nor_deletes(self):
        dry = store.Ledger(self.root / "nutrition" / "ledger.sqlite3", BERLIN, dry_run=True,
                           now=lambda: NOW)
        self.addCleanup(dry.close)
        img = dry.stage_image(str(self.photo))
        self.assertIsNone(img["token"])
        self.assertEqual(img["path"], str(self.photo))
        self.assertFalse((self.root / "nutrition").exists())

    def test_scan_is_optional_and_validates_checksums(self):
        img = self.led.stage_image(str(self.photo))
        with patch.dict(os.environ, {"PATH": str(self.root / "nobin")}):
            res = self.led.scan_image(img["token"])
        self.assertFalse(res["available"])
        bindir = self.root / "bin"
        bindir.mkdir()
        fake = bindir / "zbarimg"
        fake.write_text("#!/bin/sh\nprintf '4006381333931\\n4006381333932\\nhttp://x\\n'\n")
        fake.chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{bindir}:/usr/bin:/bin"}):
            res = self.led.scan_image(img["token"])
        self.assertTrue(res["available"])
        self.assertEqual(res["codes"], ["4006381333931"])
        self.assertEqual(len(res["rejected"]), 2)



CONFIG = """[athlete]
name = "Sam"
language = "English"
timezone = "Europe/Berlin"
[integrations]
nutrition_log = {enabled}
[nutrition]
country = "de"
language = "de"
"""


class CliBase(unittest.TestCase):
    """nutrition.py end to end in a disposable COACH_HOME and HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "athlete").mkdir()
        self.config(True)
        self.state = self.home / "athlete" / "state" / "nutrition"
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home / "user"),
                    "COACH_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "LANG": "C.UTF-8"}

    def config(self, enabled):
        (self.home / "athlete" / "config.toml").write_text(
            CONFIG.format(enabled="true" if enabled else "false"))

    def run_cli(self, *args, env=None, stdin=None):
        return subprocess.run([sys.executable, str(SCRIPTS / "nutrition.py"), *args],
                              capture_output=True, text=True, input=stdin, timeout=30,
                              env={**self.env, **(env or {})})

    def js(self, *args, **kw):
        r = self.run_cli("--json", *args, **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def e(self, **over):
        over.setdefault("eaten_at", "2026-10-25T08:00:00+01:00")
        return json.dumps(entry(**over))

    def ledger_mode(self):
        return (self.state / "ledger.sqlite3").stat().st_mode & 0o777


class CliTests(CliBase):
    def test_disabled_by_default_and_refuses_mutations_without_state(self):
        self.config(False)
        r = self.run_cli("add", self.e())
        self.assertEqual(r.returncode, 4)
        self.assertIn("nutrition_log", r.stderr)
        for args in (["propose", self.e()], ["lookup", CODE], ["mark", "2026-10-25", "complete"],
                     ["image", "stage", str(self.home / "x.jpg")]):
            self.assertEqual(self.run_cli(*args).returncode, 4, args)
        status = self.js("status")
        self.assertFalse(status["enabled"])
        self.assertEqual(self.run_cli("day", "2026-10-25").returncode, 0)
        self.assertFalse((self.home / "athlete" / "state").exists())

    def test_template_and_defaults_keep_it_off(self):
        import coachconf as cc
        self.assertFalse(cc.DEFAULTS["integrations"]["nutrition_log"])
        self.assertEqual(cc.DEFAULTS["nutrition"]["country"], "de")
        import tomllib
        tpl = tomllib.loads((ROOT / "skill/templates/config.toml").read_text())
        self.assertIs(tpl["integrations"]["nutrition_log"], False)
        self.assertEqual(set(tpl["nutrition"]), set(cc.DEFAULTS["nutrition"]))

    def test_add_day_get_and_text_receipt(self):
        r = self.run_cli("add", self.e())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"logged m_[0-9a-f]{12}")
        self.assertIn("150 kcal", r.stdout)
        day = self.js("day", "2026-10-25")
        self.assertEqual(day["status"], "partial")
        self.assertEqual(day["totals"]["protein_g"]["value"], 25)
        text = self.run_cli("day", "2026-10-25").stdout
        self.assertIn("not total intake", text)
        eid = day["entries"][0]["id"]
        self.assertEqual(self.js("get", eid)["id"], eid)
        self.assertEqual(self.ledger_mode(), 0o600)

    def test_entry_from_file_and_stdin(self):
        f = self.home / "meal.json"
        f.write_text(self.e())
        self.assertEqual(self.js("add", f"@{f}")["status"], "logged")
        self.assertEqual(self.js("add", "-", stdin=self.e(source_events=["telegram:42:2"]))
                         ["status"], "logged")

    def test_exit_codes_for_invalid_missing_and_unknown(self):
        bad = self.run_cli("add", self.e(items=[item(amount="NaN g")]))
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(self.run_cli("add", "{not json").returncode, 2)
        missing = self.run_cli("add", self.e(items=[item(amount=None)]))
        self.assertEqual(missing.returncode, 3)
        self.assertIn("How much", missing.stdout + missing.stderr)
        self.assertEqual(self.run_cli("get", "m_ffffffffffff").returncode, 5)
        self.assertEqual(self.js("list"), [])

    def test_or_propose_parks_and_confirm_logs(self):
        res = self.js("add", "--or-propose", self.e(items=[item(amount=None)]))
        self.assertEqual(res["status"], "pending")
        pid = res["proposal"]["id"]
        self.assertEqual([p["id"] for p in self.js("pending")], [pid])
        done = self.js("confirm", pid, json.dumps({"items": {"0": {"amount": "200 g"}}}))
        self.assertEqual(done["entry"]["totals"]["energy_kcal"]["value"], 120)
        self.assertEqual(self.js("pending"), [])

    def test_correct_delete_and_mark(self):
        eid = self.js("add", self.e())["entry"]["id"]
        c = self.js("correct", eid, json.dumps({"meal": "breakfast"}), "--reason", "typo",
                    "--expect-rev", "1")
        self.assertEqual((c["rev"], c["meal"]), (2, "breakfast"))
        self.assertEqual(self.run_cli("correct", eid, "{}", "--expect-rev", "1").returncode, 2)
        self.assertEqual(self.run_cli("correct", eid, json.dumps({"meal": "snack"}),
                                      "--expect-rev", "1").returncode, 5)
        self.assertEqual(self.js("mark", "2026-10-25", "complete")["status"], "complete")
        self.assertEqual(self.js("delete", eid, "--reason", "test")["state"], "deleted")
        self.assertEqual(self.js("days", "--from", "2026-10-20", "--to", "2026-10-25")
                         ["complete_days"], 1)

    def test_dry_run_writes_nothing_anywhere(self):
        dry = {"COACH_DRY_RUN": "1"}
        r = self.run_cli("add", self.e(), env=dry)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run", r.stdout)
        self.assertFalse((self.home / "athlete" / "state").exists())
        eid = self.js("add", self.e())["entry"]["id"]
        snapshot = {p: p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        photo = self.home / "p.jpg"
        photo.write_bytes(JPEG)
        for args in (["add", self.e(source_events=["telegram:42:3"])],
                     ["propose", self.e(source_events=["telegram:42:4"])],
                     ["correct", eid, json.dumps({"meal": "snack"})],
                     ["delete", eid, "--erase"], ["mark", "2026-10-25", "complete"],
                     ["image", "stage", str(photo)], ["image", "purge", "--older-than-hours", "0"]):
            r = self.run_cli(*args, env=dry)
            self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
            self.assertIn("dry run", r.stdout, args)
        after = {p: p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        self.assertEqual(after, snapshot)
        self.assertTrue(photo.exists())

    def test_image_commands_never_accept_paths_for_deletion(self):
        photo = self.home / "channel" / "photo.jpg"
        photo.parent.mkdir()
        photo.write_bytes(JPEG)
        img = self.js("image", "stage", str(photo), "--source-event", "telegram:42:1001")
        self.assertTrue(Path(img["path"]).is_relative_to(self.state / "inbox"))
        self.assertEqual(self.run_cli("image", "discard", str(photo)).returncode, 2)
        self.assertEqual(self.run_cli("image", "discard", img["path"]).returncode, 2)
        self.assertTrue(photo.exists())
        res = self.js("add", self.e(images=[img["token"]]))
        self.assertEqual(res["entry"]["image_sha256"], [img["sha256"]])
        self.assertFalse(Path(img["path"]).exists())
        self.assertTrue(photo.exists())

    def test_bad_provider_config_never_blocks_removing_data(self):
        eid = self.js("add", self.e())["entry"]["id"]
        (self.home / "athlete" / "config.toml").write_text(
            CONFIG.format(enabled="true").replace('country = "de"', 'country = "de/../x"'))
        self.assertEqual(self.run_cli("lookup", CODE).returncode, 2)
        self.assertEqual(self.js("delete", eid)["state"], "deleted")

    def test_dry_run_purge_reports_nothing_deleted(self):
        photo = self.home / "p.jpg"
        photo.write_bytes(JPEG)
        img = self.js("image", "stage", str(photo))
        res = self.js("image", "purge", "--older-than-hours", "0", env={"COACH_DRY_RUN": "1"})
        self.assertEqual(res["discarded"], [])
        self.assertEqual(res["would_discard"], [img["token"]])
        self.assertTrue(Path(img["path"]).exists())

    def test_status_and_schema(self):
        st = self.js("status")
        self.assertTrue(st["enabled"])
        self.assertEqual((st["country"], st["language"]), ("de", "de"))
        self.assertIn("ODbL", st["providers"]["openfoodfacts"])
        schema = self.js("schema")
        self.assertEqual(schema["$id"], "nutrition.entry.v1")


class CliProviderTests(CliBase):
    """Barcode-only items through the real CLI, against a loopback OFF."""

    def setUp(self):
        super().setUp()
        self.products = {CODE: (200, {}, off_body())}
        self.srv = LoopbackServer(self.route)
        self.addCleanup(self.srv.close)
        self.env["COACH_NUTRITION_OFF_URL"] = self.srv.url

    def route(self, path):
        code = path.split("/api/v2/product/")[1].split("?")[0]
        return self.products.get(code, (404, {}, json.dumps({"status": 0}).encode()))

    def barcode_entry(self, events, amount="60 g", code=CODE):
        return self.e(source_events=events, items=[{"barcode": code, "amount": amount}])

    def test_barcode_item_logs_with_attribution_and_cache(self):
        res = self.js("add", self.barcode_entry(["telegram:42:10"]))
        it = res["entry"]["items"][0]
        self.assertEqual(it["nutrients"]["energy_kcal"], 228)
        self.assertEqual(it["provenance"]["provider"], "openfoodfacts")
        self.assertIn("ODbL", it["provenance"]["attribution"])
        again = self.js("add", self.barcode_entry(["telegram:42:11"]))
        self.assertEqual(again["status"], "logged")
        self.assertNotEqual(again["entry"]["id"], res["entry"]["id"])
        self.assertEqual(len(self.srv.seen), 1)
        self.assertTrue((self.state / "provider_cache.sqlite3").exists())
        self.assertEqual(len(self.js("list")), 2)

    def test_lookup_prints_product_and_attribution(self):
        out = self.run_cli("lookup", CODE)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Synthetisches Müsli", out.stdout)
        self.assertIn("ODbL", out.stdout)
        self.assertEqual(self.run_cli("lookup", "4006381333932").returncode, 2)

    def test_unknown_barcode_asks_for_label_and_outage_is_no_fallback(self):
        missing = self.run_cli("add", self.barcode_entry(["telegram:42:12"], code=LIQUID))
        self.assertEqual(missing.returncode, 3)
        self.assertIn("label", missing.stdout + missing.stderr)
        parked = self.js("add", "--or-propose", self.barcode_entry(["telegram:42:12"], code=LIQUID))
        self.assertEqual(parked["status"], "pending")
        self.products[CODE] = (503, {}, b"")
        down = self.run_cli("add", self.barcode_entry(["telegram:42:13"]))
        self.assertEqual(down.returncode, 6)
        self.assertEqual(self.js("list"), [])

    def test_missing_serving_size_stays_pending(self):
        self.products[CODE] = (200, {}, off_body(serving_size=None, serving_quantity=None))
        r = self.run_cli("add", self.barcode_entry(["telegram:42:14"], amount="1 serving"))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.js("list"), [])

    def test_off_url_override_cannot_leave_loopback(self):
        r = self.run_cli("lookup", CODE, env={"COACH_NUTRITION_OFF_URL": "http://169.254.169.254"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.srv.seen, [])



DOC = ROOT / "docs" / "nutrition-tracking.md"
SKILL_REF = ROOT / "skill" / "references" / "nutrition-logging.md"


def json_blocks(path):
    import re
    return [json.loads(b) for b in re.findall(r"```json\n(.*?)```", path.read_text(), re.S)]


class DocsAndSchemaTests(CliBase):
    def test_documented_entry_examples_run(self):
        entries = [b for b in json_blocks(DOC) + json_blocks(SKILL_REF)
                   if isinstance(b, dict) and isinstance(b.get("items"), list)]
        self.assertGreaterEqual(len(entries), 4)
        for i, ex in enumerate(entries):
            ex = {k: v for k, v in ex.items() if k != "images"}
            ex["source_events"] = [f"doc:example{i}"]
            ex.setdefault("eaten_at", "2026-10-25T08:00:00+01:00")
            with self.subTest(example=i):
                r = self.run_cli("--json", "add", "--or-propose", json.dumps(ex),
                                 env={"COACH_NUTRITION_OFF_URL": "http://127.0.0.1:9"})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(json.loads(r.stdout)["status"], ("logged", "pending"))

    def test_schema_matches_the_code(self):
        import nutrition
        props = nutrition.ENTRY_SCHEMA["properties"]
        self.assertEqual(set(props), store.ENTRY_KEYS | {"images"})
        item_props = props["items"]["items"]["properties"]
        self.assertEqual(set(item_props), core.ITEM_KEYS)
        self.assertEqual(set(item_props["nutrients"]["properties"]),
                         set(core.NUTRIENTS + core.INPUT_ONLY))
        self.assertEqual(item_props["provenance"]["properties"]["kind"]["enum"],
                         list(core.PROVENANCE_KINDS))
        self.assertEqual(set(item_props["provenance"]["properties"]), core.PROVENANCE_KEYS)
        self.assertEqual(props["meal"]["enum"], list(store.MEALS))

    def test_documented_commands_exist(self):
        import re
        import nutrition
        sub = nutrition.parser()._subparsers._group_actions[0].choices
        texts = [DOC.read_text(), SKILL_REF.read_text(), (ROOT / "skill/SKILL.md").read_text()]
        used = {m for t in texts for m in re.findall(r"(?<![\w])nutrition\.py (?:--json )?([a-z-]+)", t)}
        self.assertTrue(used)
        self.assertEqual(used - set(sub), set())

    def test_skill_routes_and_gates_the_feature(self):
        skill = (ROOT / "skill/SKILL.md").read_text()
        self.assertIn("references/nutrition-logging.md", skill)
        self.assertIn("nutrition_log", skill)
        ref = SKILL_REF.read_text()
        for needed in ("image_path", "attachment_file_id", "download_attachment",
                       "media_group_id", "image stage", "source_events", "--or-propose"):
            self.assertIn(needed, ref, needed)
        doc = DOC.read_text()
        for needed in ("ODbL", "DbCL", "COACH_DRY_RUN", "partial", "--erase", "Yazio", "BLS"):
            self.assertIn(needed, doc, needed)


if __name__ == "__main__":
    unittest.main()
