# ercot-mis — working notes for Claude

Fetch, cache and standardize ERCOT MIS data locally, with every table traceable
to the bytes ERCOT published. **Public repo, code only.** First consumer:
`~/dev/ftr_align` (its ERCOT rung compares CRR and DAM network models).

## Confidentiality rules — read first

- ERCOT Secure/ECEII data, and anything derived from it, lives only in `data/`
  (gitignored) or `$ERCOT_MIS_DATA`. Never copy it into code, tests, docs, commit
  messages, issues or PR text.
- When inspecting local data, print **structure only**: column names, record
  counts, sizes, date ranges, report group names. Never print bus, station,
  device, contingency or constraint names, and never print document file names —
  ERCOT file names embed the participant DUNS.
- DUNS and API user come from the environment or `.env`. Never echo them.
  `Identity` hides both from its repr.
- Tests use synthetic data only (`tests/fixtures/synthetic/` when files are needed).
- `tools/check_confidential.py` is the pre-commit hook and also runs in the test
  suite. Never bypass it with `--no-verify`.
- ECEII in any cloud service needs the user to check ERCOT ECEII obligations first.

## Locked design (settled over a long planning pass — do not relitigate)

| topic | decision |
|---|---|
| scope | ERCOT only, in ERCOT vocabulary (settlement point, electrical bus, TOU block, EMIL ID, report type) |
| job | pull and standardize; the only writer of data; consumers read |
| interface | **Python API only**, used from Python files and notebooks; no CLI for now |
| entry point | `em.open()` → `Mis`; methods `probe`, later `list`, `fetch`, `ingest`, `build`, `lineage`, `match`, `table`, `sql`, `export` |
| transport | own thin clients: EWS (certificate) and Public API (archive files, not JSON rows) |
| transforms | Python parsers → `raw`; `.sql` files on DuckDB → `core`, `out` |
| storage | Parquet on disk (zstd, hive-partitioned), Arrow in memory, DuckDB catalog + SQL engine, polars for reading |
| data location | `data/` in this repo by default; `ERCOT_MIS_DATA` overrides |
| network output | standardized tables + optional `ercot_mis.sensitivities` (base-case PTDF, LODF, GTC rows); no shift factors yet |
| ratings | keep both: CRR monitored-element CSV (enforced, default) and PSS/E Rate A/B/C (MVA) |
| conventions | facts stored as data; judgment calls as named options defaulting to ERCOT practice |
| PSS/E parser | our own focused v30 parser (`parsers/psse.py`): one compiled tokenizer for both ERCOT dialects (~0.3 s per RAW), Arrow for type conversion. Arrow's CSV reader can't split the blank-separated DAM RAW, so it is used for the CSVs only. PowerFlowData.jl as an optional reference check; **not** VeraGridEngine |
| CRR scope | annual (all sequences + updates) and monthly |
| DAM scope | LMPs, SPPs, 60-day disclosure awards → node-space injections; network models for **every day** (all 24 hours, ~10 GB/yr zipped); shadow prices for validation |
| shift factors | `SYS-608-CD` tracked (listed into catalog) but not pulled |
| schedule | daily at 07:00 via launchd (`scripts/daily_pull.py`, installed as `~/Library/LaunchAgents/ercot-mis.daily-pull.plist`); required because EWS keeps nothing past the display window |

### Layers and naming

- `archive/` — original ERCOT zips, content-addressed by sha256, never edited or
  unzipped to disk; `_Upd` is a new document (revision), not an overwrite.
- `raw` — typed Parquet, one table per source file, named for the file
  (`raw.crr_monitored_lines_and_transformers`). snake_case + types only.
- `core` — tidy keyed tables. Network tables are **shared by CRR and DAM** and keyed
  by `snapshot_id` (`crr:annual:2029.1st6:seq6:2029-01:r2`, `crr:monthly:2026-10:r1`,
  `dam:2026-10-14:he07:r1`): `core.bus`, `core.branch`, `core.branch_rating`,
  `core.contingency`, `core.contingency_outage`, `core.gtc`, `core.gtc_member`,
  `core.constraint`, `core.price_node_bus`, `core.settlement_point`,
  `core.hourly_lmp`, `core.hourly_spp`, `core.hourly_award`,
  `core.hourly_shadow_price`, `core.match_*`, `core.diff_*`, `core.tou_hours`.
- `out` — what consumers read: `out.network` (conventions applied),
  `out.hourly_injection` (q).
- Layer = folder = DuckDB schema. No PUDL-style `layer_source__type` names.
  Columns: unit suffixes (`_mw`, `_mva`), `is_` booleans, `_code` categoricals.
- Hourly facts carry `interval_start_utc`, `interval_end_utc` **and** ERCOT's
  `delivery_date`, `hour_ending`, `dst_flag`.

### Domain notes that shape the model

- **GTCs** come from each model's Non-Thermal Constraints file (name, limit,
  member devices with factor and flow direction); each GTL is enforced as a
  **base-case** constraint in CRR, DAM and RT. A GTC's PTDF row is the
  factor-weighted sum of its members' rows.
- **q (node-space net injection)** is not published. Build it from the 60-Day DAM
  Disclosure (`NP3-966-ER`): generation and ESR awards at resource nodes;
  energy-only offer awards (+), energy bid awards (−), PTP obligation/option awards
  (+ source, − sink) at settlement points. Settlement points → buses: resource node
  1:1, hubs via `NP3-220-SG`, load zones via `NP4-159-CD`. Checks: Σq ≈ 0 hourly;
  MS at settlement points (SPP) = MS at buses (LMP·q); both = Σ μ·limit from
  shadow prices. MS itself is analysis and lives in `ftr_align`.
- **CRR ↔ DAM matching**: the CRR package's monthly mapping workbook (`Lines`:
  `CRR_Tag` ↔ `Operations_Name`; `Autos`) is the authoritative branch key. Buses by
  electrical bus name (never PSS/E number alone); contingencies by name then
  identical device set; GTCs by name then members/factors/limit. `match_*` records
  identity, `diff_*` records differences — never smooth differences over. Every
  match carries `match_method`; unmatched records are output. Manual overrides live
  in `data/`.
- **Annual auctions**: each LTAS auctions six consecutive six-month terms over three
  years, Seq6 (≈3 yr out) → Seq1 (≈6 months out). Monthly model is closest to DAM.
- DAM network models (`NP4-500-SG`) have a 31-day display window, disclosures a
  60-day lag: capture models first, injections fill in later.
- **A DAM package is one day holding 24 hourly models**: ~29 MB zipped, ~240 MB
  unzipped, 195 members. Per hour (`_###`): a PSS/E `.RAW` plus CSVs `Ctg`
  (contingencies), `Ln` (lines), `Xf` (transformers), `Ld` (loads), `Gn`
  (generators), `Sp` (settlement points), `Hb` (hubs); per day: `SpCtg`, `SpNb`,
  and a README. No two hourly files are byte-identical, within a day or across
  days, so savings must come from columnar Parquet on parsed rows, not blob dedup.
  The DAM snapshot key therefore needs an hour: `dam:2026-10-14:he07:r1`.

### Dataset notes — `docs/datasets/`

Per-dataset design choices live in `docs/datasets/` (one note per dataset, fixed
template: what it is, capture, package layout, what we parse, decisions, ERCOT
quirks, validation, open questions). Read the relevant note before touching a
parser; **update it in the same commit** whenever an import decision changes or a
new quirk turns up. Notes are public: structure only, never names or values.
Written so far: `psse-raw.md`, `crr-network-model.md`, `dam-network-model.md`.

### Provenance

Catalog (`catalog.duckdb`): built so far `remote_doc` (every listed document,
refreshed per listing), `archive_blob` (distinct bytes), `archive_member` (zip
members by hash), `archive_source` (one row per arrival: fetch or ingest, with
doc_id when linked). Still to come with the SQL runner: `run`, `artifact`, `lineage`.
DuckDB allows one writer: don't run the daily pull and a writing notebook at once. Transform identity = sha256 of the SQL file / parser module +
package version. Cache key = hash(transform identity, input fingerprints, params);
existing key ⇒ skip. Artifacts carry the most restrictive classification of their
inputs; `export()` refuses Secure/ECEII outside `data/`.

## Layout

```
src/ercot_mis/
  __init__.py     open(), re-exports
  config.py       data folder resolution + synced-folder warning; Identity from env/.env
  products.py     PRODUCTS: EMIL specs (report type, class, window, source, pull/track)
  mis.py          Mis: the session object; list, fetch, ingest, probe
  sources/ews.py  EWS: build_request, sign (SHA-1 WS-Security), parse_reports,
                  EwsClient (list_documents + download = the Source interface)
  store/archive.py  content-addressed store (archive/<EMIL>/<sha256>.zip, read-only,
                  atomic via archive/.partial), zip member hashing
  store/catalog.py  DuckDB catalog: remote_doc, archive_blob, archive_member, archive_source
  parsers/_common.py  Column specs, snake_case, cast_column, read_delimited (Arrow CSV,
                  exact header check), read_sheet (xlsx via fastexcel); ParseError
  parsers/psse.py   PSS/E v30 RAW -> psse_* tables, both dialects
  parsers/crr.py    CRR package member classification + parsers -> crr_* tables
  parsers/dam.py    DAM package member classification + parsers -> dam_* tables
scripts/validate_parsers.py  parse archived packages; check counts (RAW sections, DAM RAW vs CSVs)
scripts/probe.py archive-depth probe over every EWS product
scripts/daily_pull.py  fetch pulled EWS products, list tracked ones; launchd template in scripts/launchd/
tools/check_confidential.py   pre-commit guard (stdlib only)
.githooks/pre-commit
tests/            synthetic-only tests; test_guard also scans every tracked file
```

## Milestones

| # | milestone | status |
|---|---|---|
| M0 | scaffold, config, EWS client, archive probe | done — EWS depth = display window |
| M1 | archive store, catalog, fetch (+ tracked products), ingest `ftr_align/ercot_data`, Public API archive client; **daily scheduled pull** (required by the M0 finding); DAM capture starts | EWS half done and verified live; first pull 2026-09-15 archived every listed EWS document (1.8 GB) incl. the two `ftr_align/ercot_data` zips via ingest; daily scheduling not yet installed; Public API client waits for credentials |
| M2 | PSS/E v30 parser + CRR raw layer (CSV vs XML check picks canonical) | parsers done (PSS/E both dialects, CRR, DAM); `scripts/validate_parsers.py` clean on all 29 CRR packages and 768 DAM hourly models (2026-09-15). Not yet: writing raw Parquet (lands with the M3 runner), DynamicRatings, PowerFlowData.jl cross-check (needs Julia) |
| M3 | core layer + SQL runner, cache skip, lineage, validation checks | |
| M4 | `out.network` + `ftr_align/cases/ercot.py` | |
| M5 | DAM prices, awards, settlement point weights, `out.hourly_injection` | |
| M6 | CRR ↔ DAM matching + scorecard | |
| M7 | `sensitivities` (restricted float32 PTDF/LODF, cache budget, cross-test vs `ftr_align.network.compute_ptdf`) | |
| M8 | scheduled pulls (Python files), docs, first release | |

## Pick up here (next session)

State at 2026-09-15: M0–M2 done and pushed. Archive holds every EWS document offered
(1.8 GB); the daily pull runs at 07:00 via launchd; parsers validated clean on all
archived packages. Public API credentials are in `.env` (unused so far).

First, check health (2 min):
- `tail -30 data/logs/daily_pull.log` — a run each morning, `failed 0`.
- `launchctl list | grep ercot-mis` — last exit code 0.

Then, in order:
1. **M3a: raw Parquet writer with provenance.** `mis.build_raw(product)`: for each
   archived blob, each member with `is_parsed`, write the parser's tables to
   `data/raw/<table>/…/part-<member_sha256[:16]>.parquet` (zstd). Add catalog tables
   `run`, `artifact`, `lineage`. Cache key = sha256(parser module source + package
   version + member sha256); skip when the artifact exists. Add identifying columns
   to every raw row: `emil_id`, `doc_id`, `member_sha256`; CRR `auction`, `term`,
   `sequence`, `month`, `time_of_use`; DAM `operating_date`, `hour`. Partition CRR
   by `emil_id`/`month`, DAM by `operating_date`. Parse DAM days in parallel
   (process pool) — ~12 s/day serial.
2. **Snapshot IDs and revisions**: `core.snapshot` from the catalog + member
   metadata; revision `r<n>` = order of `posted_at` among documents for the same
   logical package (annual `_Upd` packages are revisions).
3. **M3b: SQL runner** (`models/core/*.sql`, header `-- inputs:` / `-- partition_by:`)
   and first core tables: `core.bus`, `core.branch` (lines + transformers),
   `core.branch_rating` (CRR CSV + RAW Rate A/B/C), `core.contingency`,
   `core.contingency_outage`, `core.gtc`, `core.gtc_member`, `core.price_node_bus`
   (normalize MW-scale weights). Normalize device-type spellings (see crr note).
4. **Public API client** (`sources/public_api.py`, same Source interface as EWS):
   token via ERCOT B2C ROPC flow (username/password form POST, `id_token`), header
   `Ocp-Apim-Subscription-Key`; archive listing `GET /archive/{emil_id}` paged by
   `_meta.totalPages`; download via each archive's `_links.endpoint.href`. Verify on
   first live call: timestamp format for `postDatetimeFrom/To`, listing field names,
   whether sizes are given. Retry on 429. Then set `source="public_api"` products
   to pull in `daily_pull.py`, confirm `NP4-183-CD` is the DAM hourly LMP product,
   and write dataset notes for each public product.
5. Write dataset notes for NP4-160-SG, NP3-220-SG, NP5-615-SG, and parse them.

Open decisions for the user: keep capturing every DAM day (~10 GB/yr)? Install Julia
for the PowerFlowData.jl cross-check?

## EWS facts learned the hard way (keep)

- API certificate required; user ID must start `API_` (`SECU1073`).
- mTLS **and** WS-Security signature over the Body (`SECU1096` without it).
- SHA-1 digest and RSA-SHA1 signature only (`SECU3518` / `SECU3517`); digest is
  checked first, so change one at a time.
- SOAPAction is a path, not a URN (`RUNTIME0031`).
- `Header` and `Request` children are `xsd:sequence`: element order is load-bearing.
- Message.xsd uses "www.docs" WSS namespaces for ReplayDetection; the Security
  header uses the standard OASIS ones. Don't unify.
- Parse replies by local name; ERCOT has changed namespaces before.
- An empty listing is `ReplyCode=ERROR` with "No reports found…", not an empty payload.
- **EWS keeps nothing older than a product's display window** (probe, 2026-09-15):
  an unbounded listing returns exactly the window, and an explicit older window
  returns "No reports found". History cannot be backfilled over EWS; anything not
  captured before it rolls off is gone. Scheduled pulls are required.
- Probe sizes (2026-09): CRR long-term ~17 docs/yr, ~43 MB each; CRR monthly
  ~12/yr, ~20 MB; DAM PSS/E model 1/day, ~29 MB; SCED shift factors ~1,200
  docs/month, ~690 MB/month (tracked only).

## Environment

- `uv sync`; public PyPI is forced via `[[tool.uv.index]]` (this machine's default
  index is private).
- `uv run pytest -q`
- `git config core.hooksPath .githooks` once per clone.
- Credentials: `~/.ercot/api.crt`, `~/.ercot/api.key`; identity in `.env`.
