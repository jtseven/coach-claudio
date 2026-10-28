"""Synthetic SQLite ledger and injected adapter only; never network or credentials."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/scripts'))
import nutrition_yazio as ny


class Fake:
    def __init__(self):
        self.products, self.consumed, self.calls = {}, {}, []
        self.fail = None
        self.live = True

    def capabilities(self):
        return {**{k: 'live_confirmed' if self.live else 'researched' for k in
                   ('create_private_product', 'create_consumed', 'delete_consumed')},
                'base_unit': 'g', 'nutrients': {k: 'live_confirmed' for k in ny.NUTRIENT_KEYS}}

    def prepare_product(self, per, nutrients, pid, name, producer=None):
        return ny.prepare_product(per, nutrients, pid, name, category='synthetic',
                                  base_units=['g', 'ml'], salt_write=True)

    def prepare_consumed(self, cid, pid, amount, unit, when, daytime, base_unit):
        return ny.prepare_consumed(cid, pid, amount, unit, when, daytime,
                                   tz='UTC', base_unit=base_unit)

    def reconcile_product(self, p):
        return {'status': 'confirmed' if p['id'] in self.products else 'absent'}

    def reconcile_consumed(self, p):
        return {'status': 'confirmed' if p['products'][0]['id'] in self.consumed else 'absent'}

    def create_private_product(self, p):
        self.calls.append(('product', p['id']))
        if self.fail == 'refuse':
            self.fail = None
            raise ny.YazioHTTPError(400, None, 'create_private_product')
        self.products[p['id']] = p
        return {'status': 'confirmed'}

    def create_consumed(self, p, *, base_unit='g'):
        cid = p['products'][0]['id']
        self.calls.append(('consumed', cid))
        self.consumed[cid] = p['products'][0]
        if self.fail == 'lost':
            self.fail = None
            raise ny.UnknownOutcome('create_consumed', cid)
        return {'status': 'confirmed'}

    def get_consumed(self, day):
        return {'products': list(self.consumed.values()), 'recipe_portions': [], 'simple_products': []}

    def delete_consumed(self, cid, day, *, base_unit='g'):
        self.calls.append(('delete', cid))
        self.consumed.pop(cid, None)
        if self.fail == 'delete':
            self.fail = None
            raise ny.UnknownOutcome('delete_consumed', cid)
        return {'status': 'deleted'}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / 'ledger.sqlite3'
        with sqlite3.connect(self.ledger) as db:
            db.executescript('CREATE TABLE entries(id TEXT PRIMARY KEY,rev INTEGER,state TEXT);'
                             'CREATE TABLE revisions(entry_id TEXT,rev INTEGER,payload TEXT);')
        self.fake = Fake()
        guard = patch.object(ny, 'HTTPSTransport', side_effect=AssertionError('network forbidden in sync tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def entry(self, rev=1, state='active', items=None):
        item = {'name': 'Synthetic oats', 'amount': '50 g', 'basis': {'per': '100g'},
                'nutrients': {'fat_g': 1, 'energy_kcal': 200, 'protein_g': 8},
                'provenance': {'kind': 'provider', 'provider': 'synthetic'}}
        payload = {'input': {'items': items if items is not None else [item],
                            'eaten_at': '2026-01-01T12:00:00+00:00', 'meal': 'lunch'}}
        with sqlite3.connect(self.ledger) as db:
            db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?)', ('meal', rev, state))
            db.execute('INSERT INTO revisions VALUES (?,?,?)',
                       ('meal', rev, json.dumps(payload) if state == 'active' else None))

    def engine(self, **kwargs):
        import nutrition_sync
        return nutrition_sync.Sync(self.root, client_factory=lambda: self.fake,
                                   **{'enabled': True, **kwargs})

    def test_status_pending_is_read_only_and_run_persists_ids(self):
        self.entry()
        s = self.engine()
        self.assertEqual(s.status()['pending'], 1)
        self.assertFalse((self.root / 'sync_outbox.sqlite3').exists())
        self.assertEqual(s.run()['synced'], 1)
        calls = list(self.fake.calls)
        self.assertEqual(s.run()['synced'], 1)
        self.assertEqual(calls, self.fake.calls)
        self.assertEqual((self.root / 'sync_outbox.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_lost_response_reconciles_without_duplicate(self):
        self.entry()
        self.fake.fail = 'lost'
        s = self.engine()
        self.assertEqual(s.run()['unknown'], 1)
        calls = list(self.fake.calls)
        self.assertEqual(s.run()['synced'], 1)
        self.assertEqual(calls, self.fake.calls)

    def test_disabled_and_dryrun_do_not_load_credentials_or_mutate(self):
        self.entry()
        for kwargs in ({'enabled': False}, {'dry_run': True}):
            s = self.engine(**kwargs)
            s.client_factory = lambda: self.fail('loaded credentials')
            self.assertEqual(s.run()['pending'], 1)
            self.assertFalse(s.path.exists())

    def test_unverified_is_blocked_and_retry_after_verification(self):
        self.entry()
        self.fake.live = False
        s = self.engine()
        self.assertEqual(s.run()['blocked'], 1)
        self.assertIn('live_confirmed', str(s.status()))
        self.assertFalse(self.fake.calls)
        self.fake.live = True
        self.assertEqual(s.run()['synced'], 1)

    def test_correction_delete_crash_resumes_before_new_create(self):
        self.entry()
        s = self.engine()
        s.run()
        old = set(self.fake.consumed)
        self.entry(2)
        self.fake.fail = 'delete'
        self.assertEqual(s.run()['unknown'], 1)
        self.assertFalse(self.fake.consumed)
        self.assertEqual(s.run()['synced'], 1)
        self.assertTrue(old.isdisjoint(self.fake.consumed))
        self.assertEqual(sum(c[0] == 'delete' for c in self.fake.calls), 1)

    def test_revision_while_unknown_reconciles_and_removes_old(self):
        self.entry()
        s = self.engine()
        self.fake.fail = 'lost'
        s.run()
        self.entry(2)
        self.assertEqual(s.run()['synced'], 1)
        self.assertEqual(len(self.fake.consumed), 1)
        self.assertEqual(sum(c[0] == 'delete' for c in self.fake.calls), 1)

    def test_erase_all_previous_remote_ids_and_scrub(self):
        self.entry()
        s = self.engine()
        s.run()
        self.entry(2)
        s.run()
        self.entry(3, 'erased')
        self.assertEqual(s.run()['synced'], 1)
        self.assertFalse(self.fake.consumed)
        with sqlite3.connect(s.path) as db:
            for row in db.execute('SELECT snapshot,product,consumed FROM jobs'):
                self.assertEqual(row, (None, None, None))
        self.assertNotIn(b'Synthetic oats', s.path.read_bytes())

    def test_ml_and_missing_photo_quantity_block_exactly(self):
        items = [{'name': 'Synthetic drink', 'amount': '330 ml', 'basis': {'per': '100ml'},
                  'nutrients': {'fat_g': 1, 'energy_kcal': 30}, 'provenance': {'kind': 'user'}},
                 {'name': 'Photo estimate', 'basis': {'per': 'total'},
                  'nutrients': {'fat_g': 1, 'energy_kcal': 500}, 'provenance': {'kind': 'estimate',
                  'confidence': 0.5, 'uncertainty': 'unknown portion'}}]
        self.entry(items=items)
        result = self.engine().run()
        self.assertEqual(result['blocked'], 1)
        self.assertIn('base unit ml', str(result))
        self.assertIn('missing amount', str(result))
        self.assertFalse(self.fake.calls)

    def test_per_unit_contract_syncs_g_and_ml_without_density(self):
        old_caps = self.fake.capabilities()
        self.fake.capabilities = lambda: {**old_caps, 'by_base_unit': {
            unit: {**old_caps, 'base_unit': unit} for unit in ('g', 'ml')}}
        self.entry(items=[{'name': 'Synthetic drink', 'amount': '0.33 l', 'basis': '100ml',
                           'nutrients': {'fat_g': 1, 'energy_kcal': 30, 'salt_g': 0.1},
                           'provenance': {'kind': 'user'}},
                          {'name': 'Synthetic food', 'amount': '50 g', 'basis': '100g',
                           'nutrients': {'fat_g': 1, 'energy_kcal': 200, 'salt_g': 0.2},
                           'provenance': {'kind': 'user'}}])
        before = self.ledger.read_bytes()
        self.assertEqual(self.engine().run()['synced'], 1)
        self.assertEqual(sorted(p['base_unit'] for p in self.fake.products.values()), ['g', 'ml'])
        self.assertEqual(sorted(p['amount'] for p in self.fake.consumed.values()), [50, 330])
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_per_unit_salt_display_proof_is_required_even_with_lenient_adapter(self):
        old_caps = self.fake.capabilities()
        ml = {**old_caps, 'base_unit': 'ml', 'nutrients': {
            **old_caps['nutrients'], 'salt_g': 'unverified'}}
        self.fake.capabilities = lambda: {**old_caps, 'by_base_unit': {'g': old_caps, 'ml': ml}}
        self.entry(items=[{'name': 'Synthetic drink', 'amount': '330 ml', 'basis': '100ml',
                          'nutrients': {'fat_g': 1, 'energy_kcal': 30, 'salt_g': 0.1},
                          'provenance': {'kind': 'user'}}])
        before = self.ledger.read_bytes()
        self.assertEqual(self.engine().run()['blocked'], 1)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_delete_does_not_fall_back_to_other_unit_proof(self):
        old_caps = self.fake.capabilities()
        ml = {**old_caps, 'base_unit': 'ml'}
        self.fake.capabilities = lambda: {**old_caps, 'by_base_unit': {'g': old_caps, 'ml': ml}}
        self.entry(items=[{'name': 'Synthetic drink', 'amount': '330 ml', 'basis': '100ml',
                          'nutrients': {'fat_g': 1, 'energy_kcal': 30}, 'provenance': {'kind': 'user'}}])
        s = self.engine()
        self.assertEqual(s.run()['synced'], 1)
        self.entry(2, 'erased')
        self.fake.capabilities = lambda: {**old_caps, 'by_base_unit': {'g': old_caps}}
        self.assertEqual(s.run()['blocked'], 1)
        self.assertEqual(len(self.fake.consumed), 1)

    def test_estimated_total_with_real_quantity_scales(self):
        self.entry(items=[{'name': 'Estimated plate', 'basis': {'per': 'total'},
                          'amount': '250 g', 'nutrients': {'fat_g': 1, 'energy_kcal': 500},
                          'provenance': {'kind': 'estimate', 'confidence': 0.5,
                                         'uncertainty': 'portion estimated'}}])
        self.assertEqual(self.engine().run()['synced'], 1)
        p = next(iter(self.fake.products.values()))
        self.assertEqual(p['nutrients']['energy.energy'], 2)
        self.assertEqual(next(iter(self.fake.consumed.values()))['amount'], 250)

    def test_declared_serving_size_is_an_explicit_quantity(self):
        self.entry(items=[{'name': 'Synthetic bar', 'amount': '1 serving',
                           'basis': {'per': '100g', 'serving': {'value': 40.0, 'unit': 'g'}},
                           'nutrients': {'fat_g': 20, 'energy_kcal': 400},
                           'provenance': {'kind': 'provider', 'provider': 'synthetic'}}])
        self.assertEqual(self.engine().run()['synced'], 1)
        self.assertEqual(next(iter(self.fake.consumed.values()))['amount'], 40)

    def test_serving_without_declared_size_stays_blocked(self):
        self.entry(items=[{'name': 'Synthetic bar', 'amount': '1 serving', 'basis': {'per': 'total'},
                           'nutrients': {'fat_g': 8, 'energy_kcal': 160},
                           'provenance': {'kind': 'user'}}])
        self.assertEqual(self.engine().run()['blocked'], 1)
        self.assertFalse(self.fake.calls)

    def test_http_refusal_is_blocked_not_unknown_and_retries(self):
        self.entry()
        self.fake.fail = 'refuse'
        s = self.engine()
        out = s.run()
        self.assertEqual((out['blocked'], out['unknown']), (1, 0))
        self.assertIn('HTTP 400', str(out['entries']))
        self.assertEqual(s.run()['synced'], 1)

    def test_actual_lock_blocks_second_runner(self):
        import fcntl
        self.entry()
        s = self.engine()
        with s.locked():
            with self.assertRaises(BlockingIOError):
                s.run()
        self.assertEqual(s.run()['synced'], 1)

    def test_disabled_preserves_pending_unknown_without_credentials(self):
        self.entry()
        self.fake.fail = 'lost'
        s = self.engine()
        s.run()
        before = s.path.read_bytes()
        s.enabled = False
        s.client_factory = lambda: self.fail('credentials loaded while disabled')
        self.assertEqual(s.run()['unknown'], 1)
        self.assertEqual(before, s.path.read_bytes())

    def test_partial_items_not_meal_synced(self):
        self.entry(items=[{'name': 'Good', 'amount': '50 g', 'basis': {'per': '100g'},
                          'nutrients': {'fat_g': 1, 'energy_kcal': 200}, 'provenance': {'kind': 'user'}},
                         {'name': 'Unknown quantity', 'basis': {'per': 'total'},
                          'nutrients': {'fat_g': 1, 'energy_kcal': 200}, 'provenance': {'kind': 'user'}}])
        result = self.engine().run()
        self.assertEqual(result['synced'], 0)
        self.assertEqual(result['blocked'], 1)
        self.assertEqual(len(self.fake.consumed), 1)

    def test_environment_dryrun_also_guards_direct_engine(self):
        self.entry()
        with patch.dict(os.environ, {'COACH_DRY_RUN': '1'}):
            s = self.engine()
            s.client_factory = lambda: self.fail('dry-run read credentials')
            self.assertEqual(s.run()['pending'], 1)
            self.assertFalse(s.path.exists())

    def test_ids_and_prepared_payloads_durable_before_external_write(self):
        self.entry()
        s = self.engine()
        original = self.fake.create_private_product
        def checked(payload):
            with sqlite3.connect(s.path) as db:
                row = db.execute('SELECT pid,cid,product,consumed,state FROM jobs').fetchone()
            self.assertEqual(payload['id'], row[0])
            self.assertTrue(row[1])
            self.assertEqual(json.loads(row[2]), payload)
            self.assertTrue(json.loads(row[3])['products'][0]['id'])
            self.assertEqual(row[4], 'unknown')
            return original(payload)
        self.fake.create_private_product = checked
        self.assertEqual(s.run()['synced'], 1)

    def test_unconfigured_persists_queue_then_retries_same_ids(self):
        self.entry()
        s = self.engine()
        def absent():
            raise ny.NotConfigured('synthetic config missing; setup required')
        s.client_factory = absent
        self.assertEqual(s.run()['blocked'], 1)
        with sqlite3.connect(s.path) as db:
            before = db.execute('SELECT pid,cid FROM jobs').fetchall()
        s.client_factory = lambda: self.fake
        self.assertEqual(s.run()['synced'], 1)
        with sqlite3.connect(s.path) as db:
            self.assertEqual(before, db.execute('SELECT pid,cid FROM jobs').fetchall())

    def test_http_success_without_readback_is_unknown(self):
        self.entry()
        self.fake.create_consumed = lambda p, **kw: {'status': 'confirmed', 'http_status': 204}
        result = self.engine().run()
        self.assertEqual(result['unknown'], 1)
        self.assertEqual(result['synced'], 0)

    def test_erase_read_failure_keeps_recovery_and_reports_unknown(self):
        self.entry()
        s = self.engine()
        s.run()
        self.entry(2, 'erased')
        def unavailable(day):
            raise ny.TransportError('synthetic read unavailable')
        self.fake.get_consumed = unavailable
        self.assertEqual(s.run()['unknown'], 1)
        with sqlite3.connect(s.path) as db:
            self.assertTrue(db.execute('SELECT snapshot FROM jobs').fetchone()[0])

    def test_trace_blocks_instead_of_sending_fake_exact_zero(self):
        self.entry(items=[{'name': 'Trace', 'amount': '50 g', 'basis': {'per': '100g'},
                          'nutrients': {'fat_g': 1, 'energy_kcal': 200, 'salt_g': '<0.5'},
                          'provenance': {'kind': 'user'}}])
        result = self.engine().run()
        self.assertEqual(result['blocked'], 1)
        self.assertIn('trace', str(result))
        self.assertFalse(self.fake.calls)

    def test_string_basis_supported_as_core_allows(self):
        self.entry(items=[{'name': 'String basis', 'amount': '50 g', 'basis': '100g',
                          'nutrients': {'fat_g': 1, 'energy_kcal': 200}, 'provenance': {'kind': 'user'}}])
        self.assertEqual(self.engine().run()['synced'], 1)

    def test_omit_salt_exports_six_nutrients_without_mutating_real_ledger(self):
        from datetime import timezone
        import nutrition_store
        self.ledger.unlink()
        ledger = nutrition_store.Ledger(self.ledger, timezone.utc)
        self.addCleanup(ledger.close)
        caps = self.fake.capabilities()
        caps['nutrients']['salt_g'] = 'unverified'
        self.fake.capabilities = lambda: {'by_base_unit': {
            unit: {**caps, 'base_unit': unit} for unit in ('g', 'ml')}}
        received = []
        def prepare(per, nutrients, pid, name, producer=None):
            received.append(dict(nutrients))
            return ny.prepare_product(per, nutrients, pid, name, category='synthetic',
                                      base_units=['g', 'ml'], salt_write=False)
        self.fake.prepare_product = prepare
        nutrients = {'energy_kcal': 200, 'protein_g': 8, 'carbohydrate_g': 20,
                     'fat_g': 9, 'fiber_g': 3, 'sugars_g': 5, 'salt_g': 0.2}
        logged = ledger.add({'eaten_at': '2026-01-01T12:00:00+00:00', 'meal': 'lunch',
                             'items': [{'name': 'Synthetic ' + unit, 'basis': '100' + unit,
                                        'amount': '50 ' + unit, 'nutrients': nutrients,
                                        'provenance': {'kind': 'user'}} for unit in ('g', 'ml')]})
        before = self.ledger.read_bytes()
        self.assertIn('salt', logged['entry']['receipt'])
        self.assertEqual(self.engine().run()['blocked'], 1)
        self.assertFalse(self.fake.calls)
        result = self.engine(omit_salt=True).run()
        self.assertEqual(result['synced'], 1, result)
        self.assertEqual(result['omitted_nutrients'], ['salt_g'])
        self.assertEqual(len(received), 2)
        for values in received:
            self.assertEqual(set(values), set(nutrients) - {'salt_g'})
        for product in self.fake.products.values():
            self.assertEqual(set(product['nutrients']),
                             {ny.NUTRIENT_KEYS[k] for k in nutrients if k != 'salt_g'})
        self.assertEqual(self.ledger.read_bytes(), before)
        local = ledger.get(logged['entry']['id'])
        self.assertEqual(local, logged['entry'])
        self.assertEqual(set(local['totals']), set(nutrients))
        self.assertEqual(local['totals']['salt_g']['value'], 0.2)
        self.assertEqual(caps['nutrients']['salt_g'], 'unverified')
        with sqlite3.connect(self.engine().path) as db:
            for (snapshot,) in db.execute('SELECT snapshot FROM jobs'):
                self.assertEqual(json.loads(snapshot)['item']['nutrients'], nutrients)
        # Persisted omissions stay visible even when a later status uses defaults.
        status = self.engine().status()
        self.assertEqual(status['entries'][0]['omissions'], [
            {'rev': 1, 'item': i, 'omitted_nutrients': ['salt_g']} for i in range(2)])

    def test_omit_salt_filters_canonical_sodium_and_only_salt_traces(self):
        import uuid
        for nutrient, value, blocked in (('salt_g', '<0.5', False),
                                         ('sodium_mg', '<100', False),
                                         ('sodium_g', 0.1, False),
                                         ('sodium_mg', 100, False),
                                         ('protein_g', '<2', True),
                                         ('energy_kcal', '<200', True),
                                         ('carbohydrate_total_g', '<10', True),
                                         ('fiber_g', '<1', True),
                                         ('sugars_g', '<1', True),
                                         ('fat_g', '<1', True)):
            with self.subTest(nutrient=nutrient, value=value):
                raw = {'name': 'Synthetic trace', 'amount': '50 g', 'basis': '100g',
                       'nutrients': {'fat_g': 1, 'energy_kcal': 200, 'carbohydrate_g': 20,
                                     'salt_g': '<0.5', nutrient: value},
                       'provenance': {'kind': 'user'}}
                if nutrient.startswith('sodium'):
                    del raw['nutrients']['salt_g']
                job = {'pid': str(uuid.uuid4()), 'cid': str(uuid.uuid4()),
                       'snapshot': json.dumps({'item': raw, 'when': '2026-01-01T12:00:00+00:00',
                                               'meal': 'lunch'})}
                engine = self.engine(omit_salt=True)
                if blocked:
                    with self.assertRaisesRegex(ValueError, 'trace'):
                        engine.prepare(self.fake, job)
                else:
                    product, _, _ = engine.prepare(self.fake, job)
                    self.assertNotIn(ny.NUTRIENT_KEYS['salt_g'], json.loads(product)['nutrients'])

    def test_omit_salt_keeps_other_unit_nutrient_and_operation_gates(self):
        self.entry(items=[{'name': 'Synthetic food', 'amount': '50 g', 'basis': '100g',
                          'nutrients': {'fat_g': 1, 'energy_kcal': 200, 'protein_g': 8, 'salt_g': 0.2},
                          'provenance': {'kind': 'user'}}])
        caps = self.fake.capabilities()
        self.fake.capabilities = lambda: caps
        for key in ('protein_g', 'energy_kcal'):
            with self.subTest(key=key):
                caps['nutrients'][key] = 'unverified'
                result = self.engine(omit_salt=True).run()
                self.assertEqual(result['blocked'], 1)
                self.assertIn(key, str(result))
                self.assertFalse(self.fake.calls)
                caps['nutrients'][key] = 'live_confirmed'
        self.fake.live = False
        caps['create_consumed'] = 'researched'
        self.assertEqual(self.engine(omit_salt=True).run()['blocked'], 1)
        self.assertFalse(self.fake.calls)
        caps['create_consumed'] = 'live_confirmed'
        caps['base_unit'] = 'ml'
        self.assertEqual(self.engine(omit_salt=True).run()['blocked'], 1)
        self.assertFalse(self.fake.calls)

    def test_omit_salt_direct_engine_requires_literal_true(self):
        self.entry(items=[{'name': 'Synthetic food', 'amount': '50 g', 'basis': '100g',
                          'nutrients': {'fat_g': 1, 'energy_kcal': 200, 'salt_g': 0.2},
                          'provenance': {'kind': 'user'}}])
        caps = self.fake.capabilities()
        caps['nutrients']['salt_g'] = 'unverified'
        self.fake.capabilities = lambda: caps
        for value in (False, 'true', 1, None):
            with self.subTest(value=value):
                result = self.engine(omit_salt=value).run()
                self.assertEqual(result['blocked'], 1)
                self.assertEqual(result['omitted_nutrients'], [])
                self.assertFalse(self.fake.calls)

    def test_omit_salt_notice_survives_unknown_retry_with_config_disabled(self):
        self.entry()
        engine = self.engine(omit_salt=True)
        self.fake.fail = 'lost'
        self.assertEqual(engine.run()['unknown'], 1)
        with sqlite3.connect(engine.path) as db:
            before = db.execute('SELECT product,omitted_nutrients FROM jobs').fetchone()
        result = self.engine().run()
        self.assertEqual(result['synced'], 1)
        self.assertEqual(result['omitted_nutrients'], [])
        self.assertEqual(result['entries'][0]['omissions'], [
            {'rev': 1, 'item': 0, 'omitted_nutrients': ['salt_g']}])
        with sqlite3.connect(engine.path) as db:
            self.assertEqual(db.execute('SELECT product,omitted_nutrients FROM jobs').fetchone(), before)

    def test_legacy_outbox_status_is_read_only_and_run_migrates_omissions(self):
        self.entry()
        engine = self.engine()
        self.assertEqual(engine.run()['synced'], 1)
        with sqlite3.connect(engine.path) as db:
            db.execute('ALTER TABLE jobs DROP COLUMN omitted_nutrients')
        before = engine.path.read_bytes()
        self.assertEqual(engine.status()['entries'][0]['omissions'], [])
        self.assertEqual(engine.path.read_bytes(), before)
        self.assertEqual(self.engine(omit_salt=True).run()['synced'], 1)
        with sqlite3.connect(engine.path) as db:
            self.assertEqual(db.execute('SELECT omitted_nutrients FROM jobs').fetchone(), ('[]',))

    def test_real_store_snapshot_and_tombstone_contract(self):
        from datetime import timezone
        import nutrition_store
        # Replace the deliberately small schema fixture with the real ledger schema.
        self.ledger.unlink()
        ledger = nutrition_store.Ledger(self.ledger, timezone.utc)
        self.addCleanup(ledger.close)
        logged = ledger.add({'eaten_at': '2026-01-01T12:00:00+00:00', 'meal': 'lunch',
                             'items': [{'name': 'Real ledger synthetic food', 'basis': '100g',
                                        'amount': '50 g', 'nutrients': {'fat_g': 1, 'energy_kcal': 200},
                                        'provenance': {'kind': 'user'}}]})
        s = self.engine()
        self.assertEqual(s.run()['synced'], 1)
        ledger.delete(logged['entry']['id'], erase=True)
        self.assertEqual(s.status()['pending'], 1)
        self.assertEqual(s.run()['synced'], 1)
        self.assertFalse(self.fake.consumed)

    def test_cli_disabled_and_dryrun_use_isolated_config(self):
        import subprocess
        athlete = self.root / 'coach/athlete'
        athlete.mkdir(parents=True)
        config = athlete / 'config.toml'
        script = Path(__file__).resolve().parents[1] / 'skill/scripts/nutrition_sync.py'
        env = {'HOME': str(self.root), 'COACH_HOME': str(athlete.parent),
               'XDG_CONFIG_HOME': str(self.root / 'empty-config'),
               'TMPDIR': str(self.root), 'PYTHONPYCACHEPREFIX': str(self.root / 'pycache')}
        for text, dry in (('[integrations]\nnutrition_log=true\nnutrition_yazio=false\n', False),
                          ('[integrations]\nnutrition_log=true\nnutrition_yazio=true\n', True)):
            config.write_text(text)
            runenv = {**env, **({'COACH_DRY_RUN': '1'} if dry else {})}
            process = subprocess.run([sys.executable, str(script), 'run'], env=runenv,
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertEqual(result['dry_run'], dry)
            self.assertEqual(result['enabled'], dry)
            self.assertFalse((athlete / 'state').exists())

    def test_unsafe_outbox_symlink_refused(self):
        self.entry()
        target = self.root / 'unrelated'
        target.write_text('untouched')
        self.engine().path.symlink_to(target)
        with self.assertRaises(ny.InsecureConfig):
            self.engine().run()
        self.assertEqual(target.read_text(), 'untouched')

    def test_engine_default_is_opt_out(self):
        import nutrition_sync
        self.entry()
        def forbidden():
            self.fail('loaded credentials without explicit enable')
        s = nutrition_sync.Sync(self.root, client_factory=forbidden)
        self.assertEqual(s.run()['pending'], 1)
        self.assertFalse(s.path.exists())

    def test_crash_after_product_send_reconciles_same_ids(self):
        self.entry()
        s = self.engine()
        original = self.fake.create_private_product
        def crash(p):
            original(p)
            raise SystemExit('synthetic process crash before durable acknowledgement')
        self.fake.create_private_product = crash
        with self.assertRaises(SystemExit):
            s.run()
        self.assertEqual(s.status()['unknown'], 1)
        self.fake.create_private_product = original
        self.assertEqual(self.engine().run()['synced'], 1)
        self.assertEqual(sum(c[0] == 'product' for c in self.fake.calls), 1)
        self.assertEqual(len(self.fake.consumed), 1)

    def test_cli_security_failure_is_json_not_traceback(self):
        import contextlib
        import io
        import types
        import nutrition_sync
        self.entry()
        self.engine().path.symlink_to(self.ledger)
        cc = types.SimpleNamespace(NUTRITION_DIR=self.root, DRY_RUN=False,
                                   get=lambda key, default: True)
        output = io.StringIO()
        with patch.dict(sys.modules, {'coachconf': cc}), contextlib.redirect_stdout(output):
            code = nutrition_sync.main(['run'])
        self.assertEqual(code, 1)
        self.assertIn('symlink', json.loads(output.getvalue())['error'])

    def test_erase_never_sent_queue_scrubs_without_credentials(self):
        self.entry()
        s = self.engine()
        def unavailable():
            raise ny.NotConfigured('not configured')
        s.client_factory = unavailable
        self.assertEqual(s.run()['blocked'], 1)
        self.entry(2, 'erased')
        self.assertEqual(s.run()['synced'], 1)
        with sqlite3.connect(s.path) as db:
            self.assertEqual(db.execute('SELECT snapshot,phase FROM jobs').fetchone(),
                             (None, 'retired'))

    def test_tombstone_notconfigured_has_meaningful_block_reason(self):
        self.entry()
        s = self.engine()
        s.run()
        self.entry(2, 'erased')
        def unavailable():
            raise ny.NotConfigured('synthetic setup required')
        s.client_factory = unavailable
        result = s.run()
        self.assertEqual(result['blocked'], 1)
        self.assertIn('setup required', str(result))

    def test_standalone_enabled_cli_queues_real_store_and_reports_unconfigured(self):
        import shutil
        import subprocess
        self.entry()
        athlete = self.root / 'cli/athlete'
        state = athlete / 'state/nutrition'
        state.mkdir(parents=True, mode=0o700)
        shutil.copyfile(self.ledger, state / 'ledger.sqlite3')
        (athlete / 'config.toml').write_text('[integrations]\nnutrition_log=true\nnutrition_yazio=true\n')
        env = {'HOME': str(self.root), 'COACH_HOME': str(athlete.parent),
               'YAZIO_CONFIG': str(self.root / 'missing-yazio.json'), 'TMPDIR': str(self.root),
               'PYTHONPYCACHEPREFIX': str(self.root / 'pycache')}
        script = Path(__file__).resolve().parents[1] / 'skill/scripts/nutrition_sync.py'
        process = subprocess.run([sys.executable, str(script), 'run'], env=env,
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['blocked'], 1)
        self.assertIn('setup', str(result))
        self.assertTrue((state / 'sync_outbox.sqlite3').exists())
        followup = subprocess.run([sys.executable, str(script), 'status'], env=env,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(followup.stdout)['blocked'], 1)


if __name__ == '__main__':
    unittest.main()
