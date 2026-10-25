"""Offline contract tests for the optional YAZIO adapter.

No account, no network: every HTTP exchange goes through FakeTransport, and
HOME, XDG_CONFIG_HOME, COACH_HOME and TMPDIR point into a throwaway directory.
All credentials below are synthetic placeholders.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skill/scripts'))
import nutrition_yazio as ny  # noqa: E402

REAL_TRANSPORT = ny.HTTPSTransport  # captured before the per-test network guard
UA = 'YAZIO/0.0.0 (synthetic.test; build:0; Test 0) Ktor'
P_ID = '11111111-2222-4333-8444-555555555555'
C_ID = '66666666-7777-4888-9999-aaaaaaaaaaaa'


class FakeTransport:
    """Scripted HTTP: each call pops the next (status, body, headers) or raises."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append({'method': method, 'url': url, 'headers': dict(headers),
                           'body': json.loads(body) if body else None, 'timeout': timeout})
        if not self.responses:
            raise AssertionError(f'unexpected request {method} {url}')
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        status, payload, *rest = r
        raw = payload if isinstance(payload, bytes) else (
            b'' if payload is None else json.dumps(payload).encode())
        return status, (rest[0] if rest else {}), raw


def token_response(access='SYNTH-ACCESS-1', refresh='SYNTH-REFRESH-1', expires=3600):
    return (200, {'access_token': access, 'refresh_token': refresh,
                  'expires_in': expires, 'token_type': 'bearer'})


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='yazio-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = {'HOME': str(base / 'home'), 'XDG_CONFIG_HOME': str(base / 'home/.config'),
               'COACH_HOME': str(base / 'coach'), 'TMPDIR': str(base / 'tmp')}
        for v in env.values():
            Path(v).mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('YAZIO_CONFIG', None)
        # Any attempt to build the real network transport is a test failure.
        guard = mock.patch.object(ny, 'HTTPSTransport', side_effect=AssertionError('real network'))
        guard.start()
        self.addCleanup(guard.stop)
        self.cfg_dir = Path(env['XDG_CONFIG_HOME']) / 'coach-claudio'

    def write_config(self, extra=None, drop=(), mode=0o600, dirmode=0o700):
        cfg = {'client_id': 'synthetic-client', 'client_secret': 'SYNTH-CLIENT-SECRET',
               'user_agent': UA, 'timezone': 'Europe/Berlin',
               'search': {'sex': 'female', 'countries': 'DE', 'locales': 'de_DE'},
               'product': {'category': 'synthetic-category', 'base_units': ['g', 'ml']}}
        cfg.update(extra or {})
        for k in drop:
            cfg.pop(k)
        self.cfg_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.cfg_dir, dirmode)
        path = self.cfg_dir / 'yazio.json'
        path.write_text(json.dumps(cfg))
        os.chmod(path, mode)
        return path


class Unconfigured(Isolated):
    def test_default_path_is_xdg_config(self):
        self.assertEqual(ny.config_path(), self.cfg_dir / 'yazio.json')

    def test_status_fails_closed_without_config(self):
        st = ny.status()
        self.assertFalse(st['configured'])
        self.assertFalse(st['can_write'])
        with self.assertRaises(ny.NotConfigured):
            ny.Client.load()



class SecureConfig(Isolated):
    def test_loads_private_config(self):
        self.write_config()
        cfg = ny.load_config()
        self.assertEqual(cfg.user_agent, UA)

    def test_rejects_group_or_world_readable_file(self):
        self.write_config(mode=0o644)
        with self.assertRaisesRegex(ny.InsecureConfig, '0600'):
            ny.load_config()

    def test_rejects_open_directory(self):
        self.write_config(dirmode=0o755)
        with self.assertRaisesRegex(ny.InsecureConfig, '0700'):
            ny.load_config()

    def test_rejects_symlinked_config(self):
        real = self.write_config()
        os.rename(real, self.cfg_dir / 'real.json')
        os.symlink(self.cfg_dir / 'real.json', real)
        with self.assertRaisesRegex(ny.InsecureConfig, 'symlink'):
            ny.load_config()

    def test_rejects_config_inside_the_repository(self):
        os.environ['YAZIO_CONFIG'] = str(ROOT / 'yazio.json')
        with self.assertRaisesRegex(ny.InsecureConfig, 'outside'):
            ny.load_config()

    def test_rejects_config_inside_coach_home(self):
        inside = Path(os.environ['COACH_HOME']) / 'secrets'
        inside.mkdir(mode=0o700)
        os.environ['YAZIO_CONFIG'] = str(inside / 'yazio.json')
        with self.assertRaisesRegex(ny.InsecureConfig, 'outside'):
            ny.load_config()

    def test_requires_client_credentials_and_user_agent(self):
        for key in ('client_id', 'client_secret', 'user_agent'):
            self.write_config(drop=(key,))
            with self.assertRaisesRegex(ny.NotConfigured, key):
                ny.load_config()

    def test_rejects_header_injection_in_user_agent(self):
        self.write_config({'user_agent': UA + '\r\nX-Evil: 1'})
        with self.assertRaisesRegex(ny.NotConfigured, 'user_agent'):
            ny.load_config()

    def test_rejects_unknown_timezone(self):
        self.write_config({'timezone': 'Mars/Olympus'})
        with self.assertRaisesRegex(ny.NotConfigured, 'timezone'):
            ny.load_config()

    def test_rejects_nan_in_config(self):
        path = self.write_config()
        path.write_text(path.read_text()[:-1] + ', "timeout_s": NaN}')
        with self.assertRaises(ny.NotConfigured):
            ny.load_config()

    def test_secret_never_in_repr(self):
        self.write_config()
        cfg = ny.load_config()
        self.assertNotIn('SYNTH-CLIENT-SECRET', repr(cfg))
        self.assertNotIn('SYNTH-CLIENT-SECRET', str(cfg))



class Login(Isolated):
    def setUp(self):
        super().setUp()
        self.write_config()

    def test_password_grant_persists_refresh_token_not_password(self):
        t = FakeTransport(token_response())
        client = ny.Client.load(transport=t)
        client.login('runner@example.invalid', 'SYNTH-PASSWORD')
        call = t.calls[0]
        self.assertEqual(call['method'], 'POST')
        self.assertEqual(call['url'], 'https://yzapi.yazio.com/v22/oauth/token')
        self.assertEqual(call['body']['grant_type'], 'password')
        self.assertEqual(call['body']['username'], 'runner@example.invalid')
        self.assertEqual(call['headers']['User-Agent'], UA)
        self.assertIsInstance(call['timeout'], (int, float))
        tok = self.cfg_dir / 'yazio-token.json'
        self.assertEqual(tok.stat().st_mode & 0o777, 0o600)
        text = tok.read_text()
        self.assertIn('SYNTH-REFRESH-1', text)
        self.assertNotIn('SYNTH-PASSWORD', text)
        self.assertNotIn('runner@example.invalid', text)
        self.assertNotIn('SYNTH-PASSWORD', repr(client))

    def test_login_error_is_sanitized(self):
        echo = {'error': 'invalid_grant', 'message': 'bad SYNTH-PASSWORD for SYNTH-CLIENT-SECRET'}
        client = ny.Client.load(transport=FakeTransport((400, echo)))
        with self.assertRaises(ny.AuthError) as e:
            client.login('u@example.invalid', 'SYNTH-PASSWORD')
        msg = str(e.exception)
        self.assertIn('invalid_grant', msg)
        self.assertNotIn('SYNTH', msg)
        self.assertFalse((self.cfg_dir / 'yazio-token.json').exists())

    def test_non_finite_json_in_response_is_rejected(self):
        bad = (200, b'{"access_token":"a","refresh_token":"r","expires_in":NaN}')
        client = ny.Client.load(transport=FakeTransport(bad))
        with self.assertRaises(ny.ProtocolError):
            client.login('u@example.invalid', 'SYNTH-PASSWORD')
        self.assertFalse((self.cfg_dir / 'yazio-token.json').exists())

    def test_redirect_is_not_followed(self):
        t = FakeTransport((302, None, {'Location': 'https://evil.example/steal'}))
        client = ny.Client.load(transport=t)
        with self.assertRaises(ny.YazioHTTPError):
            client.login('u@example.invalid', 'SYNTH-PASSWORD')
        self.assertEqual(len(t.calls), 1)

    def test_only_the_fixed_https_host_is_allowed(self):
        self.assertEqual(ny.checked_url('https://yzapi.yazio.com/v22/x'), ('yzapi.yazio.com', '/v22/x'))
        for url in ('http://yzapi.yazio.com/v22/x', 'https://evil.example/v22/x',
                    'https://yzapi.yazio.com.evil.example/v22', 'https://u@yzapi.yazio.com/v22',
                    'https://yzapi.yazio.com:8443/v22'):
            with self.assertRaises(ny.YazioError, msg=url):
                ny.checked_url(url)

    def test_version_block_stops_with_clear_error(self):
        client = ny.Client.load(transport=FakeTransport((403, {'error': 'version_blocked'})))
        with self.assertRaisesRegex(ny.VersionBlocked, 'user_agent'):
            client.login('u@example.invalid', 'SYNTH-PASSWORD')



class Refresh(Isolated):
    def setUp(self):
        super().setUp()
        self.write_config()
        self.now = 1_800_000_000.0

    def store_token(self, expires_at, access='SYNTH-ACCESS-0', refresh='SYNTH-REFRESH-0', mode=0o600):
        path = self.cfg_dir / 'yazio-token.json'
        path.write_text(json.dumps({'access_token': access, 'refresh_token': refresh,
                                    'expires_at': expires_at}))
        os.chmod(path, mode)
        return path

    def client(self, *responses):
        self.t = FakeTransport(*responses)
        return ny.Client.load(transport=self.t, clock=lambda: self.now)

    def test_valid_token_is_used_without_refresh(self):
        self.store_token(self.now + 3600)
        self.assertEqual(self.client().access_token(), 'SYNTH-ACCESS-0')
        self.assertEqual(self.t.calls, [])

    def test_expired_token_refreshes_and_persists_rotation(self):
        path = self.store_token(self.now + 10)  # inside the skew window
        c = self.client(token_response('SYNTH-ACCESS-2', 'SYNTH-REFRESH-2'))
        self.assertEqual(c.access_token(), 'SYNTH-ACCESS-2')
        body = self.t.calls[0]['body']
        self.assertEqual(body['grant_type'], 'refresh_token')
        self.assertEqual(body['refresh_token'], 'SYNTH-REFRESH-0')
        saved = json.loads(path.read_text())
        self.assertEqual(saved['refresh_token'], 'SYNTH-REFRESH-2')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rereads_token_rotated_by_another_process(self):
        self.store_token(self.now - 1)
        c = self.client()
        # Another process refreshed meanwhile; this client must not reuse the old grant.
        self.store_token(self.now + 3600, 'SYNTH-ACCESS-9', 'SYNTH-REFRESH-9')
        self.assertEqual(c.access_token(), 'SYNTH-ACCESS-9')
        self.assertEqual(self.t.calls, [])

    def test_rejected_refresh_stops_and_never_uses_a_password(self):
        path = self.store_token(self.now - 1)
        c = self.client((401, {'error': 'invalid_grant'}))
        with self.assertRaisesRegex(ny.AuthError, 'login'):
            c.access_token()
        self.assertEqual(len(self.t.calls), 1)
        self.assertNotIn('password', self.t.calls[0]['body'])
        self.assertTrue(json.loads(path.read_text()).get('revoked'))
        with self.assertRaises(ny.AuthError):  # fails fast, no second refresh attempt
            c.access_token()
        self.assertEqual(len(self.t.calls), 1)

    def test_concurrent_refreshers_refresh_once(self):
        import threading
        import time as _time
        self.store_token(self.now - 1)
        calls = []

        class Slow(FakeTransport):
            def request(inner, method, url, headers, body, timeout):
                calls.append(url)
                _time.sleep(0.2)
                return 200, {}, json.dumps(token_response('SYNTH-ACCESS-2', 'SYNTH-REFRESH-2')[1]).encode()

        got = []
        clients = [ny.Client.load(transport=Slow(), clock=lambda: self.now) for _ in range(2)]
        threads = [threading.Thread(target=lambda c=c: got.append(c.access_token())) for c in clients]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(got, ['SYNTH-ACCESS-2', 'SYNTH-ACCESS-2'])
        self.assertEqual(len(calls), 1)

    def test_refresh_without_response_keeps_old_token(self):
        path = self.store_token(self.now - 1)
        c = self.client(TimeoutError())
        with self.assertRaises(ny.TransportError):
            c.access_token()
        self.assertEqual(json.loads(path.read_text())['refresh_token'], 'SYNTH-REFRESH-0')

    def test_insecure_token_file_is_refused(self):
        self.store_token(self.now + 3600, mode=0o640)
        with self.assertRaises(ny.InsecureConfig):
            self.client().access_token()

    def test_no_token_means_not_logged_in(self):
        with self.assertRaisesRegex(ny.AuthError, 'login'):
            self.client().access_token()



def remote_product(**over):
    p = {'name': 'Synthetic Oat Drink', 'producer': 'Synthetic', 'base_unit': 'ml',
         'is_private': False, 'is_deleted': False, 'eans': ['0042000000017'],
         'servings': [], 'category': 'synthetic-category',
         'nutrients': {'energy.energy': 0.46, 'nutrient.protein': 0.01, 'nutrient.carb': 0.067,
                       'nutrient.fat': 0.015, 'nutrient.dietaryfiber': 0.008,
                       'nutrient.sugar': 0.04, 'nutrient.salt': 0.001}}
    p.update(over)
    return p


class LoggedIn(Isolated):
    def setUp(self):
        super().setUp()
        self.write_config()
        self.now = 1_800_000_000.0
        tok = self.cfg_dir / 'yazio-token.json'
        tok.write_text(json.dumps({'access_token': 'SYNTH-ACCESS-0', 'refresh_token': 'SYNTH-REFRESH-0',
                                   'expires_at': self.now + 3600}))
        os.chmod(tok, 0o600)
        self.slept = []

    def client(self, *responses):
        self.t = FakeTransport(*responses)
        return ny.Client.load(transport=self.t, clock=lambda: self.now, sleep=self.slept.append)

    def install_contract(self, **kw):
        rec = ny.contract_record(probe_results(**kw), recorded_at='2026-07-01T12:31:00+02:00')
        path = self.cfg_dir / 'yazio-contract.json'
        path.write_text(json.dumps(rec))
        os.chmod(path, 0o600)
        return path


class Reads(LoggedIn):
    def test_get_product_preserves_id_and_maps_researched_keys(self):
        c = self.client((200, remote_product()))
        p = c.get_product(P_ID)
        call = self.t.calls[0]
        self.assertEqual(call['url'], f'https://yzapi.yazio.com/v22/products/{P_ID}')
        self.assertEqual(call['headers']['Authorization'], 'Bearer SYNTH-ACCESS-0')
        self.assertEqual(p['id'], P_ID)
        self.assertEqual(p['base_unit'], 'ml')
        self.assertEqual(p['per_unit']['energy_kcal'], 0.46)
        self.assertEqual(p['per_unit']['carbohydrate_g'], 0.067)
        self.assertEqual(p['per_unit']['fiber_g'], 0.008)
        self.assertNotIn('salt_g', p['per_unit'])  # product salt scaling is unverified
        self.assertTrue(any('salt' in w for w in p['warnings']))
        self.assertEqual(p['eans'], ['0042000000017'])

    def test_product_id_must_be_a_uuid(self):
        c = self.client()
        for bad in ('../user/products', P_ID + '?x=1', 'abc', 42):
            with self.assertRaises(ValueError):
                c.get_product(bad)
        self.assertEqual(self.t.calls, [])

    def test_search_uses_configured_params_and_keeps_leading_zeros(self):
        c = self.client((200, [{'product_id': P_ID, 'name': 'X', 'base_unit': 'g'}]))
        hits = c.search('0042000000017')
        url = self.t.calls[0]['url']
        self.assertIn('query=0042000000017', url)
        self.assertIn('sex=female', url)
        self.assertIn('countries=DE', url)
        self.assertIn('locales=de_DE', url)
        self.assertEqual(hits[0]['product_id'], P_ID)

    def test_search_refuses_numeric_barcodes(self):
        with self.assertRaises(TypeError):
            self.client().search(42000000017)

    def test_search_without_configured_sex_does_not_guess(self):
        self.write_config({'search': {'countries': 'DE'}})
        with self.assertRaisesRegex(ny.NotConfigured, 'sex'):
            self.client().search('Haferdrink')
        self.assertEqual(self.t.calls, [])

    def test_get_retries_transient_failures_with_backoff(self):
        c = self.client((503, None), TimeoutError(), (200, remote_product()))
        self.assertEqual(c.get_product(P_ID)['id'], P_ID)
        self.assertEqual(len(self.t.calls), 3)
        self.assertEqual(len(self.slept), 2)

    def test_get_gives_up_after_bounded_retries(self):
        c = self.client((503, None), (503, None), (503, None))
        with self.assertRaises(ny.YazioHTTPError):
            c.get_product(P_ID)
        self.assertEqual(len(self.t.calls), 3)

    def test_401_refreshes_once_then_retries(self):
        c = self.client((401, None), token_response('SYNTH-ACCESS-2', 'SYNTH-REFRESH-2'),
                        (200, remote_product()))
        c.get_product(P_ID)
        self.assertEqual(self.t.calls[2]['headers']['Authorization'], 'Bearer SYNTH-ACCESS-2')



OIL = {'energy_kcal': 884, 'protein_g': 0, 'carbohydrate_g': 0, 'sugars_g': 0,
       'fat_g': 100, 'fiber_g': None, 'salt_g': 0.01}


class ProductPayload(unittest.TestCase):
    def prep(self, nutrients=OIL, per='100g', **kw):
        args = dict(category='synthetic-category', base_units=('g', 'ml'))
        args.update(kw)
        return ny.prepare_product(per, nutrients, P_ID, 'Synthetic Oil', **args)

    def test_divides_per_100_by_100_and_keeps_unknown_out(self):
        r = self.prep()
        p = r['payload']
        self.assertEqual(p['id'], P_ID)
        self.assertEqual(p['base_unit'], 'g')
        self.assertIs(p['is_private'], True)
        self.assertEqual(p['category'], 'synthetic-category')
        self.assertEqual(p['nutrients']['energy.energy'], 8.84)
        self.assertEqual(p['nutrients']['nutrient.fat'], 1.0)
        self.assertEqual(p['nutrients']['nutrient.protein'], 0.0)   # a known zero stays zero
        self.assertNotIn('nutrient.dietaryfiber', p['nutrients'])   # unknown is not zero
        self.assertEqual(r['unknown'], ['fiber_g'])

    def test_salt_stays_local_until_mapping_is_confirmed(self):
        r = self.prep()
        self.assertNotIn('nutrient.salt', r['payload']['nutrients'])
        self.assertEqual(r['local_only'], ['salt_g'])
        self.assertTrue(any('salt' in w for w in r['warnings']))
        self.assertNotIn('salt_g', r['synced'])
        r = self.prep(salt_write=True)
        self.assertEqual(r['payload']['nutrients']['nutrient.salt'], 0.0001)
        self.assertIn('salt_g', r['synced'])

    def test_ml_basis_uses_ml_base_unit(self):
        r = self.prep({'energy_kcal': 46, 'carbohydrate_g': 6.7}, per='100ml')
        self.assertEqual(r['payload']['base_unit'], 'ml')
        self.assertEqual(r['payload']['nutrients']['nutrient.carb'], 0.067)

    def test_only_canonical_keys_no_kj_or_sodium_duplicates(self):
        for extra in ({'energy_kj': 3700}, {'sodium_mg': 4}, {'carbs': 1}):
            with self.assertRaises(ValueError, msg=extra):
                self.prep({**OIL, **extra})

    def test_rejects_non_per_100_bases(self):
        for per in ('serving', 'total', '100 g', None):
            with self.assertRaises(ValueError):
                self.prep(per=per)

    def test_rejects_bad_numbers(self):
        for bad in (-1, float('nan'), float('inf'), True, '8'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.prep({**OIL, 'protein_g': bad})
        with self.assertRaises(ValueError):
            self.prep({**OIL, 'energy_kcal': None})

    def test_deterministic_and_never_invents_an_id(self):
        a, b = self.prep(), self.prep()
        self.assertEqual(a['payload'], b['payload'])
        self.assertEqual(a['fingerprint'], b['fingerprint'])
        with self.assertRaises(ValueError):
            ny.prepare_product('100g', OIL, None, 'X', category='c', base_units=('g',))

    def test_unconfigured_base_unit_or_category_fails_closed(self):
        with self.assertRaises(ny.NotConfigured):
            self.prep(per='100ml', base_units=('g',))
        with self.assertRaises(ny.NotConfigured):
            self.prep(category=None)


class ClientPayload(LoggedIn):
    def test_client_takes_category_from_config_never_a_default(self):
        self.write_config({'product': {'base_units': ['g']}})
        with self.assertRaisesRegex(ny.NotConfigured, 'category'):
            self.client().prepare_product('100g', OIL, P_ID, 'Synthetic Oil')



def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


class ConsumedPayload(unittest.TestCase):
    def prep(self, when=None, **kw):
        args = dict(amount=125, unit='g', daytime='lunch', tz='Europe/Berlin', base_unit='g')
        args.update(kw)
        return ny.prepare_consumed(C_ID, P_ID, when=when or utc(2026, 7, 1, 10, 30), **args)

    def test_batch_body_with_stable_ids_and_local_time(self):
        r = self.prep()
        self.assertEqual(r['payload'], {
            'products': [{'id': C_ID, 'product_id': P_ID, 'amount': 125.0, 'daytime': 'lunch',
                          'date': '2026-07-01 12:30:00', 'serving': None,
                          'serving_quantity': None}],
            'recipe_portions': [], 'simple_products': []})
        self.assertEqual(r['local_time'], '2026-07-01T12:30:00+02:00')

    def test_winter_offset_and_ambiguous_dst_hour_warns(self):
        self.assertEqual(self.prep(utc(2026, 12, 1, 11, 30))['payload']['products'][0]['date'],
                         '2026-12-01 12:30:00')
        a = self.prep(utc(2026, 10, 25, 0, 30))   # 02:30 CEST
        b = self.prep(utc(2026, 10, 25, 1, 30))   # 02:30 CET, same wall clock
        self.assertEqual(a['payload']['products'][0]['date'], b['payload']['products'][0]['date'])
        self.assertTrue(any('ambiguous' in w for w in b['warnings']))
        self.assertTrue(any('ambiguous' in w for w in a['warnings']))
        self.assertEqual(self.prep()['warnings'], [])

    def test_naive_time_is_refused(self):
        with self.assertRaises(ValueError):
            self.prep(datetime(2026, 7, 1, 12, 30))

    def test_amount_unit_must_match_product_base_unit(self):
        with self.assertRaises(ValueError):
            self.prep(unit='ml')
        for bad in (0, -5, float('nan'), True, 20001):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.prep(amount=bad)

    def test_daytime_and_ids_are_validated(self):
        with self.assertRaises(ValueError):
            self.prep(daytime='brunch')
        with self.assertRaises(ValueError):
            ny.prepare_consumed(None, P_ID, 1, 'g', utc(2026, 7, 1), 'snack',
                                tz='Europe/Berlin', base_unit='g')


class ClientConsumedPayload(LoggedIn):
    def test_timezone_must_be_configured(self):
        self.write_config(drop=('timezone',))
        with self.assertRaisesRegex(ny.NotConfigured, 'timezone'):
            self.client().prepare_consumed(C_ID, P_ID, 10, 'g', utc(2026, 7, 1), 'snack', 'g')



ALL7 = ['energy_kcal', 'protein_g', 'carbohydrate_g', 'fat_g', 'fiber_g', 'sugars_g', 'salt_g']


def probe_results(salt_display=False, nutrients=ALL7, base_unit='g', category='synthetic-category'):
    """What a successful verify-write run observes (synthetic)."""
    return {'user_agent': UA, 'category': category, 'base_unit': base_unit,
            'product_created': True, 'product_nutrients_roundtrip': list(nutrients),
            'salt_display_confirmed_by_user': salt_display,
            'consumed_created': True, 'consumed_fields_roundtrip':
                ['id', 'product_id', 'amount', 'daytime', 'date'],
            'consumed_deleted': True, 'product_deleted': True,
            'date_sent': '2026-07-01 12:30:00', 'date_read': '2026-07-01 12:30:00'}


class Capabilities(LoggedIn):
    def test_login_invalidates_contract_and_archives_abandoned_cleanup(self):
        self.install_contract()
        pending = {'product_id': P_ID, 'consumed_id': C_ID, 'when': '2026-07-01T12:30:00+02:00'}
        ny.write_private_json(self.cfg_dir / 'yazio-probe.json', pending)
        c = self.client(token_response())
        c.login('account-b@example.invalid', 'SYNTH-PASSWORD')
        self.assertFalse(ny.status()['can_write'])
        self.assertFalse((self.cfg_dir / 'yazio-probe.json').exists())
        reports = list(self.cfg_dir.glob('yazio-probe-abandoned-*.json'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())['pending_probe'], pending)
        self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ny.ContractUnverified):
            c.delete_consumed(C_ID, '2026-07-01')
        self.assertEqual([x['method'] for x in self.t.calls], ['POST'])

    def test_without_contract_everything_is_researched_and_writes_refused(self):
        c = self.client()
        caps = c.capabilities()
        self.assertEqual(caps['create_consumed'], 'researched')
        self.assertEqual(caps['nutrients']['salt_g'], 'unverified')
        self.assertEqual(caps['nutrients']['energy_kcal'], 'researched')
        payload = ny.prepare_consumed(C_ID, P_ID, 10, 'g', utc(2026, 7, 1), 'snack',
                                      tz='Europe/Berlin', base_unit='g')['payload']
        for call in (lambda: c.create_consumed(payload),
                     lambda: c.delete_consumed(C_ID, '2026-07-01'),
                     lambda: c.create_private_product({'id': P_ID})):
            with self.assertRaises(ny.ContractUnverified):
                call()
        self.assertEqual(self.t.calls, [])

    def test_config_toggle_is_not_proof(self):
        self.write_config({'verified': True, 'live_confirmed': True,
                           'contract': {'salt': True}})
        self.assertEqual(self.client().capabilities()['create_consumed'], 'researched')

    def test_hand_edited_contract_is_not_proof(self):
        path = self.install_contract()
        rec = json.loads(path.read_text())
        rec['salt']['display_confirmed_by_user'] = True   # flipped without a probe
        path.write_text(json.dumps(rec))
        self.assertEqual(self.client().capabilities()['create_consumed'], 'researched')

    def test_recorded_contract_marks_live_confirmed(self):
        self.install_contract()
        caps = self.client().capabilities()
        self.assertEqual(caps['create_consumed'], 'live_confirmed')
        self.assertEqual(caps['delete_consumed'], 'live_confirmed')
        self.assertEqual(caps['create_private_product'], 'live_confirmed')
        self.assertEqual(caps['nutrients']['fiber_g'], 'live_confirmed')
        # Salt round-tripped but nobody confirmed what the app shows: still not mapped.
        self.assertEqual(caps['nutrients']['salt_g'], 'unverified')
        self.assertFalse(self.client().salt_confirmed())

    def test_salt_needs_roundtrip_and_user_confirmed_display(self):
        self.install_contract(salt_display=True)
        self.assertEqual(self.client().capabilities()['nutrients']['salt_g'], 'live_confirmed')
        self.assertTrue(self.client().salt_confirmed())

    def test_nutrient_not_accepted_remotely_stays_local(self):
        self.install_contract(nutrients=[k for k in ALL7 if k != 'sugars_g'])
        caps = self.client().capabilities()
        self.assertEqual(caps['nutrients']['sugars_g'], 'not_accepted')
        r = self.client().prepare_product('100g', OIL, P_ID, 'Synthetic Oil')
        self.assertNotIn('nutrient.sugar', r['payload']['nutrients'])
        self.assertIn('sugars_g', r['local_only'])

    def test_contract_for_other_category_or_unit_does_not_apply(self):
        self.install_contract(category='other-category')
        self.assertEqual(self.client().capabilities()['create_private_product'], 'researched')

    def test_status_reports_without_network_or_secrets(self):
        self.install_contract()
        st = ny.status()
        self.assertTrue(st['configured'])
        self.assertTrue(st['logged_in'])
        self.assertTrue(st['can_write'])
        self.assertEqual(st['capabilities']['create_consumed'], 'live_confirmed')
        self.assertNotIn('SYNTH', json.dumps(st))



def as_remote(payload, **over):
    """What GET /v22/products/{id} would return for a stored draft."""
    r = {k: v for k, v in payload.items() if k != 'id'}
    r.update({'is_deleted': False, 'eans': []})
    r.update(over)
    return r


class CreateProduct(LoggedIn):
    def setUp(self):
        super().setUp()
        self.install_contract()
        self.draft = ny.prepare_product('100g', OIL, P_ID, 'Synthetic Oil', category='synthetic-category',
                                        base_units=('g',))['payload']

    def test_create_reads_back_list_and_product(self):
        c = self.client((200, []), (204, None), (200, [P_ID]), (200, as_remote(self.draft)))
        r = c.create_private_product(self.draft)
        self.assertEqual(r['status'], 'confirmed')
        self.assertEqual(r['id'], P_ID)
        self.assertEqual(sorted(r['verified_nutrients']),
                         sorted(['energy_kcal', 'protein_g', 'carbohydrate_g', 'sugars_g', 'fat_g']))
        methods = [(x['method'], x['url'].split('/v22')[1]) for x in self.t.calls]
        self.assertEqual(methods, [('GET', '/user/products'), ('POST', '/user/products'),
                                   ('GET', '/user/products'), ('GET', f'/products/{P_ID}')])
        self.assertEqual(self.t.calls[1]['body'], self.draft)

    def test_204_without_the_product_in_readback_is_not_success(self):
        c = self.client((200, []), (204, None), (200, []))
        with self.assertRaises(ny.ReadbackMismatch):
            c.create_private_product(self.draft)

    def test_readback_with_different_nutrient_is_mismatch(self):
        bad = as_remote(self.draft)
        bad['nutrients'] = {**bad['nutrients'], 'energy.energy': 884}
        c = self.client((200, []), (204, None), (200, [P_ID]), (200, bad))
        with self.assertRaisesRegex(ny.ReadbackMismatch, 'energy_kcal'):
            c.create_private_product(self.draft)

    def test_remote_zero_for_an_unknown_nutrient_is_flagged(self):
        remote = as_remote(self.draft)
        remote['nutrients'] = {**remote['nutrients'], 'nutrient.dietaryfiber': 0}
        c = self.client((200, []), (204, None), (200, [P_ID]), (200, remote))
        r = c.create_private_product(self.draft)
        self.assertTrue(any('fiber_g' in w for w in r['warnings']))

    def test_timeout_after_send_is_unknown_and_never_repeated(self):
        c = self.client((200, []), TimeoutError())
        with self.assertRaises(ny.UnknownOutcome) as e:
            c.create_private_product(self.draft)
        self.assertEqual(e.exception.id, P_ID)
        self.assertEqual([x['method'] for x in self.t.calls], ['GET', 'POST'])

    def test_server_error_after_send_is_unknown(self):
        c = self.client((200, []), (502, None))
        with self.assertRaises(ny.UnknownOutcome):
            c.create_private_product(self.draft)
        self.assertEqual(len(self.t.calls), 2)

    def test_existing_identical_product_is_not_created_again(self):
        c = self.client((200, [P_ID]), (200, as_remote(self.draft)))
        r = c.create_private_product(self.draft)
        self.assertEqual(r['status'], 'confirmed')
        self.assertTrue(r['already_present'])
        self.assertNotIn('POST', [x['method'] for x in self.t.calls])

    def test_existing_different_product_with_same_id_is_mismatch(self):
        c = self.client((200, [P_ID]), (200, as_remote(self.draft, name='Other')))
        with self.assertRaises(ny.ReadbackMismatch):
            c.create_private_product(self.draft)
        self.assertNotIn('POST', [x['method'] for x in self.t.calls])

    def test_reconcile_is_read_only(self):
        c = self.client((200, []))
        self.assertEqual(c.reconcile_product(self.draft)['status'], 'absent')
        c = self.client((200, [P_ID]), (200, as_remote(self.draft)))
        self.assertEqual(c.reconcile_product(self.draft)['status'], 'confirmed')
        self.assertEqual({x['method'] for x in self.t.calls}, {'GET'})

    def test_base_unit_outside_recorded_contract_is_refused(self):
        draft = ny.prepare_product('100ml', OIL, P_ID, 'X', category='synthetic-category',
                                   base_units=('ml',))['payload']
        with self.assertRaises(ny.ContractUnverified):
            self.client().create_private_product(draft)



def day_list(*items):
    return {'products': [dict(i, type='product') for i in items],
            'recipe_portions': [], 'simple_products': []}


class MalformedConsumed(LoggedIn):
    def test_every_bucket_requires_an_explicit_list(self):
        for bucket in day_list():
            for value in (None, False, 0, '', {}):
                with self.subTest(bucket=bucket, value=value):
                    c = self.client((200, {**day_list(), bucket: value}))
                    with self.assertRaises(ny.ProtocolError):
                        c.get_consumed('2026-07-01')
            data = day_list()
            del data[bucket]
            with self.assertRaises(ny.ProtocolError):
                self.client((200, data)).get_consumed('2026-07-01')

    def test_malformed_day_never_permits_create_or_confirms_delete(self):
        self.install_contract()
        payload = ny.prepare_consumed(C_ID, P_ID, 1, 'g', utc(2026, 7, 1), 'snack',
                                      tz='Europe/Berlin', base_unit='g')['payload']
        with self.assertRaises(ny.ProtocolError):
            self.client((200, {})).create_consumed(payload)
        self.assertEqual([c['method'] for c in self.t.calls], ['GET'])
        with self.assertRaises(ny.UnknownOutcome):
            self.client((200, day_list(payload['products'][0])), (204, None),
                        (200, {'products': None})).delete_consumed(C_ID, '2026-07-01')


class Consumed(LoggedIn):
    def setUp(self):
        super().setUp()
        self.install_contract()
        r = ny.prepare_consumed(C_ID, P_ID, 125, 'g', utc(2026, 7, 1, 10, 30), 'lunch',
                                tz='Europe/Berlin', base_unit='g')
        self.payload, self.item = r['payload'], r['payload']['products'][0]

    def test_get_consumed_reads_one_day(self):
        c = self.client((200, day_list(self.item)))
        day = c.get_consumed('2026-07-01')
        self.assertTrue(self.t.calls[0]['url'].endswith('/v22/user/consumed-items?date=2026-07-01'))
        self.assertEqual(day['products'][0]['id'], C_ID)
        with self.assertRaises(ValueError):
            c.get_consumed('2026-7-1')

    def test_create_confirms_exact_readback(self):
        c = self.client((200, day_list()), (204, None), (200, day_list(self.item)))
        r = c.create_consumed(self.payload)
        self.assertEqual(r['status'], 'confirmed')
        self.assertEqual(r['day'], '2026-07-01')
        self.assertEqual(self.t.calls[1]['method'], 'POST')
        self.assertEqual(self.t.calls[1]['body'], self.payload)
        self.assertNotIn('type', self.t.calls[1]['body']['products'][0])

    def test_204_alone_is_not_success(self):
        c = self.client((200, day_list()), (204, None), (200, day_list()))
        with self.assertRaises(ny.ReadbackMismatch):
            c.create_consumed(self.payload)

    def test_readback_with_other_amount_or_time_is_mismatch(self):
        for field, value in (('amount', 12.5), ('date', '2026-07-01 10:30:00'),
                             ('daytime', 'dinner'), ('product_id', C_ID)):
            c = self.client((200, day_list()), (204, None), (200, day_list({**self.item, field: value})))
            with self.assertRaisesRegex(ny.ReadbackMismatch, field):
                c.create_consumed(self.payload)

    def test_base_unit_readback_requires_explicit_null_serving_fields(self):
        for field in ('serving', 'serving_quantity'):
            for value in ('portion', 1, 0, False, {}):
                with self.subTest(field=field, value=value):
                    remote = {**self.item, field: value}
                    with self.assertRaisesRegex(ny.ReadbackMismatch, field):
                        self.client((200, day_list(remote))).create_consumed(self.payload)
            remote = dict(self.item)
            del remote[field]
            with self.assertRaisesRegex(ny.ReadbackMismatch, field):
                self.client((200, day_list(remote))).create_consumed(self.payload)

    def test_timeout_after_send_is_unknown_no_retry(self):
        c = self.client((200, day_list()), TimeoutError())
        with self.assertRaises(ny.UnknownOutcome) as e:
            c.create_consumed(self.payload)
        self.assertEqual((e.exception.id, e.exception.day), (C_ID, '2026-07-01'))
        self.assertEqual([x['method'] for x in self.t.calls], ['GET', 'POST'])

    def test_unparseable_success_body_after_send_is_unknown(self):
        c = self.client((200, day_list()), (200, b'<html>'))
        with self.assertRaises(ny.UnknownOutcome):
            c.create_consumed(self.payload)

    def test_failed_readback_after_send_is_unknown(self):
        c = self.client((200, day_list()), (204, None), TimeoutError(), TimeoutError(), TimeoutError())
        with self.assertRaises(ny.UnknownOutcome):
            c.create_consumed(self.payload)
        self.assertEqual([x['method'] for x in self.t.calls].count('POST'), 1)

    def test_resend_with_same_id_reconciles_instead_of_duplicating(self):
        c = self.client((200, day_list(self.item)))
        r = c.create_consumed(self.payload)
        self.assertTrue(r['already_present'])
        self.assertEqual([x['method'] for x in self.t.calls], ['GET'])

    def test_same_id_with_different_content_is_mismatch_not_overwrite(self):
        c = self.client((200, day_list({**self.item, 'amount': 50})))
        with self.assertRaises(ny.ReadbackMismatch):
            c.create_consumed(self.payload)
        self.assertEqual([x['method'] for x in self.t.calls], ['GET'])

    def test_only_one_product_entry_per_call(self):
        two = {**self.payload, 'products': [self.item, {**self.item, 'id': P_ID}]}
        with self.assertRaises(ValueError):
            self.client().create_consumed(two)

    def test_delete_sends_bucket_object_and_checks_absence(self):
        c = self.client((200, day_list(self.item)), (204, None), (200, day_list()))
        r = c.delete_consumed(C_ID, '2026-07-01')
        self.assertEqual(r['status'], 'deleted')
        call = self.t.calls[1]
        self.assertEqual(call['method'], 'DELETE')
        self.assertEqual(call['body'], {'products': C_ID})   # a string, not an array
        self.assertNotIn('?', call['url'])

    def test_delete_204_but_still_present_is_mismatch(self):
        c = self.client((200, day_list(self.item)), (204, None), (200, day_list(self.item)))
        with self.assertRaisesRegex(ny.ReadbackMismatch, 'still present'):
            c.delete_consumed(C_ID, '2026-07-01')

    def test_delete_of_absent_entry_sends_nothing(self):
        c = self.client((200, day_list()))
        self.assertEqual(c.delete_consumed(C_ID, '2026-07-01')['status'], 'absent')
        self.assertEqual([x['method'] for x in self.t.calls], ['GET'])

    def test_delete_timeout_is_unknown(self):
        c = self.client((200, day_list(self.item)), TimeoutError())
        with self.assertRaises(ny.UnknownOutcome):
            c.delete_consumed(C_ID, '2026-07-01')
        self.assertEqual(len(self.t.calls), 2)



OAT = {'energy_kcal': 46, 'protein_g': 1.0, 'carbohydrate_g': 6.7, 'sugars_g': 4.0,
       'fat_g': 1.5, 'fiber_g': 0.8, 'salt_g': 0.1}


class Branded(LoggedIn):
    BARCODE = '0042000000017'

    def hit(self):
        return (200, [{'product_id': P_ID, 'name': 'Synthetic Oat Drink', 'base_unit': 'ml'}])

    def test_exact_ean_and_all_known_nutrients_match(self):
        c = self.client(self.hit(), (200, remote_product()))
        r = c.find_branded(self.BARCODE, '100ml', OAT)
        self.assertEqual(r['status'], 'match')
        self.assertEqual(r['product_id'], P_ID)
        self.assertEqual(r['not_compared'], ['salt_g'])   # salt scale unverified

    def test_ean_without_leading_zero_is_not_the_same_product(self):
        c = self.client(self.hit(), (200, remote_product(eans=['42000000017'])))
        self.assertEqual(c.find_branded(self.BARCODE, '100ml', OAT)['status'], 'none')

    def test_one_nutrient_off_rejects_the_match(self):
        c = self.client(self.hit(), (200, remote_product()))
        r = c.find_branded(self.BARCODE, '100ml', {**OAT, 'sugars_g': 6.0})
        self.assertEqual(r['status'], 'none')
        self.assertIn('sugars_g', ' '.join(r['reasons']))

    def test_remote_missing_a_locally_known_nutrient_rejects(self):
        prod = remote_product()
        del prod['nutrients']['nutrient.dietaryfiber']
        c = self.client(self.hit(), (200, prod))
        self.assertEqual(c.find_branded(self.BARCODE, '100ml', OAT)['status'], 'none')

    def test_other_base_unit_rejects(self):
        c = self.client(self.hit(), (200, remote_product(base_unit='g')))
        self.assertEqual(c.find_branded(self.BARCODE, '100ml', OAT)['status'], 'none')

    def test_non_canonical_keys_are_refused_even_when_empty(self):
        with self.assertRaises(ValueError):
            self.client().find_branded(self.BARCODE, '100ml', {**OAT, 'sodium_mg': None})

    def test_barcode_must_be_digit_text(self):
        for bad in (42000000017, '42-00', ''):
            with self.assertRaises((TypeError, ValueError)):
                self.client().find_branded(bad, '100ml', OAT)



class WriteProbe(LoggedIn):
    WHEN = utc(2026, 7, 1, 10, 30)

    def script(self, product_reply=None, consumed_reply=None, drop_consumed=False):
        """Fake server replies for one full probe, built from what was sent."""
        t = self.t = FakeTransport()
        # Keep remote artifacts across new clients/resumes within the test.
        sent = getattr(self, '_probe_remote', None)
        if sent is None:
            sent = self._probe_remote = {}

        def request(method, url, headers, body, timeout):
            t.calls.append({'method': method, 'url': url, 'body': json.loads(body) if body else None})
            path = url.split('/v22')[1]
            b = json.loads(body) if body else None
            if method == 'POST' and path == '/user/products':
                sent['product'] = b
                return 204, {}, b''
            if method == 'GET' and path == '/user/products':
                return 200, {}, json.dumps([sent['product']['id']] if 'product' in sent else []).encode()
            if method == 'GET' and path.startswith('/products/'):
                r = as_remote(sent['product'])
                if product_reply:
                    product_reply(r)
                return 200, {}, json.dumps(r).encode()
            if method == 'POST' and path == '/user/consumed-items':
                if consumed_reply and consumed_reply(b):
                    raise TimeoutError()
                if not drop_consumed:
                    sent['consumed'] = b['products'][0]
                return 204, {}, b''
            if method == 'GET' and path.startswith('/user/consumed-items'):
                items = [sent['consumed']] if 'consumed' in sent else []
                return 200, {}, json.dumps(day_list(*items)).encode()
            if method == 'DELETE' and path == '/user/consumed-items':
                sent.pop('consumed')
                return 204, {}, b''
            if method == 'DELETE' and path.startswith('/user/products/'):
                sent.pop('product')
                return 204, {}, b''
            raise AssertionError(f'unexpected {method} {path}')
        t.request = request
        return ny.Client.load(transport=t, clock=lambda: self.now, sleep=self.slept.append)

    def test_requires_explicit_consent_and_small_amount(self):
        c = self.client()
        with self.assertRaises(ny.ContractUnverified):
            c.run_write_probe(consent=False, amount=1, when=self.WHEN, salt_display=lambda q: True)
        with self.assertRaises(ValueError):
            c.run_write_probe(consent=True, amount=500, when=self.WHEN, salt_display=lambda q: True)
        self.assertEqual(self.t.calls, [])

    def test_salt_prompt_accepts_equivalent_milligrams(self):
        c = self.script()
        questions = []
        rec = c.run_write_probe(consent=True, amount=1, when=self.WHEN,
                                salt_display=lambda q: questions.append(q) or 'equivalently 340 mg' in q)
        self.assertIn('equivalently 340 mg', questions[0])
        self.assertTrue(rec['salt']['display_confirmed_by_user'])

    def test_full_probe_records_what_the_server_kept_and_cleans_up(self):
        c = self.script()
        questions = []
        rec = c.run_write_probe(consent=True, amount=1, when=self.WHEN,
                                salt_display=lambda q: questions.append(q) or False)
        self.assertEqual(rec['product']['nutrients_roundtrip'], sorted(ALL7))
        self.assertTrue(rec['consumed']['create_readback'])
        self.assertTrue(rec['consumed']['delete_readback'])
        self.assertTrue(rec['product']['delete_readback'])
        self.assertEqual(rec['consumed']['date_sent'], '2026-07-01 12:30:00')
        self.assertIn('0.34 g', questions[0])
        path = self.cfg_dir / 'yazio-contract.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.cfg_dir / 'yazio-probe.json').exists())
        caps = ny.Client.load(transport=FakeTransport()).capabilities()
        self.assertEqual(caps['create_consumed'], 'live_confirmed')
        self.assertEqual(caps['nutrients']['salt_g'], 'unverified')   # user said no
        posted = [x['body'] for x in self.t.calls if x['method'] == 'POST']
        self.assertEqual(posted[0]['nutrients']['nutrient.salt'], 0.0034)
        self.assertEqual(posted[1]['products'][0]['amount'], 1.0)
        self.assertNotIn('SYNTH', path.read_text())

    def test_nutrient_dropped_by_server_is_recorded_not_accepted(self):
        c = self.script(product_reply=lambda r: r['nutrients'].pop('nutrient.sugar'))
        rec = c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: True)
        self.assertNotIn('sugars_g', rec['product']['nutrients_roundtrip'])
        caps = ny.Client.load(transport=FakeTransport()).capabilities()
        self.assertEqual(caps['nutrients']['sugars_g'], 'not_accepted')
        self.assertEqual(caps['nutrients']['salt_g'], 'live_confirmed')

    def test_unknown_outcome_keeps_ids_and_resume_reuses_them(self):
        c = self.script(consumed_reply=lambda b: True)
        with self.assertRaises(ny.UnknownOutcome):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: False)
        self.assertFalse((self.cfg_dir / 'yazio-contract.json').exists())
        pending = json.loads((self.cfg_dir / 'yazio-probe.json').read_text())
        first_post = [x['body'] for x in self.t.calls if x['method'] == 'POST'][1]
        self.assertEqual(first_post['products'][0]['id'], pending['consumed_id'])
        c2 = self.script()
        c2.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: False)
        ids = [x['body']['products'][0]['id'] for x in self.t.calls
               if x['method'] == 'POST' and 'consumed' in x['url']]
        self.assertEqual(ids, [pending['consumed_id']])

    def test_resume_reuses_the_original_timestamp(self):
        c = self.script(consumed_reply=lambda b: True)
        with self.assertRaises(ny.UnknownOutcome):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: False)
        first = [x['body'] for x in self.t.calls if 'consumed' in x['url'] and x['method'] == 'POST'][0]
        c2 = self.script()
        c2.run_write_probe(consent=True, amount=1, when=self.WHEN + timedelta(hours=3),
                           salt_display=lambda q: False)
        again = [x['body'] for x in self.t.calls if 'consumed' in x['url'] and x['method'] == 'POST'][0]
        self.assertEqual(again, first)

    def test_204_for_consumed_without_readback_records_nothing(self):
        c = self.script(drop_consumed=True)
        with self.assertRaises(ny.ReadbackMismatch):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: True)
        self.assertFalse((self.cfg_dir / 'yazio-contract.json').exists())
        self.assertEqual(ny.status()['capabilities']['create_consumed'], 'researched')

    def test_structural_readback_mismatch_writes_no_contract(self):
        c = self.script(product_reply=lambda r: r.update(base_unit='ml'))
        with self.assertRaises(ny.ReadbackMismatch):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: True)
        self.assertFalse((self.cfg_dir / 'yazio-contract.json').exists())



class ProbeRecovery(LoggedIn):
    WHEN = utc(2026, 7, 1, 10, 30)

    def server(self, fail_at):
        self.remote = {'product': None, 'consumed': None}
        self.calls = []
        self.snapshots = []
        self.fail_at = fail_at

        def request(method, url, headers, body, timeout):
            path = url.split('/v22')[1].split('?')[0]
            data = json.loads(body) if body else None
            self.calls.append((method, path, data))
            if method != 'GET':
                self.snapshots.append(json.loads((self.cfg_dir / 'yazio-probe.json').read_text()))
            if method == 'POST':
                key = 'product' if path == '/user/products' else 'consumed'
                self.remote[key] = data if key == 'product' else data['products'][0]
            elif method == 'DELETE':
                key = 'consumed' if path == '/user/consumed-items' else 'product'
                if self.fail_at != 'product_stays' or key != 'product':
                    self.remote[key] = None
                if self.fail_at == key:
                    self.fail_at = None
                    raise TimeoutError()
            elif path == '/user/products':
                data = [self.remote['product']['id']] if self.remote['product'] else []
                return 200, {}, json.dumps(data).encode()
            elif path.startswith('/products/'):
                return 200, {}, json.dumps(as_remote(self.remote['product'])).encode()
            else:
                return 200, {}, json.dumps(day_list(*([self.remote['consumed']] if self.remote['consumed'] else []))).encode()
            return 204, {}, b''
        t = FakeTransport()
        t.request = request
        self.transport = t
        return ny.Client.load(transport=t, clock=lambda: self.now)

    def resume(self, amount=1):
        # New client, same remote: never reset state across recovery.
        return ny.Client.load(transport=self.transport, clock=lambda: self.now).run_write_probe(
            consent=True, amount=amount, when=self.WHEN + timedelta(hours=3), salt_display=lambda q: False)

    def test_lost_delete_response_resumes_cleanup_without_any_post(self):
        c = self.server('consumed')
        with self.assertRaises(ny.UnknownOutcome):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: False)
        first_calls = len(self.calls)
        self.resume()
        self.assertFalse(any(m == 'POST' for m, _, _ in self.calls[first_calls:]))
        self.assertTrue(all(s.get('product_payload') and s.get('consumed_payload') and s.get('phase')
                            for s in self.snapshots))
        self.assertIsNone(self.remote['product'])
        self.assertIsNone(self.remote['consumed'])

    def test_resume_changed_amount_rejected_before_remote_access(self):
        c = self.server('consumed')
        with self.assertRaises(ny.UnknownOutcome):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: False)
        count = len(self.calls)
        with self.assertRaisesRegex(ValueError, 'amount'):
            self.resume(amount=2)
        self.assertEqual(len(self.calls), count)
        self.resume()

    def test_product_delete_unconfirmed_retains_cleanup_state(self):
        c = self.server('product_stays')
        with self.assertRaises(ny.ReadbackMismatch):
            c.run_write_probe(consent=True, amount=1, when=self.WHEN, salt_display=lambda q: False)
        pending = json.loads((self.cfg_dir / 'yazio-probe.json').read_text())
        self.assertEqual(pending['phase'], 'cleanup_product')
        self.assertFalse((self.cfg_dir / 'yazio-contract.json').exists())
        count = len(self.calls)
        self.fail_at = None
        self.resume()
        self.assertFalse(any(m == 'POST' for m, _, _ in self.calls[count:]))


class CLI(Isolated):
    def run_cli(self, argv, transport=None, tty=True, inputs=(), secrets=()):
        out = io.StringIO()
        inputs, secrets = list(inputs), list(secrets)
        with mock.patch.object(ny, '_interactive', return_value=tty), \
                mock.patch.object(ny.time, 'sleep'), \
                mock.patch('builtins.input', side_effect=lambda prompt='': inputs.pop(0)), \
                mock.patch.object(ny.getpass, 'getpass', side_effect=lambda prompt='': secrets.pop(0)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = ny.main(argv, transport=transport)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_secret_flags_do_not_exist(self):
        for argv in (['login', '--password', 'SYNTH-PASSWORD'], ['login', '--token', 'x'],
                     ['setup', '--client-secret', 'x']):
            code, out = self.run_cli(argv)
            self.assertEqual(code, 2, argv)

    def test_status_unconfigured_points_to_local_terminal_setup(self):
        code, out = self.run_cli(['status'])
        self.assertIn('nutrition_yazio.py setup', out)
        self.assertIn('local terminal', out)
        self.assertIn('not configured', out)

    def test_setup_writes_private_config_without_inventing_values(self):
        code, out = self.run_cli(['setup'], inputs=[UA, 'Europe/Berlin', '', 'DE', '', '', 'g'],
                                 secrets=['synthetic-client', 'SYNTH-CLIENT-SECRET'])
        self.assertEqual(code, 0, out)
        path = self.cfg_dir / 'yazio.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cfg_dir.stat().st_mode & 0o777, 0o700)
        cfg = json.loads(path.read_text())
        self.assertNotIn('sex', cfg['search'])        # left blank: not guessed
        self.assertNotIn('category', cfg['product'])  # left blank: not invented
        self.assertNotIn('SYNTH-CLIENT-SECRET', out)
        self.assertIn('login', out)

    def test_login_help_and_output_describe_both_stored_tokens(self):
        code, out = self.run_cli(['--help'])
        self.assertEqual(code, 0)
        self.assertIn('access + refresh tokens', out)
        self.write_config()
        code, out = self.run_cli(['login'], transport=FakeTransport(token_response()),
                                 inputs=['u@example.invalid'], secrets=['SYNTH-PASSWORD'])
        self.assertEqual(code, 0)
        self.assertIn('access + refresh tokens', out)
        self.assertNotIn('only the refresh token', out)

    def test_login_refuses_without_a_terminal(self):
        self.write_config()
        code, out = self.run_cli(['login'], transport=FakeTransport(), tty=False)
        self.assertNotEqual(code, 0)
        self.assertIn('local terminal', out)

    def test_login_prompts_with_getpass_and_prints_no_secret(self):
        self.write_config()
        t = FakeTransport(token_response())
        code, out = self.run_cli(['login'], transport=t, inputs=['u@example.invalid'],
                                 secrets=['SYNTH-PASSWORD'])
        self.assertEqual(code, 0, out)
        self.assertEqual(t.calls[0]['body']['password'], 'SYNTH-PASSWORD')
        self.assertNotIn('SYNTH', out)
        self.assertTrue((self.cfg_dir / 'yazio-token.json').exists())

    def logged_in(self):
        self.write_config()
        tok = self.cfg_dir / 'yazio-token.json'
        tok.write_text(json.dumps({'access_token': 'SYNTH-ACCESS-0', 'refresh_token': 'SYNTH-REFRESH-0',
                                   'expires_at': 4_000_000_000}))
        os.chmod(tok, 0o600)

    def test_verify_is_read_only(self):
        self.logged_in()
        t = FakeTransport((200, [P_ID]), (200, day_list()))
        code, out = self.run_cli(['verify'], transport=t)
        self.assertEqual(code, 0, out)
        self.assertEqual({c['method'] for c in t.calls}, {'GET'})
        self.assertIn('read-only', out)
        self.assertNotIn('SYNTH', out)

    def test_verify_write_needs_flag_and_typed_confirmation(self):
        self.logged_in()
        t = FakeTransport()
        code, out = self.run_cli(['verify-write', '--amount', '1'], transport=t)
        self.assertNotEqual(code, 0)
        code, out = self.run_cli(['verify-write', '--i-consent-to-test-writes', '--amount', '1'],
                                 transport=t, inputs=['yes'])
        self.assertNotEqual(code, 0)
        self.assertIn('aborted', out)
        self.assertEqual(t.calls, [])

    def test_errors_are_printed_sanitized_with_nonzero_exit(self):
        self.logged_in()
        t = FakeTransport((500, {'error': 'boom', 'trace': 'SYNTH-ACCESS-0'}),
                          (500, None), (500, None))
        code, out = self.run_cli(['verify'], transport=t)
        self.assertNotEqual(code, 0)
        self.assertIn('HTTP 500', out)
        self.assertNotIn('SYNTH', out)
        self.assertNotIn('Traceback', out)



class RealTransportOffline(unittest.TestCase):
    """The stdlib transport, with http.client replaced: no socket is opened."""

    def fake_conn(self, status=200, body=b'{}', headers=()):
        made = []

        class Resp:
            def __init__(self):
                self.status = status
                self._body = body

            def getheaders(self):
                return list(headers)

            def read(self, n=-1):
                out, self._body = self._body[:n], self._body[n:]
                return out

        class Conn:
            def __init__(self, host, port=None, timeout=None, context=None):
                made.append({'host': host, 'port': port, 'timeout': timeout, 'context': context})
                self.sent = made[-1]

            def request(self, method, path, body=None, headers=None):
                self.sent.update(method=method, path=path, body=body, headers=headers)

            def getresponse(self):
                return Resp()

            def close(self):
                self.sent['closed'] = True
        return Conn, made

    def test_pins_host_tls_and_timeout(self):
        Conn, made = self.fake_conn()
        with mock.patch.object(ny.http.client, 'HTTPSConnection', Conn):
            status, _, body = REAL_TRANSPORT().request(
                'GET', 'https://yzapi.yazio.com/v22/user/products', {'A': 'b'}, None, 7)
        self.assertEqual((status, body), (200, b'{}'))
        self.assertEqual(made[0]['host'], 'yzapi.yazio.com')
        self.assertEqual(made[0]['port'], 443)
        self.assertEqual(made[0]['timeout'], 7)
        self.assertTrue(made[0]['context'].check_hostname)
        self.assertEqual(made[0]['path'], '/v22/user/products')
        self.assertTrue(made[0]['closed'])

    def test_refuses_foreign_url_before_connecting(self):
        Conn, made = self.fake_conn()
        with mock.patch.object(ny.http.client, 'HTTPSConnection', Conn):
            with self.assertRaises(ny.YazioError):
                REAL_TRANSPORT().request('GET', 'https://evil.example/v22/x', {}, None, 5)
        self.assertEqual(made, [])

    def test_redirect_status_is_returned_not_followed(self):
        Conn, made = self.fake_conn(302, b'', [('Location', 'https://evil.example/')])
        with mock.patch.object(ny.http.client, 'HTTPSConnection', Conn):
            status, headers, _ = REAL_TRANSPORT().request(
                'GET', 'https://yzapi.yazio.com/v22/user/products', {}, None, 5)
        self.assertEqual(status, 302)
        self.assertEqual(len(made), 1)

    def test_oversized_body_is_refused(self):
        Conn, made = self.fake_conn(200, b'x' * (ny.MAX_BODY + 1))
        with mock.patch.object(ny.http.client, 'HTTPSConnection', Conn):
            with self.assertRaises(ny.ProtocolError):
                REAL_TRANSPORT().request('GET', 'https://yzapi.yazio.com/v22/user/products',
                                         {}, None, 5)

    def test_requires_a_timeout(self):
        with self.assertRaises(ValueError):
            REAL_TRANSPORT().request('GET', 'https://yzapi.yazio.com/v22/x', {}, None, None)


if __name__ == '__main__':
    unittest.main()
