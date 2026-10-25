# Generic German foods: the BLS provider

`skill/scripts/nutrition_bls.py` looks up generic foods (Haferflocken, Quark, Reis, …)
in the **Bundeslebensmittelschlüssel 4.0** of the Max Rubner-Institut. It is a
standalone, standard-library-only provider with its own command line:

```sh
python3 skill/scripts/nutrition_bls.py download --yes     # optional, ~14 MB, blsdb.de only
python3 skill/scripts/nutrition_bls.py import BLS_4_0_2025_DE.zip
python3 skill/scripts/nutrition_bls.py search Haferflocken
python3 skill/scripts/nutrition_bls.py get C133000
```

It is meant for the case where there is no barcode and no label: *"a bowl of oats with
quark"*. A current label or an exact barcode match (Open Food Facts) is still the better
source for a packaged product; BLS fills the generic-food gap and never overwrites either.

## Opt-in, private, separate

* **Nothing downloads or imports by itself.** `download` refuses without `--yes`, accepts
  only `https://blsdb.de/…`, refuses redirects **before issuing the target request**,
  caps the size (64 MiB), checks the result is a zip, and saves it
  to `athlete/state/nutrition-providers/downloads/`. It does not import. `import` is a
  second explicit command. (The official zip can also be fetched by hand from
  <https://blsdb.de/download>; `import` takes the zip or the main `.xlsx` directly.)
* **The database is its own file**, `athlete/state/nutrition-providers/bls.sqlite3`
  (mode `0600`, directory `0700`, `--db` overrides). It has two tables (`meta`, `foods`):
  no meal ledger, no Open Food Facts rows. `athlete/` is git-ignored; no BLS data is
  committed or bundled with the skill.
* **No fill-in between providers.** A missing BLS value stays missing; it is never taken from
  another product or provider.

## What the official file looks like (inspected, not assumed)

`BLS_4_0_2025_DE.zip` (14 263 306 bytes) holds `BLS_4_0_Daten_2025_DE.xlsx`, a components
reference and a PDF. The data workbook has one sheet, `BLS_4_0_Daten_2025_DE`, **7 140
foods × 418 columns** in a ~100 MB sheet XML (about 3 million cells), one header row:

```text
BLS Code | Lebensmittelbezeichnung | Food name |
ENERCJ Energie (Kilojoule) [kJ/100g] | ENERCJ Datenherkunft | ENERCJ Referenz |
ENERCC Energie (Kilokalorien) [kcal/100g] | … | NA Natrium [mg/100g] | … | Hinweis
```

Every nutrient is a triple (value, *Datenherkunft* = origin, *Referenz*); the last column
is a free-text *Hinweis*. Numbers are real numeric cells; markers are text: `-` (no value),
`TR` (trace), `<LOD`, `<LOQ`, `<LOD or <LOQ`. The importer finds columns **by header code
and unit**, not by position or count, and refuses the file if a needed code is missing or
its unit differs (for example sodium not in mg).

## The snapshot

`get` returns one record per 100 g edible portion:

| key | BLS code | unit |
|---|---|---|
| `energy_kcal` | `ENERCC` | kcal |
| `protein_g` | `PROT625` | g |
| `carbs_g` | `CHO` (available carbohydrate) | g |
| `fat_g` | `FAT` | g |
| `fiber_g` | `FIBT` | g |
| `sugars_g` | `SUGAR` | g |
| `salt_g` | `NACL` | g |
| `sodium_mg` | `NA` | **mg** |

(`carbs_g` is the name used here; `nutrition_core.py` calls the same quantity
`carbohydrate_g`. Mapping it is the integrating code's job.) Each nutrient entry has
`value`, `unit`, `qualifier`, the original cell text in `raw`, `source_code`, and
`provenance: {origin, reference}`. The record also carries `provider: "bls"`, `source_id`,
German and English names, `basis` (100 g), `source_version`, `license_id`, `attribution`,
the source file's SHA-256, `source_note` (*Hinweis*), `warnings`, and `automatic_use`.

**Qualifiers: a missing or qualified value is never 0.**

| `qualifier` | meaning | `value` |
|---|---|---|
| `exact` | a number from the file (a real `0` stays `0`) | number |
| `missing` | `-` or empty cell | `null` |
| `trace` | `TR` | `null` |
| `lt_LOD`, `lt_LOQ`, `lt_LOD_or_LOQ` | below detection / quantification limit | `null` |
| `lt_limit` | `< 0,5` style, limit in `limit` | `null` |
| `corrected` | energy recomputed per the erratum, source in `raw_value` | number |
| `derived` | salt derived from sodium, see below | number |

A non-finite, negative or unrecognised value anywhere makes the whole import fail.
Formulas in cells are never evaluated; only a cached value is read.

The kJ energy is deliberately not imported: the erratum affects it too and this provider
does not recompute it.

## BLS 4.0 erratum: energy

The erratum <https://blsdb.de/assets/uploads/Erratum_BLS_4_0_DE.pdf> says 411 foods count
the available oligosaccharides (`OLSAC`) a second time in the energy, and that this stays
until BLS 4.1. The workbook's own component formula still contains that term, so formulas
are never executed. Instead, for a BLS 4.0 file:

* A food is *affected* when `OLSAC` is a number > 0. On the official file that is exactly
  **411** rows.
* If `PROT625, FAT, CHO, POLYL, FIBT, ALC, OA` are **all known numbers**, the energy is
  recomputed with the erratum formula (no OLSAC term), rounded half-up to whole kcal:
  `4*PROT625 + 9*FAT + 4*(CHO-POLYL) + 2*FIBT + 7*ALC + 3*OA + 2.4*POLYL`.
  The entry keeps `raw_value` (the file's number), `qualifier: "corrected"`, and an
  `errata` block with the formula, the inputs, the unrounded result and the source URL.
  Real file: **387** rows corrected (largest change 500 → 453 kcal for toffee).
* If any input is `-`, absent, `TR` or `<LOD/LOQ`, nothing is substituted: the file's value
  is kept (`qualifier: "exact"`, `errata.corrected: false`, `errata.unknown_inputs`), a
  warning is added and `automatic_use.status` is **`review`**. Real file: **24** rows.
* Correction requires **CHO >= POLYL** and a finite, nonnegative result representable
  by the snapshot number format. Inconsistent inputs or Decimal arithmetic failures keep
  the source energy, set `errata.corrected: false` / `invalid_inputs: true`, and require
  **`review`**; they never auto-allow a negative correction or uncorrected source energy.
  Calculations use the original decimal cell text, not a float round-trip;
  `errata.input_sources` retains input raw text, qualifiers and origin/reference provenance.
* **T104100 (Sardelle roh)**: the erratum calls fat and energy too high but gives no exact
  replacement, so none is invented. The values are kept, marked `unreliable`, and
  `automatic_use.status` is **`blocked`**.
* Files that are not 4.0 (a future 4.1) get no correction and no flag.

`automatic_use.status` is `ok`, `review` or `blocked`, with `reasons`. A caller that logs a
meal without asking should accept only `ok`; `review` should be shown with the warning;
`blocked` needs the user's explicit decision or another source. The status is also in every
search result.

## Sodium to salt

The official file has `NACL` for every food, so this never fires on BLS 4.0. For another
workbook: only when the salt cell is *missing* (not when it is `<LOQ`, trace, or a number)
and sodium is a known number, `salt_g = sodium_mg / 1000 * 2.5`, marked `qualifier:
"derived"`, `derived: true`, `derived_from: ["sodium_mg"]` with the method in `provenance`.
Sodium stays its own entry; the salt is not a second independent measurement.

## Searching

`search` returns a list to choose from. There is no "best match" field and nothing is
picked for you (`selection_required: true`); pass the chosen code to `get`.

* Every query word must match. Case, diacritics and ß are folded both ways: `Müsli`,
  `musli` and `muesli` find the same food; `Süßkartoffel`, `susskartoffel`,
  `suesskartoffel` likewise. A hyphen between letters joins them (`Quark-Plunder` is the
  compound `Quarkplunder`).
* **No fuzzy matching**: `Haferflockn` finds nothing. `%`, `_` and quotes are inert.
* Compounds are understood: BLS spells the plain food `Hafer Flocken`, so `Haferflocken`
  matches it **exactly** and ranks it first.
* Order: `exact` → `word` (whole word) → `suffix` (the head noun of a compound: *Speise*quark,
  *Mager*quark) → `prefix` (*Quark*creme) → `substring` (*P*reis*elbeeren* for `reis`),
  then shorter names first. The German name decides; an English name only adds matches
  that come after every German one (`matched_in` says which).
* Results show `title = "<name> [raw|cooked|unspecified] · BLS <code>"`. The state comes
  from whole words in the name (`roh`, `gekocht`, `gedünstet`, `gebraten`, …; English
  too). `unspecified` means the name says neither, as for `Hafer Flocken`. Raw and cooked
  entries are different foods with different values; `--prep raw|cooked` filters.
* Default 25 results (`--limit`), with `total` and `truncated`.

The ranking is a heuristic for finding the plain food on the first page, not a claim
about which food was eaten: `quark` lists *"Roggenbrot mit Quark"* (whole word) before
*"Speisequark Magerstufe"* (head noun), and the latter sits around position 20. Search the
specific word (`speisequark`, `magerquark`) to narrow.

## Reading untrusted workbooks

The importer streams the sheet with `ElementTree` and keeps one row in memory at a time
(real file: ~32 MB peak, ~21 s, 21 MB database). It reads only four fixed parts —
`xl/workbook.xml`, its relationships, `xl/sharedStrings.xml` and the one worksheet whose
name is `BLS_<major>_<minor>_Daten…` — and never extracts an archive.

| guard | default |
|---|---|
| DOCTYPE / ENTITY in any XML part | refused (also across read chunks); UTF-8 only |
| uncompressed size of one part / of all parts read | 512 MiB / 768 MiB (real sheet: 95 MiB) |
| compression ratio of parts over 1 MiB | 200 (real sheet: ~7) |
| archive members | 200 |
| rows / columns / text per cell | 200 000 / 2 000 / 65 536 |
| member names with `..`, absolute paths, drive letters | refused |
| zip with not exactly one `BLS_x_y_Daten_*.xlsx` | refused |
| sheet part not matching `worksheets/sheetN.xml` | refused |

Corrupt, truncated or non-zip files are `ImportRejected`, not tracebacks.

## Replacing the database

Both downloads and imports use random `mkstemp` staging names, exclusively created
(`O_EXCL`) as **0600 from the start**, with an owned regular-file check. They do not
open, truncate or unlink predictable staging names left by another process. Output
parents must be user-owned and private (no group/other permission bits); all path
components are checked for symlinks, ownership and unsafe write permissions before
staging. Missing directories are created as 0700. Existing directories (including a
broad home directory supplied as the direct output parent) are **never chmodded**;
choose a dedicated private subdirectory instead. Root-owned sticky ancestors such as
`/tmp` are allowed, but not as the direct output parent. This permission boundary
protects against other users, not a malicious process already running as the same uid.

`import` builds a new SQLite file next to the target and swaps it in with `os.replace` only
after every row is stored. Any failure (bad cell, duplicate code, no header, size limit,
crash at the swap) leaves the existing database byte-identical and removes the staging
file. A successful import **replaces** the contents; nothing is merged. `meta` records the
BLS version, SHA-256 of the file given, member name, import time and the attribution. The
database also stores a `schema_version`; one built by an older importer is refused
(`StaleDatabase`) and must be imported again, because the search index is built at import.
Schema version 3 also refuses pre-hardening snapshots: reimport is needed to apply the
validated correction policy. Atomic replacement is not a power-loss durability guarantee:
there is no additional file/directory fsync protocol around the rename, and abrupt process
termination can leave an unused private staging file (normal exceptions clean it up).

## Python API for the integration

```python
import nutrition_bls as bls
bls.import_bls(path, db_path=None, limits=None)  # -> {foods, bls_version, source_sha256, ...}
bls.search("Haferflocken", db_path=None, limit=25, preparation=None)
bls.get("C133000", db_path=None)                 # exact code -> snapshot dict
bls.info(db_path=None); bls.download(dest_dir)   # download only when the user said so
```

Errors derive from `bls.BlsError`: `ImportRejected`, `NoDatabase`, `StaleDatabase`,
`NotFound`. `db_path=None` means `cc.STATE/nutrition-providers/bls.sqlite3`. Keep the
`attribution` string wherever BLS numbers are shown.

## Licence and attribution

BLS 4.0 is CC BY 4.0. Every `import`, `search`, `get` and `info` output includes:

> Max Rubner-Institut (2025): Bundeslebensmittelschlüssel (BLS), Version 4.0 — Deutsche
> Nährstoffdatenbank. Karlsruhe. DOI: 10.25826/Data20251217-134202-0. Lizenz: CC BY 4.0
> (https://creativecommons.org/licenses/by/4.0/deed.de). Änderungen: Normalisierung/Import
> und dokumentierte Errata-Korrekturen.

The corrected energies are changes to the published values, which is why the attribution
says so.

## Limits

* Only the eight values above are stored (plus the inputs for the erratum). Water, vitamins,
  minerals, amino acids and the kJ energy are in the workbook but not imported.
* Values are per 100 g edible portion. BLS has no portion sizes or densities; turning
  "1 bowl" into grams is the caller's job, and ml must not be treated as g without a density.
* The erratum handling is specific to the 4.0 file. When BLS 4.1 appears, re-read its release
  notes before importing it: the importer stops correcting (version ≠ 4.0), which is right
  only if 4.1 really fixes the energies.

## Tests

```sh
python3 -m unittest discover -s tests -p test_nutrition_bls.py -v
```

All fixtures are synthetic workbooks built inside the test file, so nothing from the real
BLS file or from an athlete is copied in. They cover column mapping by header, the
qualifiers, rejection of NaN/inf/negative/garbage values, the erratum arithmetic (a
hand-computed row, rounding, every input missing in each way, non-4.0 files, T104100),
sodium→salt, German search and ranking, hostile zips and XML, transactional replacement,
the command line, exclusive private staging and unsafe-parent rejection, Decimal edge
cases/provenance, and the download guard. A real loopback HTTP server verifies that a
cross-host redirect target is never requested; other download fixtures use a fake opener.
No external network calls are made.

The real file was imported once into a scratch database outside the repository to confirm
the numbers quoted above (7 140 foods, 411 affected = 387 corrected + 24 review, one
blocked, real oats/quark/rice records).
