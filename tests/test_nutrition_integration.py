"""Offline integration contracts; tiny schema fixtures, never a copied food database."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import timezone
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skill/scripts'))
import food_providers as fp
import nutrition_core as core
import nutrition_store as store

CODE = '4008400401621'

def v3_product(unit='100g'):
    # Shape and source distinction observed in OFF v3.6, values synthetic.
    return {'code': CODE, 'product_name': 'Synthetic spread', 'nutrition': {
        'aggregated_set': {'per': unit, 'preparation': 'as_sold', 'nutrients': {
            'energy-kcal': {'value': 500, 'unit': 'kcal', 'source': 'packaging', 'source_index': 0, 'source_per': unit},
            'carbohydrates': {'value': 50, 'unit': 'g', 'source': 'packaging', 'source_index': 0, 'source_per': unit},
            'fiber': {'value': 3.67, 'unit': 'g', 'source': 'estimate', 'source_index': 1, 'source_per': unit}}}}}

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        import os
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = {'HOME': self.home.name, 'COACH_HOME': self.home.name,
               'XDG_CONFIG_HOME': self.home.name + '/.config',
               'XDG_CACHE_HOME': self.home.name + '/.cache'}
        isolate = patch.dict(os.environ, env)
        isolate.start(); self.addCleanup(isolate.stop)
        import coachconf
        config = patch.object(coachconf, 'CONFIG_PATH', Path(self.home.name)/'athlete/config.toml')
        config.start(); self.addCleanup(config.stop)
        deny = patch.object(fp, 'urllib_transport', side_effect=AssertionError('external HTTP forbidden in integration tests'))
        deny.start(); self.addCleanup(deny.stop)

    def test_v36_packaging_not_estimated_fibre(self):
        calls = []
        def transport(url, headers, timeout):
            calls.append(url)
            return 200, {}, json.dumps({'code': CODE, 'status': 'success', 'product': v3_product()}).encode()
        p = fp.OpenFoodFacts(api_version='v3.6', transport=transport).lookup_barcode(CODE)
        self.assertIn('/api/v3.6/product/' + CODE + '.json', calls[0])
        self.assertEqual(p['basis'], '100g')
        self.assertEqual(p['nutrients']['energy_kcal'], 500)
        self.assertIsNone(p['nutrients']['fiber_g'])
        self.assertEqual(set(p['nutrients']), set(core.NUTRIENTS))
        self.assertEqual(p['schema_version'], 'v3.6')

    def test_real_search_schema_literal_query_and_503(self):
        calls = []
        # Observed Search-a-licious hit: brands is an array, nutrition absent.
        response = {'hits': [{'code': '5000159484480', 'product_name': 'Snickers',
                             'product_name_de': 'Snickers', 'quantity': '42g',
                             'brands': ['Mars Snickers', ' Snickers']}],
                    'count': 60, 'is_count_exact': True, 'timed_out': False}
        def transport(url, headers, timeout):
            calls.append(url)
            return 200, {}, json.dumps(response).encode()
        off = fp.OpenFoodFacts(transport=transport)
        found = off.search('Snickers OR countries_tags:*', limit=3)
        self.assertTrue(found['selection_required'])
        self.assertEqual(found['results'][0]['brand'], 'Mars Snickers, Snickers')
        self.assertEqual(found['results'][0]['code'], '5000159484480')
        self.assertNotIn('nutrients', found['results'][0])
        url = urlsplit(calls[0]); q = parse_qs(url.query)
        self.assertEqual((url.scheme, url.netloc, url.path), ('https', 'search.openfoodfacts.org', '/search'))
        self.assertEqual(q['q'], ['Snickers or countries_tags\\:\\* countries_tags:"en:germany"'])
        off.search('Ben & Jerry\'s "Cookie-Dough" (NOT AND)')
        q = parse_qs(urlsplit(calls[1]).query)
        self.assertEqual(q['q'], ['Ben \\& Jerry\'s \\"Cookie\\-Dough\\" \\(not and\\) '
                                  'countries_tags:"en:germany"'])
        off.transport = lambda *a: (503, {}, b'unavailable')
        with self.assertRaises(fp.ProviderError) as err:
            off.search('Snickers')
        self.assertEqual(err.exception.kind, 'unavailable')

    def test_bls_quark_mapping_qualifiers_and_blocked(self):
        snap = {'provider': 'bls', 'source_id': 'M000001', 'name': 'Synthetic Magerquark',
                'basis': {'per': '100g'}, 'source_version': '4.0', 'attribution': 'Synthetic MRI attribution',
                'automatic_use': {'status': 'ok', 'reasons': []}, 'nutrients': {
                    'energy_kcal': {'value': 60, 'qualifier': 'exact'},
                    'carbs_g': {'value': 4, 'qualifier': 'exact'},
                    'protein_g': {'value': 12, 'qualifier': 'exact'},
                    'sugars_g': {'value': None, 'qualifier': 'missing'},
                    'fiber_g': {'value': None, 'qualifier': 'trace', 'raw': 'Spuren'}}}
        raw = {'bls_code': 'M000001', 'amount': '200 g'}
        out = fp.resolve_bls_item(raw, lambda code: snap)
        computed = core.compute_item(out)
        self.assertEqual(computed['nutrients']['carbohydrate_g'], 8)
        self.assertEqual(computed['nutrients']['protein_g'], 24)
        self.assertIsNone(computed['nutrients']['fiber_g'])
        self.assertIn('trace', computed['provenance']['notes'])
        self.assertEqual(computed['provenance']['product_code'], 'M000001')
        for status in ('blocked', 'review'):
            snap['automatic_use']['status'] = status
            with self.subTest(status=status), self.assertRaises(core.MissingInfo):
                fp.resolve_bls_item(raw, lambda code: snap)
        label = dict(raw, name='Exact label', nutrients={'energy_kcal': 70},
                     basis='100g', provenance={'kind': 'label'})
        self.assertEqual(fp.resolve_bls_item(label, lambda code: self.fail('label replaced')), label)

    def test_snickers_choose_then_amount_completes_original(self):
        with tempfile.TemporaryDirectory() as td:
            led = store.Ledger(Path(td)/'ledger.sqlite3', timezone.utc)
            product = fp.OpenFoodFacts(api_version='v3.6').normalise_v36(CODE, v3_product())
            led.resolver = lambda raw: fp.resolve_item(raw, lambda code: product)
            raw = {'items': [{'query': 'ein Snickers'}], 'source_events': ['telegram:synthetic:1']}
            p = led.propose(raw)['proposal']
            self.assertEqual(p['input']['items'][0]['query'], 'ein Snickers')
            chosen = led.update_proposal(p['id'], {'items': {'0': {'barcode': CODE}}})
            self.assertEqual(chosen['proposal']['id'], p['id'])
            self.assertIn('How much', ' '.join(chosen['proposal']['questions']))
            saved = led.confirm(p['id'], {'items': {'0': {'amount': '50 g'}},
                                        'source_events': ['telegram:synthetic:2']})['entry']
            self.assertEqual(saved['items'][0]['nutrients']['energy_kcal'], 250)
            self.assertEqual(saved['source_events'], ['telegram:synthetic:1', 'telegram:synthetic:2'])
            self.assertEqual(led.add(raw)['status'], 'duplicate')
            self.assertEqual(len(led.list_entries()), 1)
            led.close()

    def test_cli_search_choose_confirm_simple_amount(self):
        import nutrition as cli
        with tempfile.TemporaryDirectory() as td:
            led = store.Ledger(Path(td)/'ledger.sqlite3', timezone.utc)
            p = led.propose({'items': [{'name': 'Snickers'}]})['proposal']
            off = fp.OpenFoodFacts(api_version='v3.6')
            product = off.normalise_v36(CODE, v3_product())
            lookups = cli.Lookups()
            led.lookups = lookups
            with patch.object(lookups, 'lookup', return_value=product):
                led.resolver = lookups.resolve
                args = cli.parser().parse_args(['choose', p['id'], '--barcode', CODE])
                result = cli.COMMANDS['choose'](args, led)
                self.assertEqual(result['status'], 'pending')
                args = cli.parser().parse_args(['confirm', p['id'], '--amount', '50 g', '--source-event', 'telegram:test:reply'])
                result = cli.cmd_confirm(args, led)
                self.assertEqual(result['entry']['items'][0]['amount_g'], 50)
            with patch.object(lookups, 'search', return_value={'results': [], 'selection_required': True}) as search:
                args = cli.parser().parse_args(['search', 'Magerquark', '--source', 'both'])
                cli.COMMANDS['search'](args, led)
                search.assert_called_once_with('Magerquark', source='both', limit=5)
            led.close()

    def test_trace_receipt_is_bound_not_measured_zero(self):
        raw = {'name': 'Trace salt label', 'basis': '100g', 'amount': '200 g',
               'nutrients': {'energy_kcal': 20, 'salt_g': '<0,5'}, 'provenance': {'kind': 'label'}}
        item = core.compute_item(raw)
        self.assertEqual(item['nutrient_bounds']['salt_g'], {'lower': 0, 'upper': 1.0, 'upper_exclusive': True})
        total = core.totals([item])
        self.assertIn('salt <1 g', core.receipt(total))
        self.assertNotIn('salt 0 g', core.receipt(total))

    def test_v36_cache_does_not_reuse_legacy_v2_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            cachepath = Path(td)/'public.sqlite3'
            old = fp.OpenFoodFacts(api_version='v2', transport=lambda *a: (200, {}, json.dumps({
                'code': CODE, 'product': {'code': CODE, 'product_name': 'Old snapshot',
                                        'quantity': '100g', 'nutriments': {'energy-kcal_100g': 100}}}).encode()))
            cache = fp.ProviderCache(cachepath, old)
            cache.lookup(CODE); cache.close()
            fresh = fp.OpenFoodFacts(api_version='v3.6', transport=lambda *a: (200, {}, json.dumps({
                'code': CODE, 'product': v3_product()}).encode()))
            cache = fp.ProviderCache(cachepath, fresh)
            self.assertEqual(cache.lookup(CODE)['nutrients']['energy_kcal'], 500)
            cache.close()

    def test_real_bls_module_config_path_lookup_and_200g(self):
        import nutrition as cli
        import nutrition_bls as bls
        import test_nutrition_bls as fixtures
        with tempfile.TemporaryDirectory() as td:
            db = Path(td)/'separate-provider-cache'/'bls.sqlite3'
            workbook = Path(td)/'synthetic.xlsx'
            workbook.write_bytes(fixtures.make_xlsx([fixtures.header_row(), fixtures.food(
                'M000001', 'Synthetic Magerquark', ENERCC=60, PROT625=12, CHO=4,
                FAT=0.2, OLSAC=0, FIBT='TR')]))
            bls.import_bls(workbook, db)
            lookups = cli.Lookups()
            def config(key, default=None):
                return str(db) if key == 'nutrition.bls_db_path' else default
            with patch.object(cli.cc, 'get', side_effect=config):
                self.assertEqual(lookups.search('Magerquark', source='bls')['results'][0]['provider'], 'bls')
                result = core.compute_item(lookups.resolve({'bls_code': 'M000001', 'amount': '200 g'}))
                self.assertEqual(result['nutrients']['carbohydrate_g'], 8)
                self.assertIsNone(result['nutrients']['fiber_g'])
                args = cli.parser().parse_args(['lookup', 'M000001', '--source', 'bls'])
                args.json = True
                led = type('Local', (), {'lookups': lookups})()
                self.assertEqual(cli.cmd_lookup(args, led)['source_id'], 'M000001')
            lookups.close()

    def test_zero_330ml_clear_variant_no_density_no_extra_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            p = v3_product('100ml'); p['product_name'] = 'Coca-Cola Zero'
            p['quantity'] = '330 ml'
            for n in p['nutrition']['aggregated_set']['nutrients'].values():
                n['value'] = 0
            product = fp.OpenFoodFacts(api_version='v3.6').normalise_v36(CODE, p)
            led = store.Ledger(Path(td)/'ledger.sqlite3', timezone.utc)
            led.resolver = lambda raw: fp.resolve_item(raw, lambda code: product)
            saved = led.add({'items': [{'barcode': CODE, 'amount': '0,33 l'}]})['entry']
            self.assertEqual(saved['items'][0]['name'], 'Coca-Cola Zero')
            self.assertEqual(saved['items'][0]['amount_ml'], 330)
            self.assertIsNone(saved['items'][0]['amount_g'])
            self.assertEqual(saved['items'][0]['nutrients']['energy_kcal'], 0)
            self.assertEqual(led.pending(), [])
            led.close()

    def test_unknown_product_quantity_answer_remains_same_pending_query(self):
        import nutrition as cli
        with tempfile.TemporaryDirectory() as td:
            led = store.Ledger(Path(td)/'ledger.sqlite3', timezone.utc)
            p = led.propose({'items': [{'query': 'eine Cola'}], 'source_events': ['telegram:offline:1']})['proposal']
            args = cli.parser().parse_args(['confirm', p['id'], '--amount', '330 ml', '--source-event', 'telegram:offline:2'])
            out = cli.cmd_confirm(args, led)
            self.assertEqual(out['status'], 'pending')
            self.assertEqual(out['proposal']['input']['items'][0], {'query': 'eine Cola', 'amount': '330 ml'})
            self.assertEqual(len(led.pending()), 1)
            self.assertEqual(led.list_entries(), [])
            led.close()

    def test_v36_trace_product_display_preserves_bound(self):
        import nutrition as cli
        p = v3_product()
        p['nutrition']['aggregated_set']['nutrients']['salt'] = {
            'value': 0.01, 'modifier': '<', 'unit': 'g', 'source': 'packaging', 'source_index': 0, 'source_per': '100g'}
        product = fp.OpenFoodFacts(api_version='v3.6').normalise_v36(CODE, p)
        self.assertIn('salt <0.01 g', cli.text_product(product))

    def test_trace_day_average_is_not_an_exact_zero(self):
        with tempfile.TemporaryDirectory() as td:
            led = store.Ledger(Path(td)/'ledger.sqlite3', timezone.utc)
            entry = led.add({'items': [{'name': 'trace', 'basis': '100g', 'amount': '100 g',
                'nutrients': {'energy_kcal': 10, 'salt_g': '<0.01'}, 'provenance': {'kind': 'label'}}]})['entry']
            led.mark_day(entry['local_date'], 'complete')
            self.assertIsNone(led.days(entry['local_date'], entry['local_date'])['average_complete_days']['salt_g'])
            led.close()

    def test_derived_values_do_not_turn_trace_into_measured_fact(self):
        raw = {'name': 'qualified conversions', 'basis': '100g', 'amount': '100 g',
               'nutrients': {'energy_kcal': 20, 'energy_kj': '<100', 'salt_g': 0.2,
                             'sodium_mg': '<100', 'carbohydrate_total_g': 10, 'fiber_g': '<2'},
               'provenance': {'kind': 'label'}}
        out = core.compute_item(raw)
        self.assertNotIn('energy_kcal', out['nutrient_bounds'])
        self.assertNotIn('salt_g', out['nutrient_bounds'])
        self.assertIsNone(out['nutrients']['carbohydrate_g'])

if __name__ == '__main__':
    unittest.main()
