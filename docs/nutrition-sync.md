# Explicit durable nutrition → YAZIO sync

`python3 skill/scripts/nutrition_sync.py status` is offline and read-only.
`python3 skill/scripts/nutrition_sync.py run` is the **explicit** export command.
Nothing installs a scheduler, contacts YAZIO on local add, or changes the ledger.
The canonical source for logged meals remains the local nutrition ledger; do not
sum its intake with the same meals imported from YAZIO/Apple Health.

## Setup → verify → enable → run

Run from the checkout, with `COACH_HOME` pointing at your own coach installation
if it is not this checkout. Never put secrets in chat, arguments, Git, or
`athlete/config.toml`. Follow [the adapter setup](nutrition-yazio.md) for the
external private credential store, accepted category, app credentials and
current User-Agent. The account owner must run these in a local terminal:

```sh
python3 skill/scripts/nutrition_yazio.py setup
python3 skill/scripts/nutrition_yazio.py login
python3 skill/scripts/nutrition_yazio.py verify
python3 skill/scripts/nutrition_yazio.py verify-write --i-consent-to-test-writes --amount 1
python3 skill/scripts/nutrition_yazio.py verify-write --i-consent-to-test-writes --base-unit ml --amount 1
python3 skill/scripts/nutrition_yazio.py status
```

`verify` is read-only; it does **not** authorize writes. `verify-write` deliberately
creates and removes a test snack and asks about salt display. Only its recorded,
read-back-verified contract enables writes for the selected base unit. The default
is **g**; verify **ml** independently with `--base-unit ml`. Each unit also needs
its own actual user confirmation of the app's salt display before known salt
can sync. A gram proof or config allowing ml never proves ml. Do not hand-edit
a contract to claim support. Re-login invalidates **all** prior unit proofs;
repeat verification as required by the adapter. Resume a pending probe with its
original amount and unit before starting another.

Then set these booleans in your **private** athlete config (merge into an existing
`[integrations]` table rather than creating a duplicate table):

```toml
[integrations]
nutrition_log = true
nutrition_yazio = true
```

Both default to false. Missing/false/non-boolean values cannot authorize sync.
Logging and any provider lookup remain independent of YAZIO export.

### Optional export without salt

If you deliberately do not need salt in YAZIO, merge this into your private
`[nutrition]` table:

```toml
[nutrition]
yazio_omit_salt = true
```

The default is `false`; only the literal boolean `true` opts in (not `"true"`
or `1`). This omits **only canonical `salt_g`** from new export drafts, for both
g and ml, including salt derived from sodium. Salt traces alone then no longer
block export. Every other qualified/trace value and every remaining nutrient,
operation and per-unit capability gate stays unchanged. No fake salt zero is
sent and no account salt capability is marked verified. Known local salt,
all seven local nutrients, receipts and immutable outbox snapshots are retained.

JSON status includes top-level `omitted_nutrients: ["salt_g"]` when configured;
this describes the current policy, even if a food has no known salt. Each
prepared job also persists its policy, exposed in the entry's `omissions` list
with revision and item index. These notices do not turn a confirmed export into
a failure: `synced` under this policy means the exported subset, **not salt**.
Persisted notices remain visible after disabling the option.

Already-prepared/in-flight or completed jobs are not rewritten or re-exported
when the setting changes. Retries use the original immutable draft and its
recorded omission policy. A blocked job without a draft can retry with the new
setting. This option neither activates sync nor installs a scheduler; both
integration flags and the normal explicit run are still required.

```sh
python3 skill/scripts/nutrition_sync.py status
COACH_DRY_RUN=1 python3 skill/scripts/nutrition_sync.py run
python3 skill/scripts/nutrition_sync.py run
python3 skill/scripts/nutrition_sync.py status
```

`COACH_DRY_RUN=1` and disabled runs never create/update the outbox, take a write
lock, load credentials, or contact the adapter. `status` never loads credentials
or contacts the adapter either. It includes local meals not yet queued. After
disabling the integration, already queued/unknown work is preserved without
attempts; explicitly re-enable and run to resume it.

Commands print JSON. `pending`, `synced`, `blocked`, `unknown` count **latest local
entries/meals**, not individual foods. Per-entry reasons expose unsupported
items and unfinished earlier-revision cleanup. A partly exported multi-item meal
is never `synced`. `run` exits 1 if work remains pending/blocked/unknown; `status`
exits 1 for blocked/unknown work. A disabled run with local pending work therefore
also exits 1, not a misleading export success. Lock contention/security/database
errors exit 1 with an error object. An empty queue is successful even if disabled.

## What is stored, and when

The existing private ledger is read from `cc.NUTRITION_DIR/ledger.sqlite3`, with
SQLite `mode=ro`, joining each entry's latest revision, including tombstones.
There are no changes to `nutrition_store.py` and no storage API hooks.

The separate `sync_outbox.sqlite3` lives beside it, in a private `0700` directory;
the database and `sync_outbox.lock` are `0600`, owned by the current user. Unsafe
paths/symlinks are refused on write. A non-blocking **fcntl advisory lock** covers
the whole queue/reconcile/write/checkpoint cycle; its inode is never unlinked.
File age is irrelevant. Another runner must wait until a later invocation.

Each `(entry_id, revision, item_index)` gets immutable product and consumed UUIDs.
All UUIDs are committed in one transaction before credentials are loaded. Prepared
product/consumed payloads are committed before any remote write. An in-flight
step is durably marked `unknown` **before** calling the adapter, so a process
crash cannot turn an ambiguous send into an ordinary unsent job.

The engine calls the actual adapter APIs: `prepare_product`, `prepare_consumed`,
`reconcile_product`, `create_private_product`, `reconcile_consumed`,
`create_consumed`, `get_consumed`, and `delete_consumed`. A private product is
created per local item revision; there is no branded/shared-product mutation.

## Supported snapshots and honest blocking

- Canonical energy, protein, available carbohydrate, fat, fiber, sugars and salt
  are passed to `prepare_product`; the adapter maps per-100 values to per-base-unit
  fields. The explicit option above excludes only salt from that export copy.
  Unknown nutrients stay unknown, never zero.
- `100g` and `100ml` snapshots require an explicit eaten quantity of the same
  dimension. Mass/volume unit conversions use the core's deterministic tables.
  No ml→g density assumption or guessed serving size is made.
- A `total` photo/user snapshot can be converted to per-100 only when its original
  item includes an explicit measured or explicitly estimated positive g/ml amount.
  A calorie-only plate with unknown grams remains blocked with `missing amount`.
  The local provenance/uncertainty remains authoritative; an export of an estimate
  does not make the estimate measured. YAZIO's numeric product fields cannot
  encode local confidence/ranges.
- Serving-basis snapshots and serving counts are blocked; resolve to an explicit
  g/ml amount and supported basis locally. Qualified/trace nutrient values are
  blocked rather than flattened to exact numbers (including false zero), except
  deliberately omitted salt when `yazio_omit_salt = true`.
- No configured client, researched/unverified operation, mismatching base-unit
  capability, unaccepted known nutrients, unverified non-omitted salt, and ambiguous DST wall
  times remain blocked with the adapter/core reason. There is no seven-nutrient
  export claim when the account only accepts some of them.
- The runner selects `by_base_unit` capabilities using each item's actual g/ml
  dimension, and passes the prepared product unit to consumed writes/deletes.
  It retains compatibility with a legacy single-unit adapter contract, without
  widening its scope. Known nutrients must all be confirmed for that unit;
  otherwise the item stays blocked and its full local nutrition is unchanged.

## Readback, retry, corrections and deletion

A `204` or successful process is not acknowledgement. Creates must return
`confirmed` **and** pass a subsequent reconciliation. Lost responses, mismatches,
failed reads and interrupted sends are `unknown`, never blindly retried. A later
explicit run reconciles the same UUID/payload first. Only an authoritative
`absent` permits another create with those **same** IDs; confirmed presence
advances without another create. A mismatch/read failure stops that step.
There is no claim of server-side UUID idempotency or exactly-once delivery beyond
the server's actual read consistency. Do not delete/rebuild the outbox to retry.

Corrections use a serial **delete-old, then create-new** saga. Every owned prior
revision's consumed ID is kept until its absence has been read back. An earlier
unknown create is reconciled before retirement. Until all earlier cleanup finishes,
no new revision is sent. A crash after deletion resumes cleanup/readback and then
creates the new version with newly persisted IDs; it never mutates an old UUID's
payload. This can leave a visible gap while a replacement is blocked/offline,
not a hidden claim that both versions are synced.

Local delete/erase tombstones remove **all** previously owned consumed entries,
not just IDs present in the latest local snapshot. Remote absence must be checked
even after a successful delete response. Missing delete capability, failed reads
or unknown remote outcomes remain pending/blocked/unknown and are reported as
such. Local erase by itself is **not** proof of remote erasure: run sync afterward
and inspect status.

After each confirmed retirement, archived snapshot/product/consumed JSON is
scrubbed from the sync DB (`secure_delete=ON`); only correlation IDs/checkpoints
remain. Successful erase also clears any remaining archived JSON. Unknown remote
cleanup retains the recovery data until it can finish, and never reports success.
Backups/filesystem snapshots and the original local ledger's own retention rules
are separate. This runner does **not** delete YAZIO product definitions: product
removal is optional and the adapter has no public general-purpose delete-product
method. Private definitions may remain in YAZIO's product catalogue and can be
removed manually in the app. No shared/branded product is ever deleted. Thus
`synced` on an erased meal means consumed-entry removal and local outbox scrubbing,
not a promise of account-wide product/backup erasure.

## Offline verification and limits

```sh
python3 -m unittest discover -s tests -p test_nutrition_sync.py
```

Tests use real temporary SQLite databases (including the actual Ledger API), tiny
injected adapter classes, and synthetic foods/identities. CLI subprocess tests
construct isolated HOME/COACH_HOME/XDG_CONFIG_HOME/TMPDIR. They cover durable IDs,
pending/unconfigured retry, lost responses, crash recovery, repeated calls,
disable/dry-run, live capability gates, g/ml blocking, missing photo quantity,
partial meals, delete/recreate correction, unknown earlier revisions, erase
scrubbing, and actual lock contention. No live account calls were made. A user
must supply their own credentials and perform the explicit live verification;
this implementation cannot truthfully claim that step has passed. Use one YAZIO
account per ledger/outbox: finish remote cleanup before switching accounts or
moving/deleting the outbox. The current adapter exposes no verified account-ID
binding, so an account switch cannot prove absence in the original account.

For an optional parent CLI wrapper, call `nutrition_sync.main(['status'])` or
`nutrition_sync.main(['run'])`. It reads the same configuration gates and returns
an exit code. Code callers can inject `Sync(directory, client_factory=...,
enabled=explicit_boolean, dry_run=..., omit_salt=explicit_boolean)`; sync and salt
omission both default to false. The standalone
CLI does not depend on a parent wrapper being installed.
