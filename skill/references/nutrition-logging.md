# Nutrition logging — meals from photos, labels, barcodes and text

Read this when the food log is on (`coach.py show` lists `nutrition_log`
under "on") and the athlete sends a meal: a food photo, a nutrition label,
a barcode, or "had 200 g skyr". With it off, the food log doesn't exist:
answer the food question, don't log, don't offer it.

Fuelling *advice* is still `references/nutrition.md`. This file is only
about keeping the record.

## Stance

- **They log; you never ask them to.** No reminders, no "you haven't logged
  lunch", no streaks. A photo they send is logged; silence is silence.
- **No judgement and no goals you weren't given.** Never frame a receipt
  against a deficit, a target or weight loss unless the profile's "Food"
  section explicitly makes weight a goal — and even then not in the receipt.
  No "good"/"bad" food, no comment on a pizza.
- **A partial day is not what they ate.** Days are partial until the
  athlete says a day is complete. Never present a partial total as intake,
  never compare one to their needs.
- **You recognise, the script calculates.** You read the label, the
  barcode digits or the plate and write down what you saw. Scaling,
  kJ→kcal, sodium→salt, servings, totals: all `nutrition.py`. Never do
  nutrition arithmetic in your head and never type a total you computed.
- **Unknown is not zero.** A value the label doesn't show is left out. Never
  write `0` for something you don't know.

## What the Telegram channel gives you

Checked against the telegram plugin's source (claude-plugins-official,
0.0.7). The inbound `<channel>` block's attributes:

- **A photo** (compressed, the normal way to send one) arrives with
  `image_path`: the plugin has already downloaded it into its own inbox.
  The caption is the message text, or `(photo)`.
- **An image sent as a file** arrives with `attachment_kind="document"`,
  `attachment_file_id`, `attachment_mime` (e.g. `image/jpeg`) and
  optionally `attachment_name`/`attachment_size`. Fetch it with the
  `download_attachment` tool (that file_id); it returns a local path.
- `chat_id` and `message_id` identify the message. The source event for
  the ledger is `telegram:<chat_id>:<message_id>`.
- **Not provided: `media_group_id`** (which photos were sent as one album)
  and the update id. An album arrives as separate messages. Grouping is
  your judgement — see "Several photos, one meal".

In the terminal there is no source event; leave `source_events` out.

## The flow for a photo

1. **Stage your own copy** — the original belongs to the channel:

   ```bash
   python3 <skill>/scripts/nutrition.py image stage "<image_path>" --source-event telegram:<chat_id>:<message_id>
   ```

   It prints a token (`i_…`) and the path of the copy. Read *that* path.
   JPEG, PNG, GIF and WebP up to 20 MB; anything else is refused.
2. **Read the image.** Blurry, cut off, glare over the numbers → ask for a
   sharper photo; don't guess digits. `nutrition.py image discard <token>`
   for an unusable one.
3. **Work out what it is** and build the entry (below).
4. **Log it** with the token in `images`. After the commit the staged copy
   is deleted and only its SHA-256 stays in the entry:

   ```bash
   python3 <skill>/scripts/nutrition.py --json add --or-propose @/tmp/meal.json
   ```

   Pass the JSON inline, as `@file`, or on stdin with `-`.
5. **Reply with the receipt** — one line, in their language:
   `Logged: skyr 250 g · 158 kcal · P 27.5 g · C 10 g · F 0.5 g`. An estimate
   gets a `~` (`~650 kcal`), as the script prints it. No confirmation
   questions for a clear input, no commentary.

`--or-propose` parks the meal as a proposal when something material is
missing, and the output carries the `questions` to ask. Ask one short
question, offering the likely answer ("about a tablespoon of oil?"). The
answer goes in as a JSON merge patch:

```bash
python3 <skill>/scripts/nutrition.py --json confirm p_0123456789ab '{"items": {"0": {"amount": "180 g"}}}'
python3 <skill>/scripts/nutrition.py --json pending
python3 <skill>/scripts/nutrition.py cancel p_0123456789ab
```

## Building the entry

Schema: `nutrition.py schema`. Every item needs a `provenance.kind`:
`label` (read off the pack), `provider` (filled in from Open Food Facts),
`estimate` (your judgement — needs `confidence` 0–1, and an `uncertainty`
note), or `user` (numbers the athlete typed). A label in hand beats the
database, and both beat an estimate.

**Nutrition label + amount** — copy the printed values exactly, per 100 g,
per 100 ml or per serving, as the column says. Commas are fine (`"4,5"`),
and so is `"<0,5"` (a qualified bound, not measured zero). The script keeps
the scaled `nutrient_bounds` and renders `<…` in receipts; its legacy numeric
field is only the lower bound. Never quote that scalar as an exact measurement.
Only kJ printed →
`energy_kj`. Sodium printed instead of salt → `sodium_g`/`sodium_mg`; the
script converts with the factor 2.5. A US/Canadian "Total Carbohydrate"
includes fibre → `carbohydrate_total_g`, not `carbohydrate_g`. The amount
comes from the athlete — a photo is no evidence of how much they ate.

```json
{
  "meal": "breakfast",
  "source_events": ["telegram:<chat_id>:<message_id>"],
  "images": ["i_0123456789abcdef"],
  "items": [{
    "name": "Skyr natur",
    "amount": "250 g",
    "basis": "100g",
    "nutrients": {"energy_kj": "268", "energy_kcal": "63", "fat_g": "0,2",
                  "carbohydrate_g": "4,0", "sugars_g": "4,0", "protein_g": "11",
                  "salt_g": "0,13"},
    "provenance": {"kind": "label"}
  }]
}
```

A drink per 100 ml is measured in ml, l or cl. Grams against a per-100 ml
label (or ml against per-100 g) need `density_g_per_ml`; the script never
assumes 1 ml = 1 g. "Half the pack": give the pack as the serving size and
eat half a serving — `"basis": {"per": "100g", "serving": "400 g"}`,
`"amount": "0,5 serving"`.

**Barcode** — digits plus amount; the script looks the product up in Open
Food Facts (country and language from the config, cached):

```json
{
  "meal": "snack",
  "items": [{"barcode": "4000000000013", "amount": "1 serving"}]
}
```

If `zbarimg` is installed, `nutrition.py image scan <token>` decodes the
bars; it's optional. Otherwise read the digits printed under the bars. The
script checks the GTIN check digit: a mismatch means a misread — look again
or ask, never adjust a digit to make it pass. A product Open Food Facts
doesn't have (or has without values, or without a serving size when they
said "one serving") comes back as a question: ask for a label photo or the
amount in grams. An outage or rate limit is an error, not a question:
`--or-propose` parks it and `confirm` retries the lookup later. There is
never a fallback to made-up values. Codes starting 20–29 are in-store
codes for weighed goods and usually aren't in the database.

**Food photo** — an estimate for the whole plate, `basis` `total`:

```json
{
  "meal": "dinner",
  "items": [{
    "name": "Pasta with tomato sauce and parmesan",
    "basis": "total",
    "nutrients": {"energy_kcal": 650, "protein_g": 22, "carbohydrate_g": 95,
                  "sugars_g": 12, "fat_g": 18, "fiber_g": 7},
    "provenance": {"kind": "estimate", "confidence": 0.5,
                   "uncertainty": "portion judged from the photo; oil unknown"}
  }]
}
```

Log an estimate directly when the dish and portion are reasonably clear.
Never claim exact grams from an unscaled photo: state the plausible range
and uncertainty in provenance; a midpoint is a declared estimate, not a
weighed or database-confirmed amount. Photos do not prove quantity eaten.
Ask (propose) only when an unknown would move energy materially — roughly
a quarter or more: hidden oil or butter, a sauce, a portion you can't judge.
Leave out nutrients you can't estimate honestly (fibre, salt).

## Text is a first-class path (existing Claude session)

Understand “Ich esse ein Snickers”, “eine Cola”, “eine halbe Pizza” and
“200 g Magerquark” in this conversation. **Do not call a second model/API**
for parsing. Extract query, variant/preparation and any stated amount; keep
the original name/query when unresolved. Do not supply remembered database
nutrition. The script searches real sources; you choose only after matching
the intended identity, never merely because a result ranks first.

1. `nutrition.py --json search 'Snickers' --source off` for branded products;
   `search 'Magerquark' --source bls` for generic foods; `--source both` keeps
   separate candidate sources. Inspect brand, quantity, source and normal/Zero
   variant. OFF uses official Search-a-licious; BLS is a local SQLite import.
2. If uncertain, persist the original message as **one** proposal:

   ```bash
   python3 <skill>/scripts/nutrition.py --json propose '{"meal":"snack","source_events":["telegram:<chat_id>:<message_id>"],"items":[{"query":"ein Snickers"}]}'
   ```

   Ask one targeted question covering the material gaps: “Welche Größe / wie
   viel Gramm war dein Riegel?” Do not assume a standard 50 g bar. Search may
   show a 42 g bar or a multipack, neither proves what the athlete ate.
3. Store the chosen identity on the returned proposal ID:

   ```bash
   python3 <skill>/scripts/nutrition.py --json choose <proposal_id> --barcode <selected_gtin>
   # For a generic food instead: --bls-code <selected_bls_code>
   ```

   `choose` fetches its actual nutrition snapshot. It remains pending if an
   amount is missing; it does not log or invent a serving size. An explicit
   `--amount '50 g'` can accompany the selection. A new variant selection
   replaces the old provider snapshot rather than mixing nutrient fields.
4. A reply “50 g” or “die 0,33-l-Dose” belongs to the **same** proposal:

   ```bash
   python3 <skill>/scripts/nutrition.py --json confirm <proposal_id> --amount '50 g' --source-event telegram:<chat_id>:<reply_message_id>
   ```

   Parse the latter reply as `0,33 l`; do not pass unparsed natural-language
   units to the deterministic CLI. To merge variant and quantity together,
   `choose … --barcode … --amount '0,33 l'`, then `confirm <proposal_id>`.
   The original and reply source events remain claimed together. Never
   `add` the reply as another meal. If identity/provider data is still
   missing, `confirm --amount` saves the answer on the pending proposal and
   returns `status: pending`, **not logged**. Read the returned status.
5. Reply briefly with the script's receipt only when `status` is `logged`.
   Do not require another human “yes” after the requested amount answer.

### Specific dialogs

- Athlete: “ein Snickers.” Search → several pack sizes. Ask size once.
  Athlete: “50 g.” Match the appropriate product, apply quantity to original
  proposal, confirm, return the deterministic receipt. Do not claim a 42 g
  candidate proves a 50 g pack or choose a multipack automatically.
- Athlete: “eine Cola.” Ask: “Normal oder Zero, und wie viel ml?” Keep raw
  query and any partial answer. “Zero, die 0,33-l-Dose” → match Coca-Cola Zero,
  apply 330 ml on that proposal. Do not use normal Cola nutrition.
- Athlete: “330 ml Coca-Cola Zero.” Identity and quantity are already clear:
  search and verify the exact variant, then `add` selected barcode + amount
  directly. No repeated size or confirmation question. No ml→g conversion.
- Athlete: “200 g Magerquark.” Search BLS; inspect preparation and generic
  identity, then `add '{"items":[{"bls_code":"<selected_code>","amount":"200 g"}]}'`.
  A returned BLS code is not a GTIN. Never choose generic quark for a clearly
  branded flavoured product. BLS `review`/`blocked` requires a verified label,
  not an override or a guessed replacement value. Roh/gegart ambiguity (rice,
  oats etc.) is a targeted follow-up before selecting.
- Athlete: “eine halbe Pizza.” Ask which pizza and package/size or weight;
  apply half of a *known* package serving, not invented standard grams.

Provider errors are not “no calories”. Report unavailable vs no match and
ask for barcode/label, or keep the proposal for retry. No silent estimated
fallback for a text product lookup. Generic and branded snapshots must not
be combined to fill missing fibre/sugars/salt. Exact current label or explicit
user nutrition beats the database. Trace/missing BLS values stay unknown.

The local ledger is canonical. Never add its meal totals to the same Yazio
nutrition arriving through Apple Health. Remote sync is separately opted in;
a local receipt is not evidence of a remote write. Report the sync outbox's
actual status, never imply that accounts or ml capabilities were verified.

## Several photos, one meal

The plate and its label, or the label and the barcode of one product, are
**one** item or meal: list every message in `source_events`. A source event
already in the ledger makes `add` return `duplicate` with the existing
entry — redelivery and double-sends are safe. When another photo of an
already logged meal arrives, attach it with `correct` (its new source event
in the patch, plus any change it brings) instead of adding a second meal.
When you can't tell whether a photo is the same meal or another one, ask.

The same product or even the same photo hash on another occasion **is**
another meal: dedupe is by message, never by barcode or image.

## Corrections, deletion, days

```bash
python3 <skill>/scripts/nutrition.py day                    # today, partial unless marked
python3 <skill>/scripts/nutrition.py list --from 2026-10-20 --to 2026-10-25
python3 <skill>/scripts/nutrition.py correct m_0123456789ab '{"items": {"0": {"amount": "300 g"}}}' --reason "300 g, not 250"
python3 <skill>/scripts/nutrition.py delete m_0123456789ab --reason "logged twice"
python3 <skill>/scripts/nutrition.py mark 2026-10-25 complete
python3 <skill>/scripts/nutrition.py days --from 2026-10-19 --to 2026-10-25
```

A correction is a new revision (`get <id> --history` shows them all). A
deletion is a tombstone; `delete --erase` also blanks the content of every
revision. Mark a day complete only when the athlete says it is ("that was
everything today"); the command refuses while proposals for that day are
open. `days` averages complete days only.

## Images and privacy

You only ever delete the feature's own staged copy, by token —
`image discard` refuses paths. The original stays where the channel put it
(the plugin's inbox) and in the Telegram chat, and you have already read
it in this conversation; Telegram's and the model provider's retention are
outside this feature's control. Never delete files in the channel's inbox,
never delete or edit Telegram messages, and never tell the athlete a photo
is "erased everywhere". If they ask, say what was deleted (the coach's copy)
and what wasn't. Stale copies of never-logged photos go with
`nutrition.py image purge` (older than 24 h by default).

## When it fails

Exit 2: the data is wrong (a misread: sugars above carbohydrate, more than
100 g of nutrients per 100 g, a kJ figure typed as kcal) — re-read the
photo; nothing was saved. Exit 3: missing information — ask. Exit 4: the
food log is off. Exit 5: unknown id or a conflict (stale revision, events
belonging to another meal). Exit 6: the product lookup failed — say so.
Warnings (a 4/4/9 energy mismatch from alcohol or polyols, kJ/kcal
disagreement) are logged with the entry; mention one only if it changes
what they should check.

`COACH_DRY_RUN=1` makes every write a no-op that says so; staging then
returns the original path to read.
