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
| storage | Parquet on disk (zstd, hive-partitioned), Arrow in memory, DuckDB catalog + SQL engine, polars for reading. **Catalog writes are short-lived**: `Mis` reads through a read-only connection and takes the writer only to record a listing, a blob or an artifact, never across a download or a parse |
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
  = order of `posted_at` within a logical package) and `core.node` (one row per RAW bus
  per snapshot: `node_key`, `station`, `kv`, `node_group`, `is_tie_member`, attachments).
  Planned: `core.branch`, `core.branch_rating`,
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
  names first (all 996 CRR source/sink names appear among DAM settlement points),
  then branches by the workbook's `Operations_Name` with the documented normalization
  (never exact: 0 of 8,294 match DAM `Branch Name` verbatim, 2,217 after dropping
  punctuation, 6,037 more as a prefix), then buses through matched branch endpoints
  (**not** by name or number: CRR and DAM numbers are unrelated, DAM names are
  stations), then contingencies by name (6,162 of 7,391) with member sets compared in
  the matched vocabulary; GTCs by name then members/factors/limit. `match_*` records
  identity, `diff_*` records differences — never smooth differences over. Every match
  carries `match_method`; unmatched records are output. Manual overrides live in
  `data/`.
- **CRR is closer to node-breaker, DAM to bus-branch**: CRR RAWs hold ~2,900
  zero-impedance branches (1,232 of them monitored), DAM none. Contract before any
  PTDF; `core.node` records the contraction.
- **CRR transformer names are not in the RAW**: the contingency, monitored and GTC
  CSVs name transformers by the `Autos` workbook sheet, matched to the RAW by
  (from, to, ckt). Lines use the RAW comment.
- **GTCs in DAM** are not in NP4-500-SG. Definitions and daily limits are ECEII
  products `NP3-770-M` and `NP3-766-M` (pulled since 2026-09-17); each GTL is a
  base-case constraint in CRR, DAM and RT with one DAM limit per operating day.
- **DAM `Monitored?`/`Monitored and Secured?`** are the CIM `DAM Monitored`/`DAM
  Secured` flags (defaults FALSE/TRUE; 98% of lines are No/Yes). Working reading:
  Secured = enforced, Monitored-only = reported; verify against `NP4-191-CD`.
- **Ratings**: CRR `BaseCaseRating` = 0.90 × RAW rate A on every monitored line;
  TOU blocks were identical in 2026-09. Store both, apply neither as a rule.
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
  one table per file (~16 MB Parquet per day, ~9 s with the process pool); semantic
  dedup belongs in core. The DAM snapshot key needs an hour: `dam:2026-10-14:he07:r1`.

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
Artifact key = sha256(parser/module source + package version | blob sha256 | table);
existing key and file ⇒ skip, changed parser ⇒ rebuild. `build_raw` writes
`raw/<table>/emil_id=<EMIL>/<blob16>.parquet` (one file per package and table) with
identity columns on every row (`emil_id`, `doc_id`, `blob_sha256`, `member_sha256`,
`member_path`, CRR `auction/term/sequence/month/time_of_use`, DAM
`operating_date/hour`). `build_core` writes `core/snapshot.parquet` and
`core/node/emil_id=<EMIL>/<blob16>.parquet`. Read with `mis.raw(table)`,
`mis.snapshots()`, `mis.nodes()` (polars lazy scans). Artifacts will carry the most
restrictive classification of their inputs; `export()` refuses Secure/ECEII outside
`data/`. Drive builds from a script or notebook, not from `python -` (the process
pool re-imports `__main__`).

## Layout

```
src/ercot_mis/
  __init__.py     open(), re-exports
  config.py       data folder resolution + synced-folder warning; Identity from env/.env
  products.py     PRODUCTS: EMIL specs (report type, class, window, source, pull/track)
  mis.py          Mis: the session object; list, fetch, ingest, probe, build_raw, build_core, raw, snapshots, nodes
  retry.py        retrying(): fixed back-off for stalled downloads and listings
  build.py        raw layer: parse packages in a process pool, write Parquet, register artifacts
  core/identity.py  equipment-based node keys; CRR tie contraction (dam_nodes, crr_nodes)
  core/build.py   core.snapshot (IDs and revisions) and core.node
  sources/ews.py  EWS: build_request, sign (SHA-1 WS-Security), parse_reports,
                  EwsClient (list_documents + download = the Source interface)
  store/archive.py  content-addressed store (archive/<EMIL>/<sha256>.zip, read-only,
                  atomic via archive/.partial), zip member hashing
  store/catalog.py  DuckDB catalog (read-only by default, short writes): remote_doc, archive_blob,
                  archive_member, archive_source, run, artifact, lineage
  parsers/_common.py  Column specs, snake_case, cast_column, read_delimited (Arrow CSV,
                  exact header check), read_sheet (xlsx via fastexcel); ParseError
  parsers/psse.py   PSS/E v30 RAW -> psse_* tables, both dialects
  parsers/crr.py    CRR package member classification + parsers -> crr_* tables
  parsers/dam.py    DAM package member classification + parsers -> dam_* tables
scripts/validate_parsers.py  parse archived packages; check counts (RAW sections, DAM RAW vs CSVs)
scripts/probe.py archive-depth probe over every EWS product
scripts/probe_keys.py  key-matching and node-identity measurements (counts only); feeds identity-and-matching.md
scripts/daily_pull.py  fetch pulled EWS products, list tracked ones; launchd template in scripts/launchd/
tools/check_confidential.py   pre-commit guard (stdlib only; also blocks Keychain-stored secrets)
.github/workflows/ci.yml  tests + guard on every push
.githooks/pre-commit
tests/            synthetic-only tests; test_guard also scans every tracked file
```

## Milestones

| # | milestone | status |
|---|---|---|
| M0 | scaffold, config, EWS client, archive probe | done — EWS depth = display window |
| M1 | archive store, catalog, fetch (+ tracked products), ingest `ftr_align/ercot_data`, Public API archive client; **daily scheduled pull** (required by the M0 finding); DAM capture starts | EWS half done and verified live; first pull 2026-09-15 archived every listed EWS document (1.8 GB) incl. the two `ftr_align/ercot_data` zips via ingest; daily scheduling not yet installed; Public API client waits for credentials |
| M2 | PSS/E v30 parser + CRR raw layer (CSV vs XML check picks canonical) | parsers done (PSS/E both dialects, CRR, DAM); `scripts/validate_parsers.py` clean on all 29 CRR packages and 768 DAM hourly models (2026-09-15). Not yet: writing raw Parquet (lands with the M3 runner), DynamicRatings, PowerFlowData.jl cross-check (needs Julia) |
| M3 | core layer + SQL runner, cache skip, lineage, validation checks | 2026-09-17: raw writer with provenance (`build_raw`, process pool, cache skip, `run`/`artifact`/`lineage`); `core.snapshot` and `core.node` with equipment-based keys (`build_core`); built on every archived package (930 snapshots, 12,570 DAM node keys, 8,972 present in all 816 hours). Not yet: SQL runner, branch/rating/contingency/GTC core tables |
| M4 | `out.network` + `ftr_align/cases/ercot.py` | |
| M5 | DAM prices, awards, settlement point weights, `out.hourly_injection` | |
| M6 | CRR ↔ DAM matching + scorecard | |
| M7 | `sensitivities` (restricted float32 PTDF/LODF, cache budget, cross-test vs `ftr_align.network.compute_ptdf`) | |
| M8 | scheduled pulls (Python files), docs, first release | |

## Pick up here (next session)

State at 2026-09-17: M0–M2 done; M3 half done (raw layer, snapshots, nodes). The
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
- `uv run python scripts/probe_keys.py` after a new CRR month posts; update the note.

Then, in order:
1. **Core branches and ratings** (`core.branch`, `core.branch_rating`): lines by RAW
   comment, transformers by `Autos` (from, to, ckt), endpoints as `node_key`s, CRR
   ties marked; ratings from the monitored CSV and RAW rate A/B/C side by side.
2. **Core contingencies and GTCs** (`core.contingency`, `core.contingency_outage`,
   `core.gtc`, `core.gtc_member`): CRR from its CSVs; DAM contingencies from `Ctg`
   (branch rows by key, load/generator/SP rows by (bus, id), split-bus rows kept as
   their own kind); DAM GTC limits need a parser for `NP3-766-M` (xls) and definitions
   from `NP3-770-M` — write dataset notes for both and check names against the CRR GTCs.
3. **Matching** (`core.match_node`, `core.match_branch`, ...): settlement points →
   nodes; branch `Operations_Name` normalization + prefix rule (measure and document);
   endpoints → nodes; contingencies by name then members. Every row has
   `match_method`; unmatched rows are output.
4. **SQL runner** for `models/core/*.sql` once the Python-built tables settle; wire
   `build_core` into the daily pull after the fetch.
5. **Public API client** (`sources/public_api.py`): B2C ROPC token, subscription key,
   paged archive listing, retry on 429; secrets via `load_secret()` (Keychain). Then
   pull NP4-191-CD shadow prices and test the monitored/secured reading.
6. `ftr_align/cases/ercot.py` needs a sparse PTDF (scipy `splu`) and a screened row
   set: 11k elements × 10k nodes × 6.5k contingencies is not a dense `K`.

Open decisions for the user: keep capturing every DAM day (~10 GB/yr zipped, ~6 GB/yr
raw Parquet)? Install Julia for the PowerFlowData.jl cross-check? Move the Public API
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
- Probe sizes (2026-09): CRR long-term ~17 docs/yr, ~43 MB each; CRR monthly
  ~12/yr, ~20 MB; DAM PSS/E model 1/day, ~29 MB; SCED shift factors ~1,200
  docs/month, ~690 MB/month (tracked only).

## Environment

- `uv sync`; public PyPI is forced via `[[tool.uv.index]]` (this machine's default
  index is private).
- `uv run pytest -q`
- `git config core.hooksPath .githooks` once per clone.
- Credentials: `~/.ercot/api.crt`, `~/.ercot/api.key`; identity in `.env`; Public API
  secrets in `.env` or the macOS Keychain (`security add-generic-password -s ercot-mis -a <VAR> -w`).
- `data/` is excluded from Time Machine (`tmutil addexclusion data`, sticky xattr).
- The launchd job runs at 07:00 local (Eastern on this machine).
