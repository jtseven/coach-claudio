"""Offline regressions for independently reproduced nutrition review blockers."""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill" / "scripts"))
import food_providers as fp
import nutrition_core as core
import nutrition_store as store

BERLIN = ZoneInfo("Europe/Berlin")
A, B = "4006381333931", "3017620422003"


def label(**changes):
    raw = {"name": "Synthetic food", "amount": "100 g", "basis": "100g",
           "nutrients": {"energy_kcal": 100}, "provenance": {"kind": "label"}}
    raw.update(changes)
    return raw


class LedgerReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = store.Ledger(Path(self.tmp.name) / "ledger.db", BERLIN,
                                  now=lambda: datetime(2026, 10, 25, 13, tzinfo=BERLIN))
        self.addCleanup(self.ledger.close)



class IdentityReviewTests(LedgerReviewCase):
    def test_provider_identity_change_requires_replacing_snapshot_atomically(self):
        calls = []
        def lookup(code):
            calls.append(code)
            return {"provider": "openfoodfacts", "name": "Synthetic " + code,
                    "basis": "100g", "nutrients": {"energy_kcal": 100 if code == A else 500},
                    "attribution": "Synthetic fixture"}
        self.ledger.resolver = lambda raw: fp.resolve_item(raw, lookup)
        entry = self.ledger.add({"items": [{"barcode": A, "amount": "100 g"}]})["entry"]
        with self.assertRaisesRegex(core.InvalidInput, "replace.*snapshot"):
            self.ledger.correct(entry["id"], {"items": {"0": {"barcode": B}}})
        unchanged = self.ledger.get(entry["id"])
        self.assertEqual(unchanged["rev"], 1)
        self.assertEqual(unchanged["totals"]["energy_kcal"]["value"], 100)
        changed = self.ledger.correct(entry["id"], {"items": [{"barcode": B, "amount": "100 g"}]})
        self.assertEqual(changed["totals"]["energy_kcal"]["value"], 500)
        self.assertEqual(changed["items"][0]["provenance"]["product_code"], B)
        self.assertEqual(calls, [A, B])

    def test_bls_identity_change_cannot_keep_previous_provider_snapshot(self):
        def lookup(code):
            return {"name": "Synthetic BLS", "source_version": "fixture", "attribution": "fixture",
                    "automatic_use": {"status": "ok"},
                    "nutrients": {"energy_kcal": {"qualifier": "exact", "value": 100}}}
        self.ledger.resolver = lambda raw: fp.resolve_bls_item(raw, lookup)
        entry = self.ledger.add({"items": [{"bls_code": "A123456", "amount": "100 g"}]})["entry"]
        with self.assertRaisesRegex(core.InvalidInput, "replace.*snapshot"):
            self.ledger.correct(entry["id"], {"items": {"0": {"bls_code": "B123456"}}})
        self.assertEqual(self.ledger.get(entry["id"])["rev"], 1)

    def test_identity_changes_preserve_deliberate_label_or_user_values(self):
        for kind in ("label", "user"):
            with self.subTest(kind=kind):
                entry = self.ledger.add({"items": [label(barcode=A, provenance={"kind": kind})]})["entry"]
                changed = self.ledger.correct(entry["id"], {"items": {"0": {"barcode": B}}})
                self.assertEqual(changed["totals"]["energy_kcal"]["value"], 100)
                self.assertEqual(changed["items"][0]["provenance"]["kind"], kind)


class LedgerDateReviewTests(LedgerReviewCase):
    def test_pending_timestamp_edit_moves_completeness_gate(self):
        proposal = self.ledger.propose({"eaten_at": "2026-10-24T12:00:00+00:00",
                                        "items": [label()]})["proposal"]
        with self.assertRaises(store.Conflict):
            self.ledger.mark_day("2026-10-24", "complete")
        moved = self.ledger.update_proposal(proposal["id"],
            {"eaten_at": "2026-10-24T22:30:00+00:00"})["proposal"]
        self.assertEqual(moved["local_date"], "2026-10-25")
        self.assertEqual(self.ledger.day("2026-10-24")["pending_proposals"], 0)
        self.assertEqual(self.ledger.day("2026-10-25")["pending_proposals"], 1)
        self.ledger.mark_day("2026-10-24", "complete")
        with self.assertRaises(store.Conflict):
            self.ledger.mark_day("2026-10-25", "complete")
        self.ledger.confirm(proposal["id"])
        self.ledger.mark_day("2026-10-25", "complete")

    def test_cross_midnight_add_confirm_correct_use_ledger_timezone(self):
        raw = {"eaten_at": "2026-10-24T22:30:00+00:00", "items": [label()]}
        entry = self.ledger.add(raw)["entry"]
        proposal = self.ledger.propose(raw)["proposal"]
        confirmed = self.ledger.confirm(proposal["id"])["entry"]
        corrected = self.ledger.correct(entry["id"], {"eaten_at": "2026-10-23T22:30:00+00:00"})
        self.assertEqual(entry["local_date"], "2026-10-25")
        self.assertEqual(proposal["local_date"], "2026-10-25")
        self.assertEqual(confirmed["local_date"], "2026-10-25")
        self.assertEqual(corrected["local_date"], "2026-10-24")
        self.assertEqual(len(self.ledger.day("2026-10-25")["entries"]), 1)
        self.assertEqual(len(self.ledger.day("2026-10-24")["entries"]), 1)


class DedupReviewTests(LedgerReviewCase):
    def test_proposal_cannot_claim_foreign_event_during_update_or_confirmation(self):
        foreign = self.ledger.add({"items": [label()], "source_events": ["test:foreign"]})["entry"]
        proposal = self.ledger.propose({"items": [label()], "source_events": ["test:pending"]})["proposal"]
        for operation in (self.ledger.update_proposal, self.ledger.confirm):
            with self.subTest(operation=operation.__name__), self.assertRaises(store.Conflict):
                operation(proposal["id"], {"source_events": ["test:foreign"]})
            self.assertEqual(self.ledger.get_proposal(proposal["id"])["state"], "pending")
            self.assertEqual(self.ledger.get(foreign["id"])["source_events"], ["test:foreign"])
        confirmed = self.ledger.confirm(proposal["id"])["entry"]
        self.assertEqual(confirmed["source_events"], ["test:pending"])

    def test_proposal_duplicate_events_survive_update_confirm_and_redelivery(self):
        proposal = self.ledger.propose({"items": [label()], "source_events": ["test:1"]})["proposal"]
        repeated = {"items": [label()], "source_events": ["test:1", "test:2"]}
        self.assertEqual(self.ledger.add(repeated)["status"], "duplicate")
        updated = self.ledger.update_proposal(proposal["id"],
            {"source_events": ["test:2"], "note": "quantity clarified"})["proposal"]
        self.assertEqual(updated["input"]["source_events"], ["test:1", "test:2"])
        # A later duplicate can attach another event without updating the payload.
        repeated["source_events"] = ["test:2", "test:3"]
        self.assertEqual(self.ledger.add(repeated)["status"], "duplicate")
        confirmed = self.ledger.confirm(proposal["id"])["entry"]
        self.assertEqual(confirmed["source_events"], ["test:1", "test:2", "test:3"])
        for event in ("test:1", "test:2", "test:3"):
            result = self.ledger.add({"items": [label()], "source_events": [event]})
            self.assertEqual(result["status"], "duplicate")
        self.assertEqual(len(self.ledger.day("2026-10-25")["entries"]), 1)

    def test_duplicate_attached_event_is_owned_not_reinserted_on_correction(self):
        entry = self.ledger.add({"items": [label()], "source_events": ["test:1"]})["entry"]
        duplicate = self.ledger.add({"items": [label()], "source_events": ["test:1", "test:2"]})
        self.assertEqual(duplicate["status"], "duplicate")
        changed = self.ledger.correct(entry["id"], {"source_events": ["test:2"], "note": "corrected"})
        self.assertEqual(changed["source_events"], ["test:1", "test:2"])
        self.assertEqual(changed["note"], "corrected")
        other = self.ledger.add({"items": [label()], "source_events": ["test:other"]})["entry"]
        with self.assertRaises(store.Conflict):
            self.ledger.correct(entry["id"], {"source_events": ["test:other"], "note": "bad"})
        self.assertEqual(self.ledger.get(entry["id"])["rev"], changed["rev"])
        self.assertEqual(self.ledger.get(other["id"])["source_events"], ["test:other"])


class QualifiedProviderReviewTests(unittest.TestCase):
    def test_derived_v36_bounds_never_become_exact_zero(self):
        provider = fp.OpenFoodFacts(api_version="v3.6")
        for source, unit, value, canonical in (
                ("sodium", "g", "0.01", "salt_g"),
                ("energy-kj", "kJ", "100", "energy_kcal")):
            with self.subTest(source=source):
                product = provider.normalise_v36(A, {"product_name": "Synthetic bound",
                    "nutrition": {"aggregated_set": {"preparation": "as_sold", "per": "100g",
                        "nutrients": {source: {"source": "packaging", "source_per": "100g",
                            "source_index": 0, "unit": unit, "value": value, "modifier": "<"}}}}})
                nutrient = product["nutrients"][canonical]
                self.assertTrue(nutrient is None or str(nutrient).startswith("<"), nutrient)
                item = fp.resolve_item({"barcode": A, "amount": "100 g"}, lambda _: product)
                computed = core.compute_item(item)
                total = core.totals([computed])[canonical]
                self.assertTrue(total["value"] is None or total.get("bounds"), total)


if __name__ == "__main__":
    unittest.main()
