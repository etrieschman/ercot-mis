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
| entry point | `em.open()` → `Session`; methods `list`, `fetch`, `ingest`, `probe`, `build_raw`, `build_core`, `raw`, `core`; later `match`, `sql`, `export` |
| transport | own thin clients: EWS (certificate) and Public API (archive files, not JSON rows) |
| transforms | Python parsers → `raw`; `.sql` files on DuckDB → `core`, `out` |
| storage | Parquet on disk (zstd, hive-partitioned), Arrow in memory, DuckDB catalog + SQL engine, polars for reading. **Catalog writes are short-lived**: `Session` reads through a read-only connection and takes the writer only to record a listing, a blob or an artifact, never across a download or a parse |
| node identity | **equipment-based `node_key`**, not PSS/E number or name (DAM renumbers every hour; names are station names). CRR bus ties (`x <= 1e-4`, in service) are contracted first. `core/identity.py`; measured in `docs/datasets/identity-and-matching.md` |
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
  `dam:2026-10-14:he07:r1`). Built so far: `core.snapshot` (one row per model, revision
  = order of `posted_at` within a logical package), `core.node` (one row per RAW bus
  per snapshot: `node_key`, `station`, `kv`, `node_group`, `is_tie_member`, attachments),
  `core.branch` (stable `branch_id`, endpoints as node keys, tie/in-service/monitored/
  secured flags), `core.branch_rating` (RAW rate A/B/C and CRR CSV ratings per TOU,
  side by side), `core.contingency` + `core.contingency_outage` (each model's own
  vocabulary resolved to branch/node keys, `is_resolved`, split-bus rows kept),
  `core.gtc` + `core.gtc_member` (CRR from its CSV with members; DAM hourly limits from
  the GTL workbook with `crr_gtc_id` from the manual crosswalk in
  `data/overrides/gtc_names.csv`, members empty), and `core.match_branch` /
  `core.match_node` per (CRR snapshot, DAM snapshot) via `session.match_branches` and
  `session.match_nodes`, and `core.match_contingency` via `session.match_contingencies`
  (name, then translated branch set; member-set differences recorded).
  Planned:
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
- **CRR ↔ DAM matching** (order settled by measurement, see
  `docs/datasets/identity-and-matching.md`): settlement points and generator/load
  names first (every CRR source/sink name appears among DAM settlement points),
  then branches by the workbook's `Operations_Name` with the documented normalization
  (never exact: some match DAM `Branch Name` after dropping punctuation, most as a
  prefix), then buses through matched branch endpoints (**not** by name or number:
  CRR and DAM numbers are unrelated, DAM names are stations), then contingencies by
  name with member sets compared in the matched vocabulary; GTCs by name then members/factors/limit. `match_*` records
  identity, `diff_*` records differences — never smooth differences over. Every match
  carries `match_method`; unmatched records are output. Manual overrides live in
  `data/overrides/` (gitignored), read by core when present.
- **CRR is closer to node-breaker, DAM to bus-branch**: CRR RAWs hold thousands of
  zero-impedance branches (some monitored), DAM none. Contract before any PTDF;
  `core.node` records the contraction.
- **CRR transformer names are not in the RAW**: the contingency, monitored and GTC
  CSVs name transformers by the `Autos` workbook sheet, matched to the RAW by
  (from, to, ckt). Lines use the RAW comment.
- **GTCs in DAM** are not in NP4-500-SG. Definitions and daily limits are ECEII
  products `NP3-770-M` and `NP3-766-M` (pulled since 2026-09-17); each GTL is a
  base-case constraint in CRR, DAM and RT. The GTL workbook gives hourly DAM and RT
  limits under human-readable names; the CRR CSV uses codes; the crosswalk is manual
  (`data/overrides/gtc_names.csv`, never committed).
- **DAM `Monitored?`/`Monitored and Secured?`** are the CIM `DAM Monitored`/`DAM
  Secured` flags (defaults FALSE/TRUE, hence mostly No/Yes). Working reading:
  Secured = enforced, Monitored-only = reported; verify against `NP4-191-CD`.
- **Ratings**: CRR `BaseCaseRating` is a fixed fraction of RAW rate A on every
  monitored line; TOU blocks can be identical for a month. Store both, apply neither
  as a rule.
- **Annual auctions**: each LTAS auctions six consecutive six-month terms over three
  years, Seq6 (≈3 yr out) → Seq1 (≈6 months out). Monthly model is closest to DAM.
- DAM network models (`NP4-500-SG`) have a 31-day display window, disclosures a
  60-day lag: capture models first, injections fill in later.
- **A DAM package is one day holding 24 hourly models**: ~29 MB zipped, ~240 MB
  unzipped, 195 members. Per hour (`_###`): a PSS/E `.RAW` plus CSVs `Ctg`
  (contingencies), `Ln` (lines), `Xf` (transformers), `Ld` (loads), `Gn`
  (generators), `Sp` (settlement points), `Hb` (hubs); per day: `SpCtg`, `SpNb`,
  and a README. No two hourly files are byte-identical because **every hourly model
  renumbers its buses**; the equipment maps are identical across hours. Raw stays
  one table per file; semantic dedup belongs in core. The DAM snapshot key needs an hour: `dam:2026-10-14:he07:r1`.

### Dataset notes — `docs/datasets/`

Per-dataset design choices live in `docs/datasets/` (one note per dataset, fixed
template: what it is, capture, package layout, what we parse, decisions, ERCOT
quirks, validation, open questions). Read the relevant note before touching a
parser; **update it in the same commit** whenever an import decision changes or a
new quirk turns up. Notes are public: structure only, never names or values.
Written so far: `psse-raw.md`, `crr-network-model.md`, `dam-network-model.md`.

### Provenance

Catalog (`catalog.duckdb`): `remote_doc` (every listed document, refreshed per
listing), `archive_blob` (distinct bytes), `archive_member` (zip members by hash),
`archive_source` (one row per arrival), `run`, `artifact` (one row per written file:
layer, table, blob, parser id, path, rows, bytes) and `lineage` (artifact ← members).
Artifact key = sha256(package version + parser/layer `VERSION`s | blob sha256 | table);
existing key and file ⇒ skip, bumped version ⇒ rebuild. `build_raw` writes
`raw/<table>/emil_id=<EMIL>/<blob16>.parquet` (one file per package and table) with
identity columns on every row (`emil_id`, `doc_id`, `blob_sha256`, `member_sha256`,
`member_path`, CRR `auction/term/sequence/month/time_of_use`, DAM
`operating_date/hour`). `build_core` writes `core/snapshot.parquet` and
`core/node/emil_id=<EMIL>/<blob16>.parquet`. Read with `session.raw(table)` and
`session.core(table)` (polars lazy scans). Artifacts will carry the most
restrictive classification of their inputs; `export()` refuses Secure/ECEII outside
`data/`. Drive builds from a script or notebook, not from `python -` (the process
pool re-imports `__main__`).

## Layout

The package mirrors the data folder's layers. Each layer package owns its `build`;
`Session` (returned by `em.open()`) strings them together and is the only writer.

```
src/ercot_mis/
  __init__.py       open() -> Session, re-exports
  session.py        Session: list, fetch, ingest, probe (fill the archive);
                    build_raw, build_core (write layers); raw(table), core(table) (read)
  config.py         data folder resolution + synced-folder warning; Identity; secrets (env, .env, Keychain)
  products.py       PRODUCTS: EMIL specs (report type, class, window, source, pull/track)
  sources/          how bytes arrive
    ews.py          EWS: build_request, sign (SHA-1 WS-Security), parse_reports, EwsClient
    retry.py        retrying(): fixed back-off for stalled downloads and listings
  archive/          layer 0: original documents, never edited
    store.py        content-addressed files (archive/<EMIL>/<sha256>.zip, read-only, atomic), zip member hashing
    catalog.py      DuckDB catalog (read-only by default, short writes): remote_doc, archive_blob,
                    archive_member, archive_source, run, artifact, lineage
  raw/              layer 1: typed tables, one per source file
    table.py        Column specs, snake_case, read_delimited (Arrow CSV, exact header check), read_sheet; ParseError
    psse.py         PSS/E v30 RAW -> psse_* tables, both dialects
    crr.py, dam.py  package member classification + parsers -> crr_* / dam_* tables
    gtl.py          NP3-766-M GTL workbook (whole document, not a zip) -> gtl_hourly
    build.py        parse packages in a process pool, write Parquet with identity columns, register artifacts
  core/             layer 2: tidy keyed tables shared by CRR and DAM
    snapshot.py     snapshot IDs and revisions from the catalog and member names
    node.py         equipment-based node keys; CRR tie contraction (dam_nodes, crr_nodes)
    branch.py       core.branch and core.branch_rating (crr_branches, dam_branches)
    contingency.py  core.contingency and core.contingency_outage, resolved to branch/node keys
    gtc.py          core.gtc and core.gtc_member (CRR; DAM empty until NP3-766-M/NP3-770-M parse)
    match.py        core.match_branch (exact -> ops+ckt -> prefix), core.match_node (settlement
                    point -> matched branch endpoints by vote), core.match_contingency (name -> members)
    build.py        writes every core table per package
scripts/daily_pull.py        fetch pulled EWS products, list tracked ones; launchd template in scripts/launchd/
scripts/probe.py             archive-depth probe over every EWS product
scripts/validate_parsers.py  parse archived packages; check counts (RAW sections, DAM RAW vs CSVs)
scripts/measure_identity.py  key-matching and node-identity measurements -> data/reports/identity/*.json
tools/check_confidential.py  pre-commit guard (stdlib only; also blocks Keychain-stored secrets)
.github/workflows/ci.yml     tests + guard on every push
tests/                       synthetic-only tests; test_guard also scans every tracked file
```

Cache keys use explicit `VERSION` constants (each raw parser, `core/node.py`,
`core/build.py`): bump one when its output changes and every artifact it produced is
rebuilt; cosmetic edits cost nothing.

**Numbers do not go in markdown.** Notes record process, decisions and quirks;
measurements live in the scripts that make them and the dated reports under
`data/reports/`.

## Milestones

| # | milestone | status |
|---|---|---|
| M0 | scaffold, config, EWS client, archive probe | done — EWS depth = display window |
| M1 | archive store, catalog, fetch (+ tracked products), ingest `ftr_align/ercot_data`, Public API archive client; **daily scheduled pull** (required by the M0 finding); DAM capture starts | EWS half done and verified live; first pull 2026-09-15 archived every listed EWS document (1.8 GB) incl. the two `ftr_align/ercot_data` zips via ingest; daily scheduling not yet installed; Public API client waits for credentials |
| M2 | PSS/E v30 parser + CRR raw layer (CSV vs XML check picks canonical) | parsers done (PSS/E both dialects, CRR, DAM); `scripts/validate_parsers.py` clean on every archived package. Not yet: writing raw Parquet (lands with the M3 runner), DynamicRatings, PowerFlowData.jl cross-check (needs Julia) |
| M3 | core layer + SQL runner, cache skip, lineage, validation checks | 2026-09-17: raw writer with provenance (`build_raw`, process pool, cache skip, `run`/`artifact`/`lineage`); `core.snapshot` and `core.node` with equipment-based keys (`build_core`); built on every archived package. Not yet: SQL runner, branch/rating/contingency/GTC core tables |
| M4 | `out.network` + `ftr_align/cases/ercot.py` | |
| M5 | DAM prices, awards, settlement point weights, `out.hourly_injection` | |
| M6 | CRR ↔ DAM matching + scorecard | |
| M7 | `sensitivities` (restricted float32 PTDF/LODF, cache budget, cross-test vs `ftr_align.network.compute_ptdf`) | |
| M8 | scheduled pulls (Python files), docs, first release | |

## Pick up here (next session)

State at 2026-09-17 (end of day): M0–M2 done; M3 mostly done (raw layer with
provenance, snapshots, nodes, branches, ratings, contingencies, GTCs); M6's matchers
exist for branches, nodes and contingencies. Not started: SQL runner, `out.network`,
Public API client. The
adversarial review of 2026-09-17 found that three locked assumptions were wrong (bus
identity by name, number-keyed DAM snapshots, GTCs from every model) and they were
replaced by measured facts: read `docs/datasets/identity-and-matching.md` first.
Security fixes landed the same day (owner-only data folder on every `open()`, listings
without file names, retries plus a macOS notification on a failed pull, Keychain
lookup for Public API secrets, CI, Time Machine exclusion of `data/`).

First, check health (2 min):
- `tail -30 data/logs/daily_pull.log` and `cat data/logs/last_run.json` — `failed 0`.
- `launchctl list | grep ercot-mis` — last exit code 0. A failure also posts a
  macOS notification.
- `uv run python scripts/measure_identity.py` after a new CRR month posts; compare the
  report with the previous one.

Then, in order:
1. **Matching quality**: disambiguate branches with several DAM circuit candidates
   (endpoint stations, kV); explain matched nodes whose kV disagree; classify
   name-matched contingencies with different branch sets (`scripts/measure_identity.py`
   now reports all three matchers).
2. **DAM GTC members**: NP3-770-M ships PDF/PPTX/database files; either parse the
   database or keep borrowing CRR member sets through `crr_gtc_id` and say so in
   `out.network`. Add NP3-766-M to `scripts/validate_parsers.py`.
3. **`out.network`: a per-snapshot network in one vocabulary (nodes, branches with
   reactance and ratings, contingencies as outage sets, GTC rows) ready for
   `ftr_align/cases/ercot.py`, with the contracted CRR topology.
4. **SQL runner** for `models/core/*.sql` once the Python-built tables settle; wire
   `build_core` into the daily pull after the fetch.
5. **Public API client** (`sources/public_api.py`): B2C ROPC token, subscription key,
   paged archive listing, retry on 429; secrets via `load_secret()` (Keychain). Then
   pull NP4-191-CD shadow prices and test the monitored/secured reading.
6. `ftr_align/cases/ercot.py` needs a sparse PTDF (scipy `splu`) and a screened row
   set: 11k elements × 10k nodes × 6.5k contingencies is not a dense `K`.

Open decisions for the user: keep capturing every DAM day (see `scripts/probe.py` and
the catalog for sizes)? Install Julia for the PowerFlowData.jl cross-check? Move the Public API
secrets from `.env` into the Keychain (commands in the README)?

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
- Sizes and cadences per product come from `scripts/probe.py` and the catalog, not
  from this file.

## Environment

- `uv sync`; public PyPI is forced via `[[tool.uv.index]]` (this machine's default
  index is private).
- `uv run pytest -q`
- `git config core.hooksPath .githooks` once per clone.
- Credentials: `~/.ercot/api.crt`, `~/.ercot/api.key`; identity in `.env`; Public API
  secrets in `.env` or the macOS Keychain (`security add-generic-password -s ercot-mis -a <VAR> -w`).
- `data/` is excluded from Time Machine (`tmutil addexclusion data`, sticky xattr).
- The launchd job runs at 07:00 local (Eastern on this machine).
