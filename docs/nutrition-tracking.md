# Nutrition tracking (optional)

An optional food log: the athlete sends a food photo, a nutrition label
with the amount, a barcode (photo or typed), or plain text, and the coach
keeps a local, private record — calories, protein, carbohydrate, sugars,
fat, fibre and salt.

It is **off by default**. Nothing about it runs, asks or nags until you
switch it on. It has no weight-loss goal and no calorie target built in:
it records what was logged, and the coaching stance stays the one in
`skill/references/nutrition.md`.

## Switching it on

In `athlete/config.toml`:

```toml
[integrations]
nutrition_log = true

[nutrition]
country = "de"          # product lookups: ISO 3166-1 alpha-2, or "world"
language = "de"         # product names: ISO 639-1
contact = ""            # optional e-mail/URL for the User-Agent
lookup_timeout_s = 10   # per request, at most 30
offline = false         # true = cached OFF products only; BLS stays local
off_api_version = "v3.6" # current nutrition schema; "v2" explicit legacy mode
# bls_db_path = "..."   # optional local SQLite path; otherwise nutrition_bls default
```

The defaults target the German market. Barcode lookups accept any OFF country;
text search maps de/at/ch/fr/gb/us to known country tags, or uses `world`.
Other countries fail explicitly rather than invent a taxonomy tag.
`python3 skill/scripts/nutrition.py status` shows what is on and
where the data lives.

## How it works

Recognition and arithmetic are split on purpose. The model (in the normal
Telegram or terminal conversation, reading the image with its own vision —
no extra AI provider, no extra cost) reads what is on the label, the digits
of a barcode, or what is on a plate, and writes it down as JSON. The script
does every number: unit conversion, scaling to the eaten amount, kJ→kcal,
sodium→salt, totals. The conversation workflow is
`skill/references/nutrition-logging.md`.

- **Clear inputs are logged directly**: a label plus an amount, a barcode
  plus an amount, a reasonable food-photo estimate. The reply is a one-line
  receipt; estimates are marked `~`.
- **Material gaps are parked, not guessed**: no amount, a serving size
  nobody knows, a product Open Food Facts doesn't have, oil you can't see.
  The meal becomes a *proposal* with one question, and the answer confirms
  it.
- **Unknown stays unknown.** A nutrient no source states is `null`, never
  `0`. Totals say when an item didn't know a value (`fibre ≥4 g`, or
  `fibre ?`).

## Quick start

```bash
S=skill/scripts
python3 $S/nutrition.py status
python3 $S/nutrition.py add '{"meal": "breakfast", "items": [{"name": "Skyr natur", "amount": "250 g", "basis": "100g", "nutrients": {"energy_kcal": 63, "protein_g": 11, "carbohydrate_g": 4, "sugars_g": 4, "fat_g": 0.2, "salt_g": 0.13}, "provenance": {"kind": "label"}}]}'
python3 $S/nutrition.py day
python3 $S/nutrition.py --json add --or-propose '{"items": [{"barcode": "4000000000013", "amount": "1 serving"}]}'
python3 $S/nutrition.py pending
```

The barcode above is synthetic (valid check digit, not a product), so the
lookup comes back as a question: a real product with values and a serving
size would be logged directly. Every command takes `--json` for
machine-readable output; `nutrition.py schema` prints the entry schema.

### Entry examples

A label photo plus amount (values as printed, commas allowed):

```json
{
  "meal": "lunch",
  "items": [{
    "name": "Apfelschorle",
    "amount": "0,5 l",
    "basis": "100ml",
    "nutrients": {"energy_kj": "100", "energy_kcal": "24", "fat_g": "0",
                  "carbohydrate_g": "5,5", "sugars_g": "5,5", "protein_g": "0",
                  "salt_g": "<0,01"},
    "provenance": {"kind": "label"}
  }]
}
```

A per-serving label, eaten by weight (the serving size makes it work):

```json
{
  "meal": "snack",
  "items": [{
    "name": "Oat bar",
    "amount": "60 g",
    "basis": {"per": "serving", "serving": "40 g"},
    "nutrients": {"energy_kcal": 160, "protein_g": 4, "carbohydrate_g": 24,
                  "sugars_g": 9, "fat_g": 5, "fiber_g": 2.4, "sodium_mg": 40},
    "provenance": {"kind": "label"}
  }]
}
```

Two items in one meal, one of them an estimate:

```json
{
  "meal": "dinner",
  "note": "photo of the plate, label of the bread",
  "items": [
    {"name": "Rye bread", "amount": "2 serving",
     "basis": {"per": "100g", "serving": "50 g"},
     "nutrients": {"energy_kcal": 210, "protein_g": 6.5, "carbohydrate_g": 38,
                   "sugars_g": 3, "fat_g": 1.5, "fiber_g": 8, "salt_g": 1.2},
     "provenance": {"kind": "label"}},
    {"name": "Lentil stew", "basis": "total",
     "nutrients": {"energy_kcal": 420, "protein_g": 22, "carbohydrate_g": 55,
                   "fat_g": 10},
     "provenance": {"kind": "estimate", "confidence": 0.6,
                    "uncertainty": "bowl size from the photo, ±25 %"}}
  ]
}
```

Answering a proposal, and correcting an entry, are JSON merge patches;
`items` as an object patches items by index:

```bash
python3 $S/nutrition.py confirm p_0123456789ab '{"items": {"0": {"amount": "180 g"}}}'
python3 $S/nutrition.py correct m_0123456789ab '{"items": {"0": {"amount": "300 g"}}}' --reason "bigger bowl"
```

## The rules the script enforces

- **Quantities** are positive and finite: `g`, `kg`, `mg`, `oz`, `lb`,
  `ml`, `cl`, `dl`, `l`, or `serving`. No NaN, infinity, booleans, signs or
  exponents; `"0,5"` and `"0.5"` both work. Numbers are Decimal internally.
- **Bases**: `100g`, `100ml`, `serving` (with an optional serving size), or
  `total` (the values are for what was eaten — estimates). A volume against
  a per-100 g label, or a mass against per-100 ml, needs
  `density_g_per_ml`; 1 ml is never assumed to be 1 g.
- **Energy**: kcal is canonical; kJ converts at 4.184 kJ/kcal. Both given
  and disagreeing → kept kcal, with a warning. Above 950 kcal per 100 g or
  100 ml is refused as a kJ value typed as kcal.
- **Salt** is canonical; sodium converts as salt = sodium × 2.5 (EU
  Regulation 1169/2011).
- **Carbohydrate** is EU-style *available* carbohydrate; fibre is separate.
  A US-style total carbohydrate (`carbohydrate_total_g`) becomes available
  carbohydrate only when fibre is known, otherwise it stays unknown.
  **Sugars** are part of carbohydrate: sugars above carbohydrate (beyond
  0.5 g of label rounding) are refused as a misread. Per 100 g, protein +
  carbohydrate + fat + fibre + salt above 100 g is refused.
- **4/4/9 check**: energy is compared to 4 kcal/g protein and
  carbohydrate, 9 fat, 2 fibre. A gap over 15 % only **warns** — alcohol,
  polyols and organic acids legitimately cause one; the label is kept.
- **Provenance** on every item: `label`, `provider`, `estimate` (needs a
  confidence 0–1) or `user`. Label values win over the database; both beat
  an estimate.
- **Atomic meals**: every item of a meal validates, or nothing is saved.

## Text: search → choose → quantity → log

The **existing Claude conversation** interprets the language, not a second
model API or a new Telegram bot. It extracts the product query, variant and
amount, then calls deterministic commands. Search returns candidates and
never chooses the first result automatically. Match brand, preparation,
normal/Zero and package identity; package size is not proof of amount eaten.

```bash
S=skill/scripts
python3 $S/nutrition.py --json search 'Snickers' --source off
python3 $S/nutrition.py --json propose '{"meal":"snack","source_events":["telegram:example:1"],"items":[{"query":"ein Snickers"}]}'
# Use the actual returned proposal ID and a user-confirmed candidate GTIN:
python3 $S/nutrition.py --json choose "$PROPOSAL_ID" --barcode "$SELECTED_GTIN"
# Reply "50 g" completes that original meal, not a second add:
python3 $S/nutrition.py --json confirm "$PROPOSAL_ID" --amount '50 g' --source-event telegram:example:2
```

`PROPOSAL_ID` and `SELECTED_GTIN` are shell variables to set from the actual
outputs/selection, not built-in example products. The observed live search
included a 42 g Snickers and a multipack; neither establishes that the
athlete ate 50 g. Ask one short size/variant question. `choose` fetches the
selected product snapshot and persists it even while quantity is missing.
`confirm --amount` keeps the answer pending if identity/nutrition remains
unknown; `pending` shows the same query and proposal ID. Reply source events
accumulate and dedupe with the original message. Complete inputs use `add`
directly, without a mandatory confirmation round.

- “eine Cola”: ask normal/Zero **and** volume together; retain the query.
- “330 ml Coca-Cola Zero”: search, verify exact variant, then `add` the
  selected barcode with `amount: "330 ml"`; do not ask again for the volume.
- “200 g Magerquark”: `search 'Magerquark' --source bls`, choose the matching
  generic food (not a branded average), then `add` with `bls_code` and amount.
- “eine halbe Pizza”: confirm which pizza and size/weight or fraction of a
  known package; no invented pizza weight or database nutrient values.

```bash
python3 $S/nutrition.py --json search 'Magerquark' --source bls
python3 $S/nutrition.py --json search 'Haferflocken' --source both
python3 $S/nutrition.py --json lookup "$SELECTED_BLS_CODE" --source bls
python3 $S/nutrition.py --json add "{\"items\":[{\"bls_code\":\"$SELECTED_BLS_CODE\",\"amount\":\"200 g\"}]}"
```

No matching product/outage is distinct from a valid zero-nutrient product.
Ask for the label or barcode; never silently replace a failed search by
an estimate. `--source both` keeps source attribution separate and reports
partial-provider errors; it never mixes nutrient fields across sources.

## Barcodes and Open Food Facts

Barcodes are GTIN-8/12/13/14, kept as digit strings (leading zeros
preserved) and checked against their check digit before anything else
happens. A barcode-only item is filled in from the
[Open Food Facts](https://world.openfoodfacts.org) product API (v3.6,
read-only): `GET /api/v3.6/product/<gtin>.json?fields=…&cc=<country>&lc=<language>`
with a `ClaudeRunningCoach-Nutrition/1.0` User-Agent (plus your `contact`),
a finite timeout, no redirects, a 2 MB response cap, and at most 10 lookups
a minute. The host is fixed in code — nothing in a message can point the
request elsewhere.

Not found, no nutrition values, an unknown per-100 g/ml basis or a missing
serving size become a question for the athlete (usually: a label photo).
An outage or rate limit is an error; there is never a fallback to generic
or invented values. The v3.6 parser reads `nutrition.aggregated_set`, uses
only packaging values with one source index, coherent per-100 g/ml basis
and `as_sold` preparation, and excludes estimated ingredient-derived fibre.
Units are validated (g/mg, kcal/kJ); missing fields remain null. It never
silently parses a v3 response as v2. The explicit legacy config `v2` retains
the former package-unit basis inference, not density inference. The old
Python constructor's v2 default is retained for library compatibility;
the application explicitly requests v3.6 unless configured otherwise.

Text search uses the separately documented official Search-a-licious API:
`GET https://search.openfoodfacts.org/search` with `q`, `langs`, `page_size`,
`page` and `fields` ([live OpenAPI](https://search.openfoodfacts.org/openapi.json)).
User text becomes literal terms: every Lucene syntax character is escaped and
`AND`/`OR`/`NOT` are lowercased (a quoted phrase matches nothing on
Search-a-licious, observed Oct 2026). The country filter is built internally.
`hits` are candidate metadata, not nutrition snapshots. Search and product
requests have separate 10/minute budgets, bounded response size, no redirects
and the same custom contact User-Agent. Product cache keys separate v2/v3.6.

Products are cached in their own file, separate from the private ledger:
found products for 30 days, not-found for a day, errors never.
`nutrition.py lookup <gtin>` shows a product and its source.

**Attribution.** Product data comes from Open Food Facts: the database is
available under the [Open Database License (ODbL 1.0)](https://opendatacommons.org/licenses/odbl/1-0/),
individual contents under the [Database Contents License (DbCL 1.0)](https://opendatacommons.org/licenses/dbcl/1-0/).
Every provider-filled item keeps this attribution in its provenance. No
product images are fetched.

**Optional local decoder.** If `zbarimg` (from the zbar tools) is
installed, `nutrition.py image scan <token>` decodes the barcode in a
staged photo. Nothing is installed or downloaded; without it the model
reads the printed digits and the check digit catches misreads.

## Storage and privacy

Everything is local, under `athlete/state/nutrition/`:

| file | what | |
|---|---|---|
| `ledger.sqlite3` | the private food log: entries, revisions, proposals, day marks | mode 600 |
| `provider_cache.sqlite3` | public product data from Open Food Facts | deletable any time |
| `inbox/` | the feature's own temporary image copies | mode 700, files 600 |

OFF receives the searched product text or barcode, country/language and
User-Agent, never the meal ledger or image. BLS searches stay local.
Optional Yazio synchronization is a separately gated local outbox; see
`nutrition-yazio.md` and the sync command's status. A local receipt is not
a remote acknowledgement. Never sum this canonical meal ledger with the
same Yazio nutrition exported through Apple Health.

**Images.** For a Telegram photo the plugin has already saved the original
in its own inbox, and the model has read it in the conversation.
`nutrition.py image stage` copies it into the feature's inbox; the copy is
deleted after the meal is logged (or the proposal confirmed or cancelled),
and only its SHA-256 stays in the entry. `image discard` deletes a copy by
token and never accepts a path; it refuses anything in the inbox that
isn't a regular file. `image purge` removes stale copies of photos that
were never logged (older than 24 h). The feature **never** deletes the
channel's original, other files, or Telegram messages, and it cannot erase
what Telegram or the model provider retain under their own policies — so it
never claims to.

**Deleting meals.** `delete` writes a tombstone: the entry disappears from
days and lists, its history stays. `delete --erase` additionally blanks the
content of every revision (SQLite `secure_delete` is on) and keeps only the
id, its timestamps and the message ids, so a redelivered message can't log
it again. If `athlete/` is backed up as a git repository
(`docs/deployment.md` "Backup"), earlier commits of the ledger keep what was
there; the athlete `.gitignore` template excludes the image inbox and the
product cache.

## Dedupe

Every Telegram message is a source event (`telegram:<chat_id>:<message_id>`).
A message already in the ledger — redelivered, or one of several photos of
the same meal — returns `duplicate` with the existing entry instead of a
second meal. The same image hash or the same barcode is **not** a
duplicate: one photo or product can be two meals.

## Days are partial unless marked complete

`nutrition.py day` shows a day's totals with its status. Every day is
**partial** — "logged so far, not total intake" — until the athlete says
it was everything and the coach runs `nutrition.py mark <date> complete`
(refused while proposals for that day are open; `mark <date> partial`
reopens it). `nutrition.py days --from … --to …` averages complete days
only, and gives no average for a nutrient that a complete day doesn't
fully know.

## Dry run

With `COACH_DRY_RUN=1` every command validates and computes as usual and
writes nothing: no ledger is created or changed, no product is cached, no
image is copied or deleted. Staging then returns the original path.

## Exit codes

0 ok (including `duplicate`), 2 invalid input (nothing saved), 3
information missing (ask), 4 the food log is off, 5 unknown id or conflict,
6 product lookup failed.

## BLS local database and qualified nutrients

Use the standalone importer described in [nutrition-bls.md](nutrition-bls.md).
The default SQLite is `athlete/state/nutrition-providers/bls.sqlite3`, separate
from the private ledger and OFF cache; `nutrition.bls_db_path` overrides it.
No BLS database is copied into this repository. Per 100 g edible portion,
available `carbs_g` maps to canonical `carbohydrate_g`. Missing, trace and
limit-qualified BLS values remain null and their qualifiers are recorded in
provenance notes. Rows marked `review` or `blocked` cannot be automatically
logged: use verified label values rather than bypass the errata guard.
The original label/user nutrient snapshot always takes priority.

Attribution: Max Rubner-Institut (2025), Bundeslebensmittelschlüssel,
Version 4.0, DOI [10.25826/Data20251217-134202-0](https://doi.org/10.25826/Data20251217-134202-0),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de).
Sources: [official download](https://blsdb.de/download),
[BLS documentation](https://blsdb.de/assets/uploads/BLS_4_0_Dokumentation_DE.pdf),
[erratum](https://blsdb.de/assets/uploads/Erratum_BLS_4_0_DE.pdf).

A label `<0.5` is **not measured zero**: `nutrient_bounds` records the scaled
interval, and receipts render e.g. `salt <1 g` for 200 g. The legacy numeric
field is the conservative lower bound for compatibility, not an exact
measurement; JSON consumers must honor `trace`, `nutrient_bounds` and totals
`bounds`. Unknowns are still null. Qualified complete-day nutrients do not
produce an exact scalar average. No ml→g density is inferred.

## Verification

```bash
python3 -m unittest discover -s tests -p test_nutrition.py -v
python3 -m unittest discover -s tests -p test_nutrition_integration.py -v
python3 -m unittest discover -s tests -p 'test_nutrition*.py' -q
```

All fixtures are synthetic; every test uses a temporary `COACH_HOME` and
`HOME`; provider HTTP goes to an injected transport or a loopback server.
No external network, no accounts, no athlete data. The integration tests
exercise recorded-schema OFF responses and synthetic BLS workbooks through
the real parser/importer/resolver/ledger. These are **not** live-account or
live-OFF acceptance tests. Public read-only OFF/BLS smoke verification is
separate; Yazio login/write/readback and ml capabilities require explicit
user provisioning and remain unverified until actually exercised.
