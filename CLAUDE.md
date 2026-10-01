# ercot-mis — working notes for Claude

Fetch, cache and standardize ERCOT MIS data locally, with every table traceable
to the bytes ERCOT published. **Public repo, code only.** First consumer:
`~/dev/ftr_align` (its ERCOT rung compares CRR and DAM network models).

## Confidentiality rules — read first

- ERCOT Secure/ECEII data, and anything derived from it, lives only in `data/`
  (gitignored) or `$ERCOT_MIS_DATA`. Never copy it into code, tests, docs, commit
  messages, issues or PR text.
- When inspecting local data, print **structure only**: column names, record
  counts, sizes, date ranges, report group names. Never print node, bus, substation,
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
| entry point | `em.open()` → `Session`; methods `list`, `fetch`, `ingest`, `probe`, `build_raw`, `build_core`, `raw`, `core`, `match_*`, `diff_*`, `network`, `viewer` |
| transport | own thin clients: EWS (certificate) and Public API (archive files, not JSON rows) |
| transforms | Python parsers → `raw`; Python (polars) → `core`, `out`. The `.sql` runner was dropped on 2026-10-01; DuckDB remains for the catalog and ad hoc SQL |
| storage | Parquet on disk (zstd, hive-partitioned), Arrow in memory, DuckDB catalog + SQL engine, polars for reading. **Catalog writes are short-lived**: `Session` reads through a read-only connection and takes the writer only to record a listing, a blob or an artifact, never across a download or a parse |
| bus identity | **equipment-based `bus_key`**, not PSS/E number or name (DAM renumbers every hour; names are substation names). CRR bus ties (closed breakers and switches at PSS/E's minimum reactance, in service) are contracted **for identity and matching only**; `core/node.py`; explained in `docs/datasets/identity-and-matching.md`, measured by `scripts/measure_identity.py` |
| fidelity | **defaults do what ERCOT does; our analysis choices are named options.** `out.network` keeps CRR ties as branches and enforces the monitored breakers, as the auction does; contraction is `Options(contract_ties=True)`. Every assumption, its status and how it was checked lives in `docs/assumptions.md` (update it in the same commit as the code) |
| data location | `data/` in this repo by default; `ERCOT_MIS_DATA` overrides |
| network output | standardized tables + optional `ercot_mis.shift_factors` (DC solve and shift-factor rows, base case and under a contingency) |
| ratings | keep both: CRR monitored-element CSV (enforced, default) and PSS/E Rate A/B/C (MVA) |
| conventions | facts stored as data; judgment calls as named options defaulting to ERCOT practice |
| PSS/E parser | our own focused v30 parser (`raw/psse.py`): one compiled tokenizer for both ERCOT dialects (~0.3 s per RAW), Arrow for type conversion. Arrow's CSV reader can't split the blank-separated DAM RAW, so it is used for the CSVs only. PowerFlowData.jl as an optional reference check; **not** VeraGridEngine |
| CRR scope | annual (all sequences + updates) and monthly |
| DAM scope | LMPs, SPPs, 60-day disclosure awards → node-space injections; network models for **every day** (all 24 hours, ~10 GB/yr zipped); shadow prices for validation |
| shift factors | `SYS-608-CD` tracked (listed into catalog) but not pulled |
| schedule | daily at 07:00 via launchd (`scripts/daily_pull.py`, installed as `~/Library/LaunchAgents/ercot-mis.daily-pull.plist`); required because EWS keeps nothing past the display window |

### Vocabulary (settled 2026-10-01; the user's research and node-breaker convention)

- **node** = the finest connection point a model gives: one RAW record. PSS/E calls it a
  "bus", and only the raw layer keeps that word (`raw.psse_bus`); processed layers say
  `node_number`, `node_type`, `from_node`, `to_node`.
- **bus** = nodes joined by closed breakers. `bus_key` identifies it; matching
  (`core.match_bus`, `session.match_buses`) and settlement point weights
  (`core.settlement_point_bus`) are at bus level. A DAM node is a bus on its own.
- **substation** = the physical yard (several voltage levels). Never "station", except
  where a raw column quotes ERCOT's header.
- `out.network` vertices are "nodes" (model nodes by default; buses with `contract_ties`).

Do not write "node" for the merged thing or "bus" for a CRR bus section again.

### Layers and naming

- `archive/` — original ERCOT zips, content-addressed by sha256, never edited or
  unzipped to disk; `_Upd` is a new document (revision), not an overwrite.
- `raw` — typed Parquet, one table per source file, named for the file
  (`raw.crr_monitored_lines_and_transformers`). snake_case + types only.
- `core` — tidy keyed tables. Network tables are **shared by CRR and DAM** and keyed
  by `snapshot_id` (`crr:annual:2029.1st6:seq6:2029-01:r2`, `crr:monthly:2026-10:r1`,
  `dam:2026-10-14:he07:r1`). Built so far: `core.snapshot` (one row per model, revision
  = order of `posted_at` within a logical package), `core.node` (one row per node, that is per RAW record,
  per snapshot: `bus_key`, `substation`, `kv`, `bus_group`, `is_tie_member`, attachments),
  `core.branch` (stable `branch_id`, endpoints as bus keys, tie/in-service/monitored/
  secured flags), `core.branch_rating` (RAW rate A/B/C and CRR CSV ratings per TOU,
  side by side), `core.contingency` + `core.contingency_outage` (each model's own
  vocabulary resolved to branch/bus keys, `is_resolved`, split-bus rows kept),
  `core.gtc` + `core.gtc_member` (CRR from its CSV with members; DAM hourly limits from
  the GTL workbook with `crr_gtc_id` from the manual crosswalk in
  `data/overrides/gtc_names.csv`, members empty), and `core.match_branch` /
  `core.settlement_point` + `core.settlement_point_bus` (kind, bus weights summing to
  one from CRR participation factors or DAM `Sp`/`Hb`/`Ld`), `core.load` (one row per load:
  node, service status, MW; DAM zone, distribution factor and rollover flags), and `core.match_branch` /
  `core.match_bus` per (CRR snapshot, DAM snapshot) via `session.match_branches` and
  `session.match_buses`, and `core.match_contingency` via `session.match_contingencies`
  (name, then translated branch set; member-set differences recorded).
  Planned: `core.constraint`, `core.hourly_lmp`, `core.hourly_spp`, `core.hourly_award`,
  `core.hourly_shadow_price`, `core.tou_hours`. `core.diff_branch`
  (`session.diff_branches`) puts matched branches' kind, reactance, voltage levels,
  service and ratings side by side with `same_*` verdicts; `core.diff_settlement_point`
  (`session.diff_settlement_points`) does the same for settlement point node sets.
- `out` — what consumers read: `out.network` (`session.network(snapshot_id, Options)`,
  assembled on first request and cached under `out/network/<snapshot>/<key>/` as Parquet, conventions in `docs/out-network.md`), `out.hourly_injection` (q).
- Layer = folder = DuckDB schema. No PUDL-style `layer_source__type` names.
  Columns: unit suffixes (`_mw`, `_mva`), `is_` booleans, `_code` categoricals.
- Hourly facts carry `interval_start_utc`, `interval_end_utc` **and** ERCOT's
  `delivery_date`, `hour_ending`, `dst_flag`.

### Domain notes that shape the model

- **GTCs**: the CRR package's Non-Thermal Constraints file gives name, limit and
  member devices with factor and flow direction; DAM limits come from the GTL
  workbook (see below). Each GTL is enforced as a **base-case** constraint in CRR,
  DAM and RT. A GTC's PTDF row is the factor-weighted sum of its members' rows.
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
  CRR and DAM numbers are unrelated, DAM names are substations), then contingencies by
  name with member sets compared in the matched vocabulary; GTCs by name then members/factors/limit. `match_*` records
  identity, `diff_*` records differences — never smooth differences over. Every match
  carries `match_method`; unmatched records are output. Manual overrides live in
  `data/overrides/` (gitignored), read by core when present.
- **CRR is closer to node-breaker, DAM to bus-branch**: CRR RAWs hold thousands of
  branches at PSS/E's minimum reactance (closed breakers; some monitored with real
  breaker ratings), DAM none below its own floor of 0.0005 pu, where the CRR ties it
  keeps sit. ERCOT's auction solves the CRR model with the ties; so does `out.network`
  by default (`scripts/check_network.py` measures the cost of contracting instead).
  `core.node` records the tie groups because matching needs them.
- **Both RAWs already reflect outages** (DAM README; CRR measured against its Outages
  file). Status OUT is the outage; the CRR Outages file is informational.
- **Name orientation**: CRR line names follow the RAW's from/to; CRR transformer names
  follow the `Autos` sheet, which is reversed for a large minority
  (`core.branch.is_name_reversed`). CSV flow directions refer to the name.
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
  renumbers its nodes**; the equipment maps are identical across hours. Raw stays
  one table per file; semantic dedup belongs in core. The DAM snapshot key needs an hour: `dam:2026-10-14:he07:r1`.

### Dataset notes — `docs/datasets/`

Per-dataset design choices live in `docs/datasets/` (one note per dataset, fixed
template: what it is, capture, package layout, what we parse, decisions, ERCOT
quirks, validation, open questions). Read the relevant note before touching a
parser; **update it in the same commit** whenever an import decision changes or a
new quirk turns up. Notes are public: structure only, never names or values.
Written so far: `psse-raw.md`, `crr-network-model.md`, `dam-network-model.md`,
`identity-and-matching.md`, `generic-transmission-limits.md`, `dam-prices.md`,
`planning-inputs.md`. Beside them:
`docs/out-network.md` (the conventions and options of the network handed to a solver)
and `docs/assumptions.md` (the register of every assumption, checked or open).

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
`operating_date/hour`). `build_core` writes `core/snapshot.parquet` and, per
package, `core/<table>/emil_id=<EMIL>/<blob16>.parquet` for node, branch,
branch_rating, contingency, contingency_outage, gtc and gtc_member; the matchers
cache under `core/match_<kind>/v<N>/`. Read with `session.raw(table)` and
`session.core(table)` (polars lazy scans that span products; identity columns a
product lacks come back null). Artifacts will carry the most
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
    public_api.py   Public API: ID token, paged archive listing, paced calls, retry on 429; PublicApiClient
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
    prices.py       DAM shadow prices, bus LMPs, settlement point prices (Public API) -> dam_* tables
    build.py        parse packages in a process pool, write Parquet with identity columns, register artifacts
  core/             layer 2: tidy keyed tables shared by CRR and DAM
    snapshot.py     snapshot IDs and revisions from the catalog and member names
    node.py         equipment-based bus keys; CRR tie contraction (dam_nodes, crr_nodes)
    branch.py       core.branch and core.branch_rating (crr_branches, dam_branches)
    contingency.py  core.contingency and core.contingency_outage, resolved to branch/bus keys
    gtc.py          core.gtc and core.gtc_member (CRR members; DAM hourly limits + crosswalk)
    match.py        core.match_branch (exact -> ops+ckt -> prefix -> prefix+x), core.match_bus (settlement
                    point -> matched branch endpoints by vote), core.match_contingency (name -> members)
    settlement_point.py  core.settlement_point and core.settlement_point_bus (kind, bus weights; both models)
    load.py         core.load: every load with its node, service status, MW; DAM zone, LDF and rollover flags
    diff.py         core.diff_branch, core.diff_settlement_point and core.diff_load: both models side by side
    build.py        writes every core table per package
  out/              layer 3: what consumers read
    network.py      Network for one snapshot: nodes (or buses when ties are contracted), branches with limits, contingency
                    index sets, signed GTC members, settlement point weights, dropped elements with
                    reasons; Options = judgment calls
  shift_factors.py  DcSystem: the one place shift factors are computed (factorize once; outaged() re-solves under a
                    contingency, with split buses and islanding); needs the `shift-factors` extra (numpy, scipy)
  viewer/           single-file substation-by-substation network viewer (template.html + data builder); output is CEII
scripts/daily_pull.py        fetch pulled EWS products, list tracked ones, then build_raw + build_core; launchd template in scripts/launchd/
scripts/probe.py             archive-depth probe over every EWS product
scripts/validate_parsers.py  parse archived packages; check counts (RAW sections, DAM RAW vs CSVs, GTL hours)
scripts/measure_identity.py  key-matching and node-identity measurements -> data/reports/identity/*.json
scripts/check_prices.py      price identity per DAM hour: shadow prices x shift factors vs published prices -> data/reports/prices/*.json
scripts/check_network.py     DC solve on assembled networks; contracted vs ERCOT topology -> data/reports/network/*.json
tools/check_confidential.py  pre-commit guard (stdlib only; also blocks Keychain-stored secrets)
.github/workflows/ci.yml     tests + guard on every push
tests/                       synthetic-only tests; test_guard also scans every tracked file
```

Cache keys use explicit `VERSION` constants (each raw parser, every `core/*.py` table
module, `core/build.py`, `core/match.py`, `core/diff.py`): bump one when its output
changes and every artifact it produced is rebuilt (a full core rebuild is a few
minutes); cosmetic edits cost nothing. `numpy` and `scipy` are needed only by `ercot_mis.shift_factors` and the check scripts
(the `shift-factors` extra; also in the dev group).

**Numbers do not go in markdown.** Notes record process, decisions and quirks;
measurements live in the scripts that make them and the dated reports under
`data/reports/`.

## Milestones

| # | milestone | status |
|---|---|---|
| M0 | scaffold, config, EWS client, archive probe | done — EWS depth = display window |
| M1 | archive store, catalog, fetch (+ tracked products), ingest `ftr_align/ercot_data`, Public API archive client; **daily scheduled pull** (required by the M0 finding); DAM capture starts | EWS side done and running daily via launchd since 2026-09-15 (retries, status file, notification on failure); 2026-10-01: Public API archive client; DAM prices pulled daily |
| M2 | PSS/E v30 parser + CRR raw layer (CSV vs XML check picks canonical) | parsers done (PSS/E both dialects, CRR, DAM, GTL workbook); `scripts/validate_parsers.py` clean on every archived package. Not yet: DynamicRatings, PowerFlowData.jl cross-check (needs Julia) |
| M3 | core layer + SQL runner, cache skip, lineage, validation checks | 2026-09-17: raw writer with provenance (`build_raw`, process pool, cache skip, `run`/`artifact`/`lineage`); `build_core` writes snapshot, node, branch, branch_rating, contingency, contingency_outage, gtc, gtc_member for every archived package. 2026-09-24: both builds run at the end of the daily pull. 2026-10-01: SQL runner dropped; register lint (`tests/test_register.py`); `scripts/check_prices.py`. Not yet: flow check, status page |
| M4 | `out.network` + `ftr_align/cases/ercot.py` (on hold) | 2026-10-01: cached on disk (`out/network/`). 2026-09-24: `session.network()` (per-snapshot, own vocabulary, ERCOT's topology by default, settlement point weights, audit frames, `docs/out-network.md`); `scripts/check_network.py` solves it. Not yet: on-disk `out/`, `ercot.py` (needs the sparse PTDF, see next steps) |
| M5 | DAM prices, awards, settlement point weights, `out.hourly_injection` | 2026-10-01: prices pulled daily and parsed; price identity check. Not yet: awards (60-day lag), injections |
| M6 | CRR ↔ DAM matching + scorecard | matchers for branches, buses and contingencies with `match_method` and unmatched rows (`session.match_*`); `scripts/measure_identity.py` reports their rates. 2026-09-24: `prefix+x` tie-break by reactance, `core.diff_branch`, `core.settlement_point[_node]`, `core.diff_settlement_point`. Not yet: `diff_bus`/`diff_contingency`, `subset` contingency method, scorecard |
| M7 | `shift_factors` (restricted float32 PTDF/LODF, cache budget, cross-test vs `ftr_align.network.compute_ptdf`) | 2026-10-01: `ercot_mis.shift_factors` (sparse factorization, re-solve per contingency), used by the check scripts. The dense PTDF/LODF cache is on hold with the research |
| M8 | scheduled pulls (Python files), docs, first release | |

## Pick up here (next session)

State at the end of 2026-10-01. Scope settled with the user that day: **an impeccable
fetcher for the network models and their inputs; research (ftr_align cases) is on
hold.** Three rules from the same day:

- **Vocabulary is strict** (section above): node, bus, substation. The user's research
  relies on node and bus in precise settings; only the raw layer keeps a file's own word.
- **The register is for choices, `diff_*` is for discrepancies.** Where our code decides
  something it cites a row of `docs/assumptions.md` (`tests/test_register.py` enforces
  it). Where two ERCOT models disagree, we change nothing and record it in a
  `core.diff_*` table.
- **Models are tested against what cleared, without re-clearing.** The price identity
  (`scripts/check_prices.py`) tests shift factors on binding constraints; the flow
  check (not built) will test limits and settlement point weights. **The user's bar: a
  settlement point off by more than a couple of dollars is a problem.**

Built on 2026-10-01 (all on `main`): register ids and lint; Public API client; DAM
prices pulled daily and parsed; `check_prices.py`; `ercot_mis.shift_factors` (the one
place shift factors are computed); `core.load`, `core.diff_load`,
`core.branch.is_temporary`; the viewer (`session.viewer`); networks cached on disk
(`out/network/`); the Public API catalogue sweep (`docs/datasets/planning-inputs.md`);
the vocabulary rename.

First, check health (2 min):
- `tail -40 data/logs/daily_pull.log` and `cat data/logs/last_run.json`: `failed 0`.
  Lines now carry seconds per product, and there are three Public API lines
  (`NP4-183/190/191`). **The 2026-10-02 run is the first to call the Public API from
  launchd (it reads the Keychain there): confirm it worked.** The 2026-10-01 run took
  two hours for a reason not yet known; the new timings will show where.
- `launchctl list | grep ercot-mis`: last exit code 0.
- After a new CRR month posts: `measure_identity.py`, `check_network.py` and
  `check_prices.py --day <a recent day>`; compare with the previous reports.

Then, in order:

1. **Price identity: get every settlement point within a couple of dollars.** State
   (day tried: 2026-09-30): hours with little GTC shadow price are within one to four
   dollars at worst; the evening hours, where one GTC binds hard, still miss by up to
   about eight dollars at a few dozen points. Found so far: direction read to the
   tenth of a kV; split-bus contingency rows; in GTC hours the residual is one fixed
   pattern times the GTC's shadow price, and regressing that pattern on nearby shift
   factors showed the DAM's definition has two parallel lines the CRR's lacks (kept in
   the gitignored `data/overrides/dam_gtc_members.csv`). NP3-770-M cannot supply
   definitions as data: it is PDFs and slides. Next, in this order:
   - run other days, to see whether the same GTC pattern and the same fix hold;
   - the rest of that GTC's pattern (a wider regression points at lines far from the
     interface with fractional factors, which does not look like a definition; check
     the CRR members' orientation and whether a second constraint is involved);
   - points a contingency cuts off (PRC-03): pull NP4-200-CD, NP4-158-SG, NP4-231-CD;
   - the system price from NP4-523-CD instead of a fitted intercept;
   - move split-bus rows (NAM-09) and GTC members (NAM-05) from the script into
     `out.network`, then price nodes as well as settlement points (parse NP4-160-SG).
2. **Viewer, from the user's first look** (they like it; text was still confusing):
   show `diff_load` in the side panel; keep simplifying labels; check layout on the
   largest substations; later, overlays for prices and flows. Look at it only on
   synthetic data (`tests/fixtures/synthetic/viewer_data.json` into the template), or
   on the real file with `?nohash` and counts only: a screenshot or the URL hash of a
   real page sends names off the machine.
3. **Substations in core.** `core.node.substation` is null for CRR (its RAW names
   nodes); the viewer assigns CRR substations through the bus match and matched lines.
   Move that into core (asked of the user on 2026-10-01, not yet answered), then build
   the per-substation table of switch outages active in a CRR model's month, which is
   the only level at which "normally open or open for an outage" can be answered.
4. **Flow check**, once NP3-966-ER overlaps the archived models (mid-October 2026):
   awards through the network; binding rows at their limit, nothing enforced above it.
5. **Planning-stage inputs**: pull and parse what `planning-inputs.md` lists as the
   user asks for them; find the EWS report type IDs for the transmission outage
   reports and the CRR auction results, which the Public API does not carry.
6. **Cross-model network** (one bus set for a CRR and a DAM network): the rule for a
   CRR bus that maps to two DAM buses is the user's call.
7. A generated status page (register x latest check result) with a notification when
   a row's result changes; `diff_bus`, `diff_contingency`, a `subset` contingency
   method; add `diff_load` counts to `measure_identity.py`.

Other open rows of `docs/assumptions.md`: RAT-04 (which RAW rate is the DAM's
post-contingency limit), SP-01 (protocol sections for hubs), SP-04, NAM-08 (the long
day, 2026-11-01).

Open decisions for the user: move CRR substation assignment into core? Keep capturing
every DAM day? Install Julia for the PowerFlowData.jl cross-check?

Slips to avoid repeating (2026-10-01): a URL hash and a `value_counts` on a column
assumed categorical each put a few equipment names into tool output. Mask or count
before printing any column of a raw or core table, and never navigate a real viewer
page without `?nohash`.

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
