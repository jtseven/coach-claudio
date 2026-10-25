#!/usr/bin/env python3
"""Optional, unofficial YAZIO adapter. Standard library only.

YAZIO has no public API. The wire contract here comes from the MIT-licensed
community description yazio-community/yazio-api-specification v0.4.0 (API
/v22), which was derived from app traffic and is not official. Nothing in this
module has been confirmed against a live account until `verify-write` has run
for that account and recorded what the server actually accepted.

The local nutrition journal stays the source of truth. This adapter fails
closed: no config, insecure files, an unknown User-Agent, a revoked token or an
unverified write contract all stop it instead of guessing.

    nutrition_yazio.py setup          # local terminal only, prompts via getpass
    nutrition_yazio.py login
    nutrition_yazio.py status         # offline
    nutrition_yazio.py verify         # read-only account calls
    nutrition_yazio.py verify-write --i-consent-to-test-writes --amount 1
"""
import argparse
from datetime import date, datetime
from decimal import Decimal
import fcntl
import getpass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import sys
import time
from urllib.parse import urlencode, urlsplit
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = 'yzapi.yazio.com'
API = '/v22'
SPEC = 'yazio-community/yazio-api-specification v0.4.0'
TOKEN_SKEW_S = 120
CONTRACT_VERSION = 1
CONSUMED_FIELDS = ['amount', 'date', 'daytime', 'id', 'product_id']
# Distinct, non-zero per-100 g values so a dropped or rescaled key is visible.
PROBE_PER100 = {'energy_kcal': 123, 'protein_g': 4.5, 'carbohydrate_g': 6.7, 'sugars_g': 2.3,
                'fat_g': 8.9, 'fiber_g': 1.2, 'salt_g': 0.34}
PROBE_NAME = 'coach-claudio contract check (safe to delete)'
PROBE_MAX_AMOUNT = 100
GET_ATTEMPTS = 3
MAX_BODY = 5_000_000

# Local canonical key -> YAZIO /v22 product nutrient key, per ONE base unit
# (g or ml). Source: the pinned spec; "researched", not live-confirmed. Salt is
# documented for recipes only; for products its scale (g vs mg) is open, so it
# is never mapped until a recorded contract test confirmed it on the account.
NUTRIENT_KEYS = {
    'energy_kcal': 'energy.energy',
    'protein_g': 'nutrient.protein',
    'carbohydrate_g': 'nutrient.carb',
    'fat_g': 'nutrient.fat',
    'fiber_g': 'nutrient.dietaryfiber',
    'sugars_g': 'nutrient.sugar',
    'salt_g': 'nutrient.salt',
}
DAYTIMES = ('breakfast', 'lunch', 'dinner', 'snack')
MAX_AMOUNT = 20000
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_PARAM = re.compile(r'[A-Za-z][A-Za-z_,-]{0,39}')


class YazioError(Exception):
    """Base error. Messages never carry secrets or raw HTTP bodies."""


class NotConfigured(YazioError):
    pass


class InsecureConfig(YazioError):
    pass


class ContractUnverified(YazioError):
    """This write has not been confirmed on the account by `verify-write`."""


class UnknownOutcome(YazioError):
    """A write was sent but its effect is unknown (timeout, 5xx, failed
    read-back). Do not resend: reconcile with the same ids first."""

    def __init__(self, op, id, day=None, why=''):
        self.op, self.id, self.day = op, id, day
        super().__init__(f'{op} {id}: outcome UNKNOWN ({why}); not retried - '
                         'reconcile with the same id before any resend')


class ReadbackMismatch(YazioError):
    """Read-back after (or before) a write does not show what was sent."""

    def __init__(self, op, id, diffs):
        self.op, self.id, self.diffs = op, id, list(diffs)
        super().__init__(f'{op} {id}: YAZIO read-back differs: {", ".join(self.diffs)}')


class ProtocolError(YazioError):
    """Response was not the strict JSON shape the contract describes."""


class YazioHTTPError(YazioError):
    def __init__(self, status, code=None, what='request'):
        self.status, self.code = status, code
        super().__init__(f'YAZIO {what} failed: HTTP {status}' + (f' ({code})' if code else ''))


class VersionBlocked(YazioHTTPError):
    def __init__(self):
        super().__init__(403, 'version_blocked')
        self.args = ('YAZIO refused this app version: update user_agent in the config to the '
                     'User-Agent of a current YAZIO app, then retry. Not retrying blindly.',)


class AuthError(YazioError):
    """Login or refresh rejected. Needs a new local `login`; never falls back."""


class TransportError(YazioError):
    """No HTTP response (timeout, connection reset, TLS). Outcome unknown for writes."""


def checked_url(url):
    """Only https://yzapi.yazio.com on the default port, no userinfo."""
    u = urlsplit(url)
    if u.scheme != 'https' or u.hostname != HOST or u.port not in (None, 443) \
            or u.username or u.password or u.netloc != HOST or not u.path.startswith(API + '/'):
        raise YazioError('refusing a URL outside https://yzapi.yazio.com/v22')
    return u.hostname, u.path + (f'?{u.query}' if u.query else '')


def _reject_constant(name):
    raise ValueError(f'non-finite number {name} is not JSON')


def _finite_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise ValueError('non-finite number')
    return v


def strict_loads(text):
    """JSON with finite numbers only: NaN/Infinity and overflow are errors."""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def strict_dumps(obj):
    return json.dumps(obj, allow_nan=False, sort_keys=True, separators=(',', ':'))


def _check_private(path, want_dir):
    """Refuse symlinks, foreign owners and any group/other permission bits."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise NotConfigured(f'{path} does not exist; run: nutrition_yazio.py setup') from None
    if stat.S_ISLNK(st.st_mode):
        raise InsecureConfig(f'{path} is a symlink; use a regular private file')
    if want_dir != stat.S_ISDIR(st.st_mode) or (not want_dir and not stat.S_ISREG(st.st_mode)):
        raise InsecureConfig(f'{path} is not a regular {"directory" if want_dir else "file"}')
    if st.st_uid != os.getuid():
        raise InsecureConfig(f'{path} is not owned by the current user')
    if st.st_mode & 0o077:
        raise InsecureConfig(f'{path} is accessible by group/others; '
                             f'chmod {"0700" if want_dir else "0600"} it')


def _outside_repo(path):
    """Secrets never live in the repository or COACH_HOME (synced, backed up)."""
    resolved = Path(os.path.realpath(path))
    roots = [REPO_ROOT] + ([Path(os.path.realpath(os.environ['COACH_HOME']))]
                           if os.environ.get('COACH_HOME') else [])
    for root in roots:
        if resolved == root or root in resolved.parents:
            raise InsecureConfig(f'secret files must live outside the repository and '
                                 f'COACH_HOME ({root}); use $XDG_CONFIG_HOME/coach-claudio/')
    return Path(os.path.abspath(path))


def _ensure_private_dir(path):
    _outside_repo(path)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    _check_private(path, True)


def write_private_json(path, data):
    """Atomic 0600 replace with fsync; refuses symlink targets."""
    path = _outside_repo(path)
    _ensure_private_dir(path.parent)
    if path.is_symlink():
        raise InsecureConfig(f'{path} is a symlink; refusing to write through it')
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, allow_nan=False, indent=1, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class _Lock:
    """Exclusive inter-process lock on a private lock file beside the config."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        _ensure_private_dir(self.path.parent)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def read_private_json(path):
    path = _outside_repo(path)
    _check_private(path.parent, True)
    _check_private(path, False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding='utf-8') as f:
        text = f.read(1_000_000)
    try:
        data = strict_loads(text)
    except ValueError:
        raise NotConfigured(f'{path} is not valid strict JSON') from None
    if not isinstance(data, dict):
        raise NotConfigured(f'{path} must hold a JSON object')
    return data


_HEADER_SAFE = re.compile(r'[\x20-\x7e]{1,300}')


def _text(cfg, key, required=True, limit=300):
    v = cfg.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not _HEADER_SAFE.fullmatch(v) or len(v) > limit:
        raise NotConfigured(f'config: {key} is missing or not printable ASCII')
    return v


class Config:
    """Validated config. Client secret is held privately and never printed."""

    def __init__(self, data, path):
        self.path = path
        self.client_id = _text(data, 'client_id')
        self._client_secret = _text(data, 'client_secret')
        self.user_agent = _text(data, 'user_agent')
        if not self.user_agent.startswith('YAZIO/'):
            raise NotConfigured('config: user_agent must be a current YAZIO app '
                                'User-Agent ("YAZIO/<version> (...)")')
        self.timeout_s = data.get('timeout_s', 20)
        if isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float)) \
                or not 1 <= self.timeout_s <= 120:
            raise NotConfigured('config: timeout_s must be a number of seconds in 1..120')
        self.search = data.get('search') or {}
        if not isinstance(self.search, dict):
            raise NotConfigured('config: search must be an object')
        self.timezone = data.get('timezone')
        if self.timezone is not None and zone(self.timezone) is None:
            raise NotConfigured('config: timezone must be an IANA zone name (e.g. Europe/Berlin)')
        self.product = data.get('product') or {}
        if not isinstance(self.product, dict):
            raise NotConfigured('config: product must be an object')
        units = self.product.get('base_units', [])
        if not isinstance(units, list) or not set(units) <= {'g', 'ml'}:
            raise NotConfigured('config: product.base_units must be a subset of ["g", "ml"]')
        self.token_path = path.with_name(path.stem + '-token.json')
        self.contract_path = path.with_name(path.stem + '-contract.json')
        self.lock_path = path.with_name(path.stem + '.lock')
        self.probe_path = path.with_name(path.stem + '-probe.json')

    def search_params(self):
        """sex/countries/locales exactly as the account owner configured them."""
        out = {}
        for key, required in (('sex', True), ('countries', True), ('locales', False)):
            v = self.search.get(key)
            if v is None and not required:
                continue
            if not isinstance(v, str) or not _PARAM.fullmatch(v):
                raise NotConfigured(f'config: search.{key} is not set; the adapter never '
                                    'infers it (set it to what the YAZIO account uses)')
            out[key] = v
        return out

    def app_fields(self):
        return {'client_id': self.client_id, 'client_secret': self._client_secret}

    def __repr__(self):
        return f'<yazio Config {self.path} client_id=*** client_secret=***>'

    __str__ = __repr__


def load_config():
    path = config_path()
    return Config(read_private_json(path), path)


def config_path():
    explicit = os.environ.get('YAZIO_CONFIG')
    if explicit:
        return Path(explicit)
    xdg = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(xdg) / 'coach-claudio' / 'yazio.json'


def status():
    """Offline summary for humans and the parent sync: no network, no secrets."""
    path = config_path()
    out = {'config': str(path), 'configured': False, 'logged_in': False, 'can_write': False,
           'capabilities': None, 'problem': None}
    try:
        client = Client.load(transport=_NoNetwork())
    except YazioError as e:
        out['problem'] = str(e)
        return out
    out['configured'] = True
    tok_path = client.config.token_path
    try:
        tok = read_private_json(tok_path) if tok_path.exists() or tok_path.is_symlink() else {}
        out['logged_in'] = bool(tok.get('refresh_token')) and not tok.get('revoked')
        if tok.get('revoked'):
            out['problem'] = 'refresh token was rejected; run nutrition_yazio.py login'
        caps = client.capabilities()
    except YazioError as e:
        out['problem'] = str(e)
        return out
    out['capabilities'] = caps
    out['can_write'] = out['logged_in'] and caps['create_consumed'] == 'live_confirmed'
    return out


class _NoNetwork:
    def request(self, *a, **kw):
        raise YazioError('status is offline')


_ERROR_CODE = re.compile(r'[a-z][a-z0-9_]{0,39}')


def _error_code(data):
    """The only part of an error body ever surfaced: a short snake_case code."""
    code = data.get('error') if isinstance(data, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None


def checked_uuid(v, what='id'):
    """Lower-case canonical UUID string; anything else could reach a URL path."""
    if not isinstance(v, str) or not UUID_RE.fullmatch(v):
        raise ValueError(f'{what} must be a lower-case UUID string')
    return v


def _token_text(v):
    if not isinstance(v, str) or not 8 <= len(v) <= 8192 or not _HEADER_SAFE.fullmatch(v[:300]) \
            or any(not 0x21 <= ord(c) <= 0x7e for c in v):
        raise ProtocolError('token response: token missing or malformed')
    return v


class Client:
    """One YAZIO account. Every method is a single documented /v22 call or a
    write followed by read-back; nothing here retries a write."""

    def __init__(self, config, transport, clock=time.time, sleep=None):
        self.config, self.transport, self.clock = config, transport, clock
        self.sleep = sleep or (lambda seconds: time.sleep(seconds))

    @classmethod
    def load(cls, transport=None, **kw):
        if not config_path().exists() and not config_path().is_symlink():
            raise NotConfigured(f'no YAZIO config at {config_path()}; '
                                'run in a local terminal: nutrition_yazio.py setup')
        cfg = load_config()
        return cls(cfg, transport or HTTPSTransport(), **kw)

    def __repr__(self):
        return f'<yazio Client {self.config.path}>'

    # -- HTTP -------------------------------------------------------------
    def _send(self, method, path, body=None, query=None, token=None, what='request'):
        url = f'https://{HOST}{API}{path}' + (f'?{urlencode(query)}' if query else '')
        checked_url(url)
        headers = {'User-Agent': self.config.user_agent, 'Accept': 'application/json'}
        raw = None
        if body is not None:
            raw = strict_dumps(body).encode()
            headers['Content-Type'] = 'application/json; charset=utf-8'
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        try:
            status, _headers, payload = self.transport.request(
                method, url, headers, raw, self.config.timeout_s)
        except YazioError:
            raise
        except Exception as e:  # timeout, reset, TLS: no trustworthy response
            raise TransportError(f'YAZIO {what}: no response ({type(e).__name__})') from None
        data = None
        if payload:
            try:
                data = strict_loads(payload.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                if 200 <= status < 300:
                    raise ProtocolError(f'YAZIO {what}: response is not strict JSON') from None
        if 300 <= status < 400:
            raise YazioHTTPError(status, 'redirect_refused', what)
        if status == 403 and _error_code(data) == 'version_blocked':
            raise VersionBlocked()
        if not 200 <= status < 300:
            raise YazioHTTPError(status, _error_code(data), what)
        return status, data

    # -- auth -------------------------------------------------------------
    def _grant(self, fields, what):
        try:
            _, data = self._send('POST', '/oauth/token', {**self.config.app_fields(), **fields},
                                 what=what)
        except VersionBlocked:
            raise
        except YazioHTTPError as e:
            if e.status in (400, 401, 403):
                raise AuthError(f'YAZIO {what} rejected ({e.code or e.status}); '
                                'run in a local terminal: nutrition_yazio.py login') from None
            raise
        if not isinstance(data, dict):
            raise ProtocolError(f'YAZIO {what}: no token object')
        expires = data.get('expires_in')
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) \
                or not 0 < expires < 10 * 365 * 86400:
            raise ProtocolError(f'YAZIO {what}: expires_in missing or out of range')
        return {'access_token': _token_text(data.get('access_token')),
                'refresh_token': _token_text(data.get('refresh_token')),
                'expires_at': self.clock() + expires}

    def login(self, username, password):
        """Password grant. The password is sent once and never stored."""
        with _Lock(self.config.lock_path):
            # No stable verified account id is available. Every explicit login
            # invalidates proof; rotating bearer/refresh tokens are not identity.
            cfg = self.config
            cfg.contract_path.unlink(missing_ok=True)
            if cfg.probe_path.exists() or cfg.probe_path.is_symlink():
                pending = read_private_json(cfg.probe_path)
                report = cfg.probe_path.with_name(
                    f'{cfg.path.stem}-probe-abandoned-{uuid.uuid4()}.json')
                write_private_json(report, {
                    'pending_probe': pending,
                    'reason': 'explicit login; previous account cleanup unconfirmed',
                    'action': 'Inspect the previous account manually; never resume on the new login.'})
                cfg.probe_path.unlink()
            tok = self._grant({'grant_type': 'password', 'username': username,
                               'password': password}, 'login')
            write_private_json(self.config.token_path, tok)

    def access_token(self, force_refresh=False):
        """Current bearer token. Under the lock, rereads the token file so a
        rotation by another process wins, refreshes when near expiry and
        persists the rotated pair before anything else uses it."""
        with _Lock(self.config.lock_path):
            if not self.config.token_path.exists() and not self.config.token_path.is_symlink():
                raise AuthError('not logged in; run in a local terminal: nutrition_yazio.py login')
            tok = read_private_json(self.config.token_path)
            if tok.get('revoked'):
                raise AuthError('YAZIO refresh token was rejected earlier; '
                                'run in a local terminal: nutrition_yazio.py login')
            expires_at = tok.get('expires_at')
            if not force_refresh and isinstance(expires_at, (int, float)) \
                    and not isinstance(expires_at, bool) \
                    and expires_at - TOKEN_SKEW_S > self.clock():
                return _token_text(tok.get('access_token'))
            refresh = _token_text(tok.get('refresh_token'))
            try:
                new = self._grant({'grant_type': 'refresh_token', 'refresh_token': refresh},
                                  'token refresh')
            except AuthError:
                write_private_json(self.config.token_path, {'revoked': True,
                                                            'revoked_at': self.clock()})
                raise
            write_private_json(self.config.token_path, new)
            return new['access_token']


    def _get(self, path, query=None, what='read'):
        """Authenticated GET with bounded backoff; one forced refresh on 401."""
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._send('GET', path, query=query, token=self.access_token(),
                                  what=what)[1]
            except YazioHTTPError as e:
                if e.status == 401 and not refreshed:
                    refreshed = True
                    self.access_token(force_refresh=True)
                    attempt -= 1
                    continue
                if isinstance(e, VersionBlocked) or not (e.status == 429 or e.status >= 500) \
                        or attempt >= GET_ATTEMPTS:
                    raise
            except TransportError:
                if attempt >= GET_ATTEMPTS:
                    raise
            self.sleep(min(2 ** (attempt - 1), 8))

    # -- capabilities -----------------------------------------------------
    def contract(self):
        """Valid contract record written by `verify-write`, or {}. Config keys
        such as "verified": true are ignored: only an observed run counts."""
        path = self.config.contract_path
        if not path.exists() and not path.is_symlink():
            return {}
        rec = read_private_json(path)
        return rec if contract_valid(rec, self.config) else {}

    def capabilities(self):
        """Per operation and nutrient: researched (pinned spec only),
        live_confirmed (recorded verify-write run), not_accepted, unverified."""
        rec = self.contract()
        product = rec.get('product', {})
        consumed = rec.get('consumed', {})
        roundtrip = set(product.get('nutrients_roundtrip', []))
        live = lambda ok: 'live_confirmed' if rec and ok else 'researched'  # noqa: E731
        nutrients = {}
        for key in NUTRIENT_KEYS:
            if key == 'salt_g':
                salt = rec.get('salt', {})
                ok = salt.get('roundtrip') and salt.get('display_confirmed_by_user')
                nutrients[key] = 'live_confirmed' if rec and ok else 'unverified'
            elif not rec:
                nutrients[key] = 'researched'
            else:
                nutrients[key] = 'live_confirmed' if key in roundtrip else 'not_accepted'
        return {'read': 'researched', 'search': 'researched',
                'create_private_product': live(product.get('create_readback')
                                               and 'energy_kcal' in roundtrip),
                'create_consumed': live(consumed.get('create_readback') and
                                        consumed.get('fields_roundtrip') == CONSUMED_FIELDS),
                'delete_consumed': live(consumed.get('delete_readback')),
                'base_unit': rec.get('base_unit'), 'recorded_at': rec.get('recorded_at'),
                'nutrients': nutrients}

    def salt_confirmed(self):
        return self.capabilities()['nutrients']['salt_g'] == 'live_confirmed'

    def _require(self, op, probe=False):
        if not probe and self.capabilities()[op] != 'live_confirmed':
            raise ContractUnverified(
                f'{op} is not confirmed on this YAZIO account; it stays local. The account '
                'owner can run the opt-in check: nutrition_yazio.py verify-write --help')

    # -- writes (gated) ---------------------------------------------------
    def _write(self, method, path, body, op, id, day=None):
        """Exactly one send. No response or a 5xx means UNKNOWN, never retried."""
        try:
            return self._send(method, path, body, token=self.access_token(), what=op)
        except TransportError:
            raise UnknownOutcome(op, id, day, 'no response') from None
        except ProtocolError:
            raise UnknownOutcome(op, id, day, 'unreadable success response') from None
        except VersionBlocked:
            raise
        except YazioHTTPError as e:
            if e.status >= 500:
                raise UnknownOutcome(op, id, day, f'HTTP {e.status}') from None
            raise

    def own_product_ids(self):
        """GET /v22/user/products: ids of the account's own products."""
        data = self._get('/user/products', what='own products')
        if not isinstance(data, list) or not all(isinstance(i, str) for i in data):
            raise ProtocolError('own products: expected a list of id strings')
        return data

    def _check_product_draft(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('nutrients'), dict):
            raise ValueError('payload must come from prepare_product')
        checked_uuid(payload.get('id'), 'product id')

    def reconcile_product(self, payload):
        """Read-only: absent | confirmed (all sent fields match) | mismatch."""
        self._check_product_draft(payload)
        pid = payload['id']
        if pid not in self.own_product_ids():
            return {'status': 'absent', 'id': pid}
        remote = self._get(f'/products/{pid}', what='product read-back')
        diffs, verified, warnings = compare_product(payload, remote)
        return {'status': 'mismatch' if diffs else 'confirmed', 'id': pid, 'diffs': diffs,
                'verified_nutrients': verified, 'warnings': warnings, 'remote': remote}

    def create_private_product(self, payload, _probe=False):
        """POST /v22/user/products, then read it back. Existing id: reconcile,
        never a second create. Returns {'status': 'confirmed', ...}."""
        self._require('create_private_product', _probe)
        self._check_product_draft(payload)
        if not _probe and payload.get('base_unit') != self.capabilities()['base_unit']:
            raise ContractUnverified(f'base unit {payload.get("base_unit")!r} was not part of '
                                     'the recorded verify-write check')
        pid = payload['id']
        before = self.reconcile_product(payload)
        if before['status'] == 'mismatch':
            raise ReadbackMismatch('create_private_product', pid, before['diffs'])
        if before['status'] == 'confirmed':
            return {**before, 'already_present': True}
        self._write('POST', '/user/products', payload, 'create_private_product', pid)
        try:
            after = self.reconcile_product(payload)
        except YazioError as e:
            raise UnknownOutcome('create_private_product', pid, None,
                                 f'sent, read-back failed: {type(e).__name__}') from None
        if after['status'] != 'confirmed':
            raise ReadbackMismatch('create_private_product', pid,
                                   after.get('diffs') or ['not in own products after HTTP success'])
        return {**after, 'already_present': False}

    def get_consumed(self, day):
        """GET /v22/user/consumed-items?date=YYYY-MM-DD (one local day)."""
        data = self._get('/user/consumed-items', {'date': checked_day(day)}, what='consumed read')
        if not isinstance(data, dict):
            raise ProtocolError('consumed read: expected an object')
        out = {}
        for bucket in ('products', 'recipe_portions', 'simple_products'):
            items = data.get(bucket)
            if not isinstance(items, list) or not all(
                    isinstance(i, dict) and isinstance(i.get('id'), str) for i in items):
                raise ProtocolError(f'consumed read: {bucket} must be objects with ids')
            out[bucket] = items
        return out

    def _find_consumed(self, day, consumed_id):
        found = [i for i in self.get_consumed(day)['products'] if i['id'] == consumed_id]
        if len(found) > 1:
            raise ProtocolError(f'consumed read: id {consumed_id} listed twice')
        return found[0] if found else None

    def _consumed_item(self, payload):
        if not isinstance(payload, dict) or payload.get('recipe_portions') != [] \
                or payload.get('simple_products') != [] \
                or not isinstance(payload.get('products'), list) or len(payload['products']) != 1:
            raise ValueError('payload must come from prepare_consumed (exactly one product entry)')
        item = payload['products'][0]
        checked_uuid(item.get('id'), 'consumed id')
        checked_uuid(item.get('product_id'), 'product_id')
        return item, checked_day(str(item.get('date', ''))[:10])

    def reconcile_consumed(self, payload):
        """Read-only: absent | confirmed | mismatch for one prepared entry."""
        item, day = self._consumed_item(payload)
        remote = self._find_consumed(day, item['id'])
        if remote is None:
            return {'status': 'absent', 'id': item['id'], 'day': day}
        diffs = compare_consumed(item, remote)
        return {'status': 'mismatch' if diffs else 'confirmed', 'id': item['id'], 'day': day,
                'diffs': diffs, 'remote': remote}

    def create_consumed(self, payload, _probe=False):
        """POST /v22/user/consumed-items, then read the day back and require the
        same id, product_id, amount, daytime and wall-clock date. An existing id
        is reconciled, never re-posted (no upsert is documented)."""
        self._require('create_consumed', _probe)
        item, day = self._consumed_item(payload)
        before = self.reconcile_consumed(payload)
        if before['status'] == 'mismatch':
            raise ReadbackMismatch('create_consumed', item['id'], before['diffs'])
        if before['status'] == 'confirmed':
            return {**before, 'already_present': True}
        self._write('POST', '/user/consumed-items', payload, 'create_consumed', item['id'], day)
        try:
            after = self.reconcile_consumed(payload)
        except YazioError as e:
            raise UnknownOutcome('create_consumed', item['id'], day,
                                 f'sent, read-back failed: {type(e).__name__}') from None
        if after['status'] != 'confirmed':
            raise ReadbackMismatch('create_consumed', item['id'],
                                   after.get('diffs') or ['not listed after HTTP success'])
        return {**after, 'already_present': False}

    def delete_consumed(self, consumed_id, day, _probe=False):
        """DELETE /v22/user/consumed-items with {"products": "<id>"} (a single
        id string per the v22 spec, not an array or ?id=), then require that
        the day no longer lists it."""
        self._require('delete_consumed', _probe)
        checked_uuid(consumed_id, 'consumed_id')
        checked_day(day)
        if self._find_consumed(day, consumed_id) is None:
            return {'status': 'absent', 'id': consumed_id, 'day': day}
        self._write('DELETE', '/user/consumed-items', {'products': consumed_id},
                    'delete_consumed', consumed_id, day)
        try:
            still = self._find_consumed(day, consumed_id)
        except YazioError as e:
            raise UnknownOutcome('delete_consumed', consumed_id, day,
                                 f'sent, read-back failed: {type(e).__name__}') from None
        if still is not None:
            raise ReadbackMismatch('delete_consumed', consumed_id, ['still present after delete'])
        return {'status': 'deleted', 'id': consumed_id, 'day': day}

    # -- opt-in live contract check ---------------------------------------
    def run_write_probe(self, *, consent, amount, salt_display, when=None):
        """The only way to reach live_confirmed. Writes to the real account:
        one private test product (all 7 nutrients, distinct values), one
        consumed entry of `amount` g, then deletes both and checks each step by
        read-back. ids are persisted before the first send; a rerun after an
        UNKNOWN reconciles the same ids. salt_display(question) -> bool asks the
        account owner what the app shows, since the API cannot tell g from mg."""
        if consent is not True:
            raise ContractUnverified('verify-write needs explicit consent '
                                     '(--i-consent-to-test-writes)')
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
                or not 0 < amount <= PROBE_MAX_AMOUNT:
            raise ValueError(f'test amount must be in (0, {PROBE_MAX_AMOUNT}] g')
        z = zone(self.config.timezone)
        if z is None:
            raise NotConfigured('config: timezone (IANA name) is not set')
        cfg = self.config
        pend = (read_private_json(cfg.probe_path)
                if cfg.probe_path.exists() or cfg.probe_path.is_symlink() else None)
        if pend:
            if not all(k in pend for k in ('phase', 'product_payload', 'consumed_payload', 'amount')):
                raise ContractUnverified('legacy unfinished probe: inspect previous test artifacts '
                                         'manually; login archives cleanup evidence before a new probe')
            if amount != pend['amount']:
                raise ValueError('pending probe amount differs; resume with the original amount')
            if pend['phase'] not in ('create_product', 'create_consumed', 'cleanup_consumed',
                                     'cleanup_product', 'record'):
                raise ProtocolError('pending probe phase is invalid')
        else:
            pid, cid = str(uuid.uuid4()), str(uuid.uuid4())
            when = when or datetime.fromtimestamp(self.clock(), z)
            draft = prepare_product('100g', PROBE_PER100, pid, PROBE_NAME,
                                    category=cfg.product.get('category'),
                                    base_units=cfg.product.get('base_units'), salt_write=True)['payload']
            prepared = self.prepare_consumed(cid, pid, amount, 'g', when, 'snack', 'g')
            pend = {'product_id': pid, 'consumed_id': cid, 'when': when.isoformat(),
                    'amount': amount, 'phase': 'create_product', 'product_payload': draft,
                    'consumed_payload': prepared['payload'], 'day': prepared['day']}
            write_private_json(cfg.probe_path, pend)
        pid, cid = checked_uuid(pend['product_id']), checked_uuid(pend['consumed_id'])
        draft, payload = pend['product_payload'], pend['consumed_payload']

        def checkpoint(phase, **observations):
            pend.update(observations)
            pend['phase'] = phase
            write_private_json(cfg.probe_path, pend)

        if pend['phase'] == 'create_product':
            found = self.reconcile_product(draft)
            if found['status'] == 'absent':
                self._write('POST', '/user/products', draft, 'create_private_product', pid)
                try:
                    found = self.reconcile_product(draft)
                except YazioError as e:
                    raise UnknownOutcome('create_private_product', pid, None,
                                         f'sent, read-back failed: {type(e).__name__}') from None
            if found['status'] == 'absent':
                raise ReadbackMismatch('create_private_product', pid, ['not in own products'])
            structural = [d for d in found['diffs'] if d not in NUTRIENT_KEYS]
            if structural:
                raise ReadbackMismatch('create_private_product', pid, structural)
            roundtrip = found['verified_nutrients']
            salt_ok = 'salt_g' in roundtrip and salt_display(
                f'In the YAZIO app, open the private product "{PROBE_NAME}". Does it show salt '
                '0.34 g per 100 g (equivalently 340 mg per 100 g, not 0.0034 g)?') is True
            checkpoint('create_consumed', roundtrip=roundtrip, salt_ok=salt_ok)
        if pend['phase'] == 'create_consumed':
            made = self.create_consumed(payload, _probe=True)
            checkpoint('cleanup_consumed', date_read=made['remote'].get('date'))
        # Cleanup is monotonic: after this durable checkpoint no POST is reachable.
        if pend['phase'] == 'cleanup_consumed':
            self.delete_consumed(cid, pend['day'], _probe=True)
            checkpoint('cleanup_product')
        if pend['phase'] == 'cleanup_product':
            if pid in self.own_product_ids():
                self._write('DELETE', f'/user/products/{pid}', None, 'delete_product', pid)
            if pid in self.own_product_ids():
                raise ReadbackMismatch('delete_product', pid, ['still present after delete'])
            checkpoint('record')
        rec = contract_record({
            'user_agent': cfg.user_agent, 'category': draft['category'], 'base_unit': 'g',
            'product_created': True, 'product_nutrients_roundtrip': pend['roundtrip'],
            'salt_display_confirmed_by_user': pend['salt_ok'], 'consumed_created': True,
            'consumed_fields_roundtrip': CONSUMED_FIELDS, 'consumed_deleted': True,
            'product_deleted': True, 'date_sent': payload['products'][0]['date'],
            'date_read': pend['date_read']},
            recorded_at=datetime.fromtimestamp(self.clock(), z).isoformat())
        write_private_json(cfg.contract_path, rec)
        cfg.probe_path.unlink(missing_ok=True)
        return rec

    # -- payloads ---------------------------------------------------------
    def prepare_product(self, per, nutrients, product_id, name, producer=None):
        caps = self.capabilities()['nutrients']
        keep_local = [k for k, v in caps.items() if v == 'not_accepted']
        return prepare_product(per, nutrients, product_id, name,
                               category=self.config.product.get('category'),
                               base_units=self.config.product.get('base_units'),
                               salt_write=caps['salt_g'] == 'live_confirmed',
                               keep_local=keep_local, producer=producer)

    def prepare_consumed(self, consumed_id, product_id, amount, unit, when, daytime, base_unit):
        if zone(self.config.timezone) is None:
            raise NotConfigured('config: timezone (IANA name, e.g. Europe/Berlin) is not set')
        return prepare_consumed(consumed_id, product_id, amount, unit, when, daytime,
                                tz=self.config.timezone, base_unit=base_unit)

    # -- reads ------------------------------------------------------------
    def _map_nutrients(self, raw):
        per_unit, warnings = {}, []
        raw = raw if isinstance(raw, dict) else {}
        for local, key in NUTRIENT_KEYS.items():
            v = raw.get(key)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ProtocolError(f'product nutrient {key} is not a number')
            if local == 'salt_g' and not self.salt_confirmed():
                warnings.append('salt: nutrient.salt scale for products is unverified; '
                                'not mapped (keep the local salt value)')
                continue
            per_unit[local] = v
        return per_unit, warnings

    def get_product(self, product_id):
        """GET /v22/products/{id}. The response has no id, so the asked id is kept."""
        checked_uuid(product_id, 'product_id')
        data = self._get(f'/products/{product_id}', what='product read')
        if not isinstance(data, dict):
            raise ProtocolError('product read: expected an object')
        per_unit, warnings = self._map_nutrients(data.get('nutrients'))
        eans = data.get('eans') or []
        if not isinstance(eans, list) or not all(isinstance(e, str) for e in eans):
            raise ProtocolError('product read: eans must be strings')
        return {'id': product_id, 'name': data.get('name'), 'producer': data.get('producer'),
                'base_unit': data.get('base_unit'), 'category': data.get('category'),
                'is_private': data.get('is_private'), 'is_deleted': data.get('is_deleted'),
                'servings': data.get('servings'), 'eans': eans,
                'nutrients': data.get('nutrients'), 'per_unit': per_unit, 'warnings': warnings}

    def find_branded(self, barcode, per, nutrients, max_hits=5):
        """Prefer an existing YAZIO product over a private copy, but only when
        its EAN list contains this exact barcode string (leading zeros kept),
        the base unit matches and every locally known nutrient agrees per 100.
        Salt is only compared once its scale is confirmed. Read-only."""
        if not isinstance(barcode, str):
            raise TypeError('barcode must be a string (leading zeros matter)')
        if not re.fullmatch(r'\d{8}|\d{12,14}', barcode):
            raise ValueError('barcode must be 8 or 12-14 digits')
        if per not in ('100g', '100ml'):
            raise ValueError("basis must be '100g' or '100ml'")
        if not isinstance(nutrients, dict) or set(nutrients) - set(NUTRIENT_KEYS):
            raise ValueError(f'nutrients: only canonical keys {sorted(NUTRIENT_KEYS)}')
        known = {k: _nutrient(v, k) for k, v in nutrients.items() if v is not None}
        if 'energy_kcal' not in known:
            raise ValueError('nutrients: canonical keys with energy_kcal known')
        not_compared = [] if self.salt_confirmed() else [k for k in ('salt_g',) if k in known]
        reasons = []
        for hit in self.search(barcode)[:max_hits]:
            pid = hit.get('product_id')
            try:
                pid = checked_uuid(pid, 'product_id')
            except ValueError:
                continue
            remote = self.get_product(pid)
            why = []
            if barcode not in remote['eans']:
                why.append('EAN not listed exactly')
            if remote['base_unit'] != per[3:]:
                why.append(f'base unit {remote["base_unit"]!r}')
            if remote['is_deleted']:
                why.append('deleted')
            for key, local in known.items():
                if key in not_compared:
                    continue
                got = remote['per_unit'].get(key)
                if got is None or not per100_agrees(key, local, got):
                    why.append(f'{key} differs or missing')
            if not why:
                return {'status': 'match', 'product_id': pid, 'name': remote['name'],
                        'compared': sorted(set(known) - set(not_compared)),
                        'not_compared': not_compared}
            reasons.append(f'{pid}: {"; ".join(why)}')
        return {'status': 'none', 'reasons': reasons}

    def search(self, query):
        """GET /v22/products/search. A barcode must be a string (leading zeros)."""
        if not isinstance(query, str):
            raise TypeError('search query must be a string; pass barcodes as text')
        query = query.strip()
        if not 1 <= len(query) <= 200:
            raise ValueError('search query must be 1..200 characters')
        data = self._get('/products/search', {'query': query, **self.config.search_params()},
                         what='search')
        if not isinstance(data, list) or not all(isinstance(h, dict) for h in data):
            raise ProtocolError('search: expected a list of objects')
        return data


# -- payloads (pure, deterministic) ---------------------------------------
def _nutrient(v, key):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError(f'nutrient {key} must be a finite number >= 0 or None')
    return v


def prepare_product(per, nutrients, product_id, name, *, category, base_units,
                    salt_write=False, keep_local=(), producer=None):
    """Private-product draft from a canonical per-100 g/ml snapshot.

    Every value is divided by 100 in Decimal (per one g or ml; energy stays kcal,
    no J/kJ arithmetic here). None means unknown and is left out, never sent as
    0. Salt is only sent when salt_write is True (a recorded live check);
    otherwise it stays local and is listed in local_only.
    """
    checked_uuid(product_id, 'product_id')
    if per not in ('100g', '100ml'):
        raise ValueError("basis must be '100g' or '100ml' (convert servings/totals first)")
    unit = per[3:]
    if unit not in tuple(base_units or ()):
        raise NotConfigured(f'config: product.base_units does not include {unit!r}')
    if not isinstance(category, str) or not category:
        raise NotConfigured('config: product.category is not set; the adapter never invents one')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError('name must be 1..120 characters')
    if not isinstance(nutrients, dict):
        raise ValueError('nutrients must be an object')
    unknown_keys = set(nutrients) - set(NUTRIENT_KEYS)
    if unknown_keys:
        raise ValueError(f'nutrients: only canonical keys {sorted(NUTRIENT_KEYS)}, '
                         f'not {sorted(unknown_keys)}')
    out, synced, unknown, local_only, warnings = {}, [], [], [], []
    for local, key in NUTRIENT_KEYS.items():
        v = nutrients.get(local)
        if v is None:
            unknown.append(local)
            continue
        v = _nutrient(v, local)
        if local == 'salt_g' and not salt_write:
            local_only.append(local)
            warnings.append('salt kept local: YAZIO product salt scale is not confirmed '
                            'for this account (run verify-write)')
            continue
        if local in keep_local:
            local_only.append(local)
            warnings.append(f'{local} kept local: YAZIO did not store it in the recorded check')
            continue
        out[key] = float(Decimal(repr(v)) / 100)
        synced.append(local)
    if 'energy_kcal' not in synced:
        raise ValueError('energy_kcal is required for a YAZIO product')
    payload = {'id': product_id, 'name': name.strip(), 'category': category, 'base_unit': unit,
               'is_private': True, 'nutrients': out, 'servings': []}
    if producer:
        payload['producer'] = str(producer)[:120]
    return {'payload': payload, 'synced': synced, 'unknown': unknown, 'local_only': local_only,
            'warnings': warnings, 'fingerprint': fingerprint(payload)}


def zone(name):
    try:
        return ZoneInfo(name) if isinstance(name, str) and name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def prepare_consumed(consumed_id, product_id, amount, unit, when, daytime, *, tz, base_unit):
    """POST /v22/user/consumed-items body for one product entry.

    consumed_id is the caller's durable outbox UUID (persist it before sending).
    `when` must be timezone-aware; YAZIO takes a local wall-clock timestamp
    without offset, so it is rendered in `tz`. Around a DST fall-back the same
    wall clock happens twice and the server's reading is unverified: warned.
    """
    checked_uuid(consumed_id, 'consumed_id')
    checked_uuid(product_id, 'product_id')
    if unit != base_unit or unit not in ('g', 'ml'):
        raise ValueError(f'amount unit {unit!r} must be the product base unit {base_unit!r} '
                         '(g or ml; no serving arithmetic here)')
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
            or not math.isfinite(amount) or not 0 < amount <= MAX_AMOUNT:
        raise ValueError(f'amount must be a finite number in (0, {MAX_AMOUNT}]')
    if daytime not in DAYTIMES:
        raise ValueError(f'daytime must be one of {", ".join(DAYTIMES)}')
    if not isinstance(when, datetime) or when.utcoffset() is None:
        raise ValueError('when must be a timezone-aware datetime')
    z = zone(tz)
    if z is None:
        raise NotConfigured(f'timezone {tz!r} is not a known IANA zone')
    local = when.astimezone(z).replace(microsecond=0)
    warnings = []
    if local.replace(fold=1 - local.fold).utcoffset() != local.utcoffset():
        warnings.append(f'{local:%Y-%m-%d %H:%M} is ambiguous in {tz} (DST change); YAZIO '
                        'stores wall-clock time without offset and its reading is unverified')
    item = {'id': consumed_id, 'product_id': product_id, 'amount': float(amount),
            'daytime': daytime, 'date': local.strftime('%Y-%m-%d %H:%M:%S'),
            'serving': None, 'serving_quantity': None}
    payload = {'products': [item], 'recipe_portions': [], 'simple_products': []}
    return {'payload': payload, 'day': local.strftime('%Y-%m-%d'),
            'local_time': local.isoformat(), 'warnings': warnings,
            'fingerprint': fingerprint(payload)}


def contract_record(results, recorded_at):
    """The only thing that turns a capability from researched into
    live_confirmed: what one verify-write run observed on the account. The
    digest catches hand edits and stale schemas; it is a consistency check,
    not a signature (the account owner can always edit local files)."""
    roundtrip = sorted(k for k in results['product_nutrients_roundtrip'] if k in NUTRIENT_KEYS)
    rec = {'schema': CONTRACT_VERSION, 'api': API, 'spec': SPEC, 'recorded_at': recorded_at,
           'user_agent': results['user_agent'], 'category': results['category'],
           'base_unit': results['base_unit'],
           'product': {'create_readback': bool(results['product_created']),
                       'nutrients_roundtrip': roundtrip,
                       'delete_readback': bool(results.get('product_deleted'))},
           'salt': {'roundtrip': 'salt_g' in roundtrip,
                    'display_confirmed_by_user': bool(results['salt_display_confirmed_by_user'])},
           'consumed': {'create_readback': bool(results['consumed_created']),
                        'fields_roundtrip': sorted(results['consumed_fields_roundtrip']),
                        'delete_readback': bool(results['consumed_deleted']),
                        'date_sent': results.get('date_sent'),
                        'date_read': results.get('date_read')}}
    rec['digest'] = fingerprint(rec)
    return rec


def contract_valid(rec, config):
    if not isinstance(rec, dict) or rec.get('schema') != CONTRACT_VERSION or rec.get('api') != API:
        return False
    body = {k: v for k, v in rec.items() if k != 'digest'}
    try:
        if rec.get('digest') != fingerprint(body):
            return False
    except (TypeError, ValueError):
        return False
    return (rec.get('category') == config.product.get('category')
            and rec.get('base_unit') in config.product.get('base_units', []))


def _close(a, b):
    return (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and not isinstance(a, bool) and not isinstance(b, bool)
            and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9))


def compare_product(payload, remote):
    """(diffs, verified_local_keys, warnings) for a draft vs GET /products/{id}."""
    diffs, verified, warnings = [], [], []
    if not isinstance(remote, dict):
        return ['product object'], verified, warnings
    for key in ('name', 'base_unit'):
        if remote.get(key) != payload.get(key):
            diffs.append(key)
    if remote.get('is_private') is not True:
        diffs.append('is_private')
    if remote.get('is_deleted'):
        diffs.append('is_deleted')
    if 'category' in remote and remote['category'] != payload.get('category'):
        diffs.append('category')
    got = remote.get('nutrients') if isinstance(remote.get('nutrients'), dict) else {}
    sent = payload.get('nutrients', {})
    for local, key in NUTRIENT_KEYS.items():
        if key in sent:
            if _close(got.get(key), sent[key]):
                verified.append(local)
            else:
                diffs.append(local)
        elif got.get(key) is not None:
            warnings.append(f'{local} is unknown locally but YAZIO shows {got[key]!r} per unit '
                            '(not sent by this adapter)')
    return diffs, verified, warnings


def checked_day(day):
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('day must be YYYY-MM-DD')
    date.fromisoformat(day)
    return day


def _wall_clock(v):
    """Remote timestamp -> 'YYYY-MM-DD HH:MM:SS' wall clock, or None."""
    if not isinstance(v, str):
        return None
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?', v)
    return f'{m[1]} {m[2]}' if m else None


def compare_consumed(sent, remote):
    diffs = []
    for key in ('serving', 'serving_quantity'):
        if key not in remote or remote[key] is not None or sent.get(key) is not None:
            diffs.append(key)
    for key in ('id', 'product_id', 'daytime'):
        if remote.get(key) != sent[key]:
            diffs.append(key)
    if not _close(remote.get('amount'), sent['amount']):
        diffs.append('amount')
    if _wall_clock(remote.get('date')) != sent['date']:
        diffs.append('date')
    return diffs


# Per-100 agreement allowed for a branded match: label rounding, not more.
MATCH_ABS = {'energy_kcal': 1.0, 'salt_g': 0.01}
MATCH_ABS_DEFAULT = 0.1
MATCH_REL = 0.02


def per100_agrees(key, local, remote_per_unit):
    remote = remote_per_unit * 100
    return abs(remote - local) <= max(MATCH_ABS.get(key, MATCH_ABS_DEFAULT),
                                      MATCH_REL * max(abs(remote), abs(local)))


def fingerprint(payload):
    return hashlib.sha256(strict_dumps(payload).encode()).hexdigest()


class HTTPSTransport:
    """Stdlib HTTPS to the one fixed host. Verified TLS, explicit timeout,
    no proxy environment, never follows redirects, bounded body."""

    def __init__(self):
        self.context = ssl.create_default_context()

    def request(self, method, url, headers, body, timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError('every request needs a timeout')
        host, path = checked_url(url)
        conn = http.client.HTTPSConnection(host, port=443, timeout=timeout, context=self.context)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ProtocolError('response body too large')
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()


# -- CLI --------------------------------------------------------------------
LOCAL_ONLY = ('run this yourself in a local terminal (never paste passwords or tokens '
              'into a chat): python3 skill/scripts/nutrition_yazio.py')


def _interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt, keep=None):
    v = input(f'{prompt}{f" [{keep}]" if keep else ""}: ').strip()
    return v or keep


def cmd_setup(args, transport):
    path = config_path()
    old = {}
    if path.exists() or path.is_symlink():
        old = read_private_json(path)
    print(f'YAZIO adapter setup -> {path} (0600, outside the repository)')
    print('The app client id/secret and User-Agent must come from your own setup; this '
          'tool ships none. Input is hidden; leave blank to keep the current value.')
    cfg = dict(old)
    cfg['client_id'] = getpass.getpass('app client_id: ').strip() or old.get('client_id')
    cfg['client_secret'] = getpass.getpass('app client_secret: ').strip() or old.get('client_secret')
    cfg['user_agent'] = _ask('User-Agent of a current YAZIO app', old.get('user_agent'))
    cfg['timezone'] = _ask('your IANA timezone (e.g. Europe/Berlin)', old.get('timezone'))
    search = dict(old.get('search') or {})
    print('Search parameters: exactly what your YAZIO account uses. Blank = unset '
          '(search stays disabled; nothing is guessed).')
    for key, label in (('sex', 'search sex'), ('countries', 'search countries (e.g. DE)'),
                       ('locales', 'search locales (optional)')):
        v = _ask(label, search.get(key))
        if v:
            search[key] = v
        else:
            search.pop(key, None)
    cfg['search'] = search
    product = dict(old.get('product') or {})
    v = _ask('private product category accepted by YAZIO (blank = product writes disabled)',
             product.get('category'))
    if v:
        product['category'] = v
    else:
        product.pop('category', None)
    units = _ask('base units to allow (g, ml or g,ml)', ','.join(product.get('base_units', [])))
    product['base_units'] = [u.strip() for u in (units or '').split(',') if u.strip()]
    cfg['product'] = product
    Config(cfg, path)  # validate before writing anything
    write_private_json(path, cfg)
    print('saved. Next, in this terminal: python3 skill/scripts/nutrition_yazio.py login')
    return 0


def cmd_login(args, transport):
    client = Client.load(transport=transport)
    username = input('YAZIO e-mail: ').strip()
    password = getpass.getpass('YAZIO password (hidden, never stored): ')
    try:
        client.login(username, password)
    finally:
        del password
    print('logged in; access + refresh tokens are stored (0600). The password was not saved.')
    return 0


def cmd_logout(args, transport):
    path = load_config().token_path
    path.unlink(missing_ok=True)
    print('token removed')
    return 0


def cmd_status(args, transport):
    st = status()
    if args.json:
        print(json.dumps(st, indent=1, sort_keys=True))
        return 0
    print(f'config: {st["config"]}')
    if not st['configured']:
        print(f'YAZIO sync: not configured ({st["problem"]})')
        print(f'To enable: {LOCAL_ONLY} setup')
        return 0
    print(f'logged in: {"yes" if st["logged_in"] else "no"}')
    caps = st['capabilities'] or {}
    for op in ('create_private_product', 'create_consumed', 'delete_consumed'):
        print(f'{op}: {caps.get(op)}')
    print('nutrients: ' + ', '.join(f'{k}={v}' for k, v in (caps.get('nutrients') or {}).items()))
    if caps.get('recorded_at'):
        print(f'live contract recorded {caps["recorded_at"]} (base unit {caps["base_unit"]})')
    else:
        print('no live contract recorded: everything is researched (spec only); writes stay local')
    print(f'writes allowed: {"yes" if st["can_write"] else "no"}')
    if st['problem']:
        print(f'problem: {st["problem"]}')
    return 0


def cmd_verify(args, transport):
    client = Client.load(transport=transport)
    print('read-only check (GET only, nothing is written)')
    ids = client.own_product_ids()
    print(f'own products: {len(ids)} ids, shape ok')
    z = zone(client.config.timezone)
    day = (datetime.now(z) if z else datetime.now().astimezone()).strftime('%Y-%m-%d')
    consumed = client.get_consumed(day)
    print(f'consumed {day}: ' + ', '.join(f'{k} {len(v)}' for k, v in consumed.items())
          + ', shape ok')
    if args.search:
        print(f'search: {len(client.search(args.search))} hits, shape ok')
    print('read contract looks as documented. This does not verify writes.')
    return 0


def cmd_verify_write(args, transport):
    if not args.i_consent_to_test_writes:
        print('verify-write writes to your real YAZIO account and needs '
              '--i-consent-to-test-writes')
        return 2
    client = Client.load(transport=transport)
    print(f'This will, on your real YAZIO account: create the private product '
          f'"{PROBE_NAME}", log {args.amount:g} g of it as a snack now, delete that entry, '
          'delete the product, and check every step by reading it back.')
    if input('Type WRITE to continue: ').strip() != 'WRITE':
        print('aborted, nothing sent')
        return 1

    def salt_display(question):
        return input(f'{question} [y/N]: ').strip().lower() == 'y'

    rec = client.run_write_probe(consent=True, amount=args.amount, salt_display=salt_display)
    print(f'recorded live contract -> {client.config.contract_path}')
    print('nutrients stored as sent: ' + ', '.join(rec['product']['nutrients_roundtrip']))
    print(f'salt mapping confirmed: {"yes" if rec["salt"]["display_confirmed_by_user"] else "no"}')
    print(f'timestamp sent {rec["consumed"]["date_sent"]!r}, read {rec["consumed"]["date_read"]!r}')
    if not rec['product']['delete_readback']:
        print(f'note: delete "{PROBE_NAME}" in the app; its removal was not confirmed')
    return 0


def main(argv=None, transport=None):
    ap = argparse.ArgumentParser(description='Optional unofficial YAZIO adapter. Secrets are '
                                 'only ever read from hidden prompts, never from arguments.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('setup', help='write the private config (interactive)')
    sub.add_parser('login', help='password login, stores access + refresh tokens (interactive)')
    sub.add_parser('logout', help='remove the stored token')
    st = sub.add_parser('status', help='offline summary, no secrets')
    st.add_argument('--json', action='store_true')
    v = sub.add_parser('verify', help='read-only account check')
    v.add_argument('--search', help='optional text or barcode to search')
    w = sub.add_parser('verify-write', help='opt-in live write contract check (interactive)')
    w.add_argument('--i-consent-to-test-writes', action='store_true')
    w.add_argument('--amount', type=float, required=True,
                   help=f'grams to log of the test product (max {PROBE_MAX_AMOUNT})')
    args = ap.parse_args(argv)
    if args.cmd in ('setup', 'login', 'verify-write') and not _interactive():
        print(f'{args.cmd} needs an interactive local terminal; {LOCAL_ONLY} {args.cmd}')
        return 2
    commands = {'setup': cmd_setup, 'login': cmd_login, 'logout': cmd_logout,
                'status': cmd_status, 'verify': cmd_verify, 'verify-write': cmd_verify_write}
    try:
        return commands[args.cmd](args, transport)
    except (YazioError, ValueError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('aborted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
