# YAZIO adapter (optional, unofficial, off by default)

`skill/scripts/nutrition_yazio.py` can mirror entries from the local food log
into a YAZIO account. It uses only the Python standard library. The local
journal stays the source of truth. If YAZIO is unconfigured, unreachable or
unverified, entries stay local, and a local save is never reported as a YAZIO
export.

YAZIO has no public API. The wire format comes from the MIT-licensed community
description [yazio-community/yazio-api-specification `v0.4.0`](https://github.com/yazio-community/yazio-api-specification/tree/v0.4.0)
(API `/v22`), which was derived from app traffic and is incomplete. No other
client's code is used. MIT licenses the description, not the use of the service.

**Status of this code:** the adapter is tested offline against mocked contracts
only. Nobody has run it against a live account. Every capability starts as
`researched` and only becomes `live_confirmed` once the account owner runs
`verify-write` (below) on their own account.

## Turning it on (account owner, local terminal only)

Run these yourself in a terminal on the coach machine. Never paste a password,
token or client secret into a chat, a Telegram message or a command line. The
tool only reads secrets from hidden `getpass` prompts.

```bash
python3 skill/scripts/nutrition_yazio.py setup      # app client id/secret, User-Agent, search params
python3 skill/scripts/nutrition_yazio.py login      # e-mail + password; stores access + refresh tokens
python3 skill/scripts/nutrition_yazio.py status     # offline: what is configured / confirmed
python3 skill/scripts/nutrition_yazio.py verify     # read-only GETs against the account
python3 skill/scripts/nutrition_yazio.py verify-write --i-consent-to-test-writes --amount 1
```

- **App credentials and User-Agent** come from your own setup. The repository
  ships none, and copying public example values is not a security approval.
  YAZIO blocks unknown app versions with `403 version_blocked`. When that
  happens the adapter stops and asks you to update `user_agent`. It does not
  cycle through versions.
- **Search parameters** (`sex`, `countries`, optional `locales`) are whatever
  your account uses. Leaving one blank disables search. Nothing is inferred
  from a profile or a name.
- **Product category and base units** (`g`, `ml`) have no documented enum.
  Leave the category blank until you know a value YAZIO accepts. Blank means
  private-product writes stay disabled, and no default is invented.

## Files and security

Default location: `$XDG_CONFIG_HOME/coach-claudio/` (normally
`~/.config/coach-claudio/`), or `YAZIO_CONFIG=/abs/path/yazio.json`.

| File | Holds |
|---|---|
| `yazio.json` | client id/secret, User-Agent, timezone, search, product settings |
| `yazio-token.json` | access + refresh token, expiry (never the password) |
| `yazio-contract.json` | what `verify-write` observed on the account |
| `yazio-probe.json` | immutable payloads, amount, timestamp, observations and durable recovery phase |
| `yazio-probe-abandoned-<uuid>.json` | previous-login cleanup evidence; inspect that account manually |
| `yazio.lock` | inter-process lock for token refresh |

- The directory must be `0700` and the files `0600`, owned by you. Symlinks
  and paths inside the repository or `COACH_HOME` are refused. Writes are
  atomic (temp file, fsync, rename).
- Token refresh runs under the lock. It re-reads the token file first, so a
  rotation by another process wins. The rotated pair is persisted before it is
  used. A rejected refresh marks the token revoked and stops; it never falls
  back to a password, which is not stored. Run `login` again.
- HTTPS only to `https://yzapi.yazio.com/v22/...`. Verified TLS, every request
  has a timeout, no proxy environment, and redirects are refused, so a token
  can't follow one. There's no host setting.
- JSON is strict: `NaN`/`Infinity` are refused both ways. Errors show the HTTP
  status and at most a short snake_case `error` code, never a response body.
  Secrets never appear in `repr`, logs or messages.

## Capability markers

`Client.capabilities()` and `status()` report, per operation and per nutrient:

- `researched`: from the pinned spec only. Writes are refused (`ContractUnverified`).
- `live_confirmed`: seen by `verify-write` on this account, read back exactly.
- `not_accepted`: sent during `verify-write` but not stored. The nutrient stays
  local.
- `unverified` (salt only): see below.

Editing the config (`"verified": true` or similar) changes nothing. Only a
well-formed contract record written by `verify-write` counts, and only for the
configured category and base unit. Its digest catches hand edits and stale
schemas. It is a consistency check, not a signature, since the account owner
can always edit local files.

### What `verify-write` does

It writes to the real account, so it needs the flag plus typed `WRITE`
confirmation:

1. Persists two fresh UUIDs, timestamp, amount and complete immutable payloads
   with a recovery phase before any remote mutation.
2. Creates a private product with all 7 nutrients at distinct non-zero values,
   reads it back, and records which keys came back as sent.
3. Asks whether the app shows salt as `0.34 g` **or equivalently `340 mg`**
   per 100 g. Salt is only mapped once you confirm that unit-aware display.
4. Logs `--amount` g of it (max 100), reads the day back, and compares id,
   product id, amount, daytime, wall-clock time and explicit null `serving` /
   `serving_quantity` (base-unit semantics).
5. Durably enters cleanup before deleting the entry, checks absence, then
   deletes the product and checks absence. Unconfirmed cleanup retains state
   and does not publish a new contract.
6. Writes `yazio-contract.json` only after both removals are confirmed.

If a step times out it ends with UNKNOWN. Rerunning uses the stored payloads
and original timestamp; a different `--amount` is rejected before remote access.
Once cleanup starts, reruns only resume cleanup and never create again. Legacy
pending files without immutable payloads/phases fail closed: inspect their
artifacts manually, then use `login` to archive the evidence before a new probe.
Every explicit `login` invalidates the old contract, even for the same account,
because no stable verified account id is available. It moves unfinished probe
evidence into a private abandoned-cleanup report before switching tokens. It
never deletes old-account artifacts on the new login; inspect the previous
account manually. Token refresh does not invalidate proof, and rotating tokens
are not used as account identity.
Remote test entries are only created by this command, never on setup, login or
enable. It covers `g` products; `ml` products stay unconfirmed.

## Nutrients and units

| Local (per 100 g/ml) | YAZIO product key (per 1 g/ml) |
|---|---|
| `energy_kcal` | `energy.energy` (kcal per base unit, e.g. oil 8.84) |
| `protein_g` | `nutrient.protein` |
| `carbohydrate_g` | `nutrient.carb` |
| `fat_g` | `nutrient.fat` |
| `fiber_g` | `nutrient.dietaryfiber` |
| `sugars_g` | `nutrient.sugar` |
| `salt_g` | `nutrient.salt`: **not sent** until salt is `live_confirmed` |

- Every value is divided by 100 in `Decimal`. Energy stays kcal; kJ/J and
  sodium conversion belong to the nutrition core, and the adapter only accepts
  the canonical keys (no `energy_kj`, no `sodium_*`, so nothing is duplicated).
- An unknown value (`None`) is left out of the payload, never sent as 0. If
  YAZIO then reports a value for it, the read-back carries a warning.
- **No seven-nutrient guarantee:** a nutrient that isn't `live_confirmed` is
  listed in `local_only` with a warning. YAZIO's daily totals endpoint only has
  kcal and macros, so keep totals locally.

## API for the parent sync

```python
import nutrition_yazio as ny

ny.status() -> dict                       # offline; configured, logged_in, can_write, capabilities
c = ny.Client.load(transport=None)        # NotConfigured / InsecureConfig when not set up
c.capabilities() -> dict

# deterministic, no I/O; ids are supplied by the caller's outbox and never generated
ny.prepare_product(per, nutrients, product_id, name, *, category, base_units,
                   salt_write=False, keep_local=(), producer=None)
c.prepare_product(per, nutrients, product_id, name, producer=None)
    -> {'payload', 'synced', 'unknown', 'local_only', 'warnings', 'fingerprint'}
ny.prepare_consumed(consumed_id, product_id, amount, unit, when, daytime, *, tz, base_unit)
c.prepare_consumed(consumed_id, product_id, amount, unit, when, daytime, base_unit)
    -> {'payload', 'day', 'local_time', 'warnings', 'fingerprint'}

# reads (GET, bounded retry)
c.get_product(product_id) -> dict       # id kept from the request; per_unit maps confirmed keys
c.search(query) -> list                 # barcodes as strings (leading zeros)
c.find_branded(barcode, per, nutrients) -> {'status': 'match'|'none', ...}
c.own_product_ids() -> list
c.get_consumed(day) -> {'products', 'recipe_portions', 'simple_products'}
c.reconcile_product(payload) / c.reconcile_consumed(payload)
    -> {'status': 'absent'|'confirmed'|'mismatch', ...}      # read-only

# writes (refused unless live_confirmed; single send, never retried)
c.create_private_product(payload) -> {'status': 'confirmed', 'already_present': bool, ...}
c.create_consumed(payload)        -> {'status': 'confirmed', 'already_present': bool, ...}
c.delete_consumed(consumed_id, day) -> {'status': 'deleted'|'absent', ...}
```

Exceptions: `ContractUnverified`, `UnknownOutcome` (`.op .id .day`),
`ReadbackMismatch` (`.diffs`), `AuthError`, `VersionBlocked`,
`YazioHTTPError` (`.status .code`), `TransportError`, `ProtocolError`,
`NotConfigured`, `InsecureConfig`. All of them subclass `YazioError`.

How the parent should use it:

- Persist the outbox row, with stable product and consumed UUIDs and the
  payload fingerprint, **before** calling a write. The same Telegram delivery
  must not mint new ids. The UUID is for correlation only; nothing documents
  server idempotency.
- A write succeeds only when it returns `confirmed`, which means the read-back
  matched. A `204` alone is never success.
- `UnknownOutcome` covers a timeout, a 5xx, an unreadable success response or
  a failed read-back after sending. Mark the row `unknown`, don't resend, and
  call `reconcile_*` with the same payload later. Stop the export if it stays
  ambiguous. Writes are never retried automatically.
- Every create reconciles first: an id that's already present and identical is
  `confirmed` without a second POST, and a different one is `ReadbackMismatch`.
  Consumed-day reads require all three bucket keys with actual lists; missing,
  null or false buckets are protocol errors, never proof of absence for create
  or delete.
- Prefer `find_branded`: an exact EAN string, the same base unit, and every
  locally known nutrient agreeing within label rounding. Otherwise use one
  immutable private product per local item version, with a new UUID when
  values change. A photo estimate becomes a private product only when the core
  has a reliable weight and per-100 basis. Without one it stays local, with no
  invented gram amounts.
- There's no documented PUT/PATCH or upsert. A correction is a saga run by the
  parent with stable ids and read-back at each step. Create-then-delete risks a
  brief duplicate, delete-then-create a brief gap; choose explicitly and keep
  failures visible.
- Timestamps: pass timezone-aware datetimes. They're sent as local wall-clock
  time without an offset, in the configured timezone. Around a DST fall-back
  hour the same wall clock occurs twice, and the server's reading is
  unverified, so a warning is returned.

## Known limitations (until live runs exist)

- The UA/version check, the minimal accepted product payload (`servings: []`,
  category values), salt/sodium scaling, how the server reads timestamps,
  whether resending the same UUID duplicates, upserts or errors, and refresh
  token rotation are all unconfirmed until `verify-write` and real use.
- If a refresh response is lost after the server rotated the token, the old
  refresh token may be dead. The next refresh fails and asks for `login`.
- `delete_consumed` and reconcile only look at the given day. An entry stored
  under another date isn't found.
- Recipes, simple products, serving units, barcode publishing and photo
  uploads are out of scope.

Tests: `python3 -m unittest tests/test_nutrition_yazio.py` (offline; isolated
`HOME`, `XDG_CONFIG_HOME`, `COACH_HOME` and `TMPDIR`; a fake transport, with the
real one guarded).
