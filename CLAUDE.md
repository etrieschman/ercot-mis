# ercot-mis, working notes

ercot-mis fetches ERCOT Market Information System (MIS) data, keeps the original files,
and standardizes them into tables that record which ERCOT file and which code produced
every row. It is a public repository holding code only. Its job is to be a faithful
fetcher for the CRR and DAM network models and their inputs. Research on those models
belongs to `~/dev/ftr_align`, which reads what this repo writes.

## Confidentiality

- ERCOT Secure and ECEII data, and anything derived from it, live only in `data/`
  (gitignored) or the folder `ERCOT_MIS_DATA` names. They never go into code, tests,
  documentation, commit messages, issues or pull requests.
- Inspecting local data prints structure only: column names, record counts, sizes, date
  ranges, report group names. It never prints a node, bus, substation, device,
  contingency or constraint name, and never a document file name, because ERCOT file
  names embed the participant DUNS.
- The DUNS and the API user come from the environment or `.env`. `Identity` hides both
  from its repr.
- Tests use synthetic data only (`tests/fixtures/synthetic/` when files are needed).
- `scripts/check_confidential.py` is the pre-commit hook and runs in the test suite. It is
  never bypassed with `--no-verify`.
- Putting ECEII in a cloud service needs the user to check ERCOT's ECEII obligations first.
- Before printing any column of a raw or core table, mask it or count it. A real viewer
  page is opened only with `?nohash`, so its address never leaves the machine.

## Rules

- **Protocol first.** Before the code picks a rule or a weight, find ERCOT's own rule
  (Nodal Protocols, a product README) and ERCOT's data for it. Only where ERCOT publishes
  nothing may the code pick, and then the pick is a `forced` row of the register whose
  check names the script that measures it.
- **Definitions come from the document that publishes them.** Inferring a constraint's
  members from prices is a diagnostic, never a source. No PDF is parsed yet.
- **Facts are data; judgment calls are named options.** An option exists only if a check
  varies it; every other rule is ERCOT's practice, fixed in code and named in the
  register. `Options` has `contract_ties` and `time_of_use`.
- **The register is for choices, `diff_*` is for differences.** Where the two ERCOT
  models disagree (a rating, a load's status, a settlement point's buses), the
  difference is recorded on both sides and nothing is smoothed.
- **The checks are reports.** They compare counts and differences and have no pass or
  fail threshold. No statistics in the code: no fits, no regressions, no correlations.
- **Models are tested against what cleared**, never re-cleared.
- **Numbers do not go in markdown.** Measurements live in the scripts that make them and
  the reports under `data/reports/`. Product ids, protocol sections and column names are
  words.
- **Explaining the repo is a notebook cell, not a page.** `demo/tour.ipynb` walks through
  every call against its own gitignored `demo/data/`; it is committed without outputs.
- **Legibility.** The package reads layer by layer (`tests/test_layers.py`); one name per
  rule; records are frozen dataclasses without behavior; no wrapper layer; no helper used
  once; short declarative docstrings. The style reference is `~/dev/flexload`.
- **In chat, plain language.** No metaphors. A decision is stated as the thing, what is
  wrong with it, the choices, and the pick.

## Design

| topic | decision |
|---|---|
| scope | ERCOT only, in ERCOT's vocabulary: settlement point, electrical bus, time-of-use block, EMIL id, report type |
| job | fetch and standardize; the only writer of the data folder; consumers read |
| interface | Python only: `em.open()` returns a `Session`; no command line |
| transport | two thin clients: EWS (certificate, WS-Security) and the Public API (archive files, never JSON rows) |
| storage | Parquet (zstd, hive-partitioned by product), polars for reading, a DuckDB catalog for provenance and ad hoc SQL. No connection outlives a call: a notebook left open never blocks the daily pull |
| data folder | `data/` in this repository, or `ERCOT_MIS_DATA` |
| bus identity | an equipment-based `bus_key`, never a PSS/E number or name (DAM numbers change every hour; DAM names are substations). CRR nodes joined by in-service ties share one key (`core/bus.py`, `docs/datasets/identity-and-matching.md`, measured by `checks/identity.py`) |
| network | `out.network` keeps CRR ties as branches and enforces the monitored ones, as ERCOT's auction does; contraction is `Options(contract_ties=True)` and `checks/network.py` measures its cost |
| ratings | both kept: the CRR monitored-element CSV per time-of-use block, and the RAW's rate A, B and C. Each model's network enforces its own |
| PSS/E parser | our own v30 parser (`raw/psse.py`), one tokenizer for both ERCOT dialects, Arrow for types |
| CRR scope | annual auctions (every sequence and update) and monthly, with each auction's posted results |
| DAM scope | network models for every day, prices, shadow prices, the system price, the pricing inputs and the 60-day disclosure's awards |
| shift factors | `SYS-608-CD` is listed, not fetched |
| schedule | a daily launchd job (`scripts/daily_pull.py`), because EWS keeps nothing past a product's display window |

### Vocabulary

- A **node** is a point of a node-breaker model: one RAW record of the CRR model, the
  finest point its closed breakers separate. `core.node` holds the CRR nodes
  (`node_number`, `node_type`, `bus_key`, `is_tie_member`); CRR branches carry
  `from_node` and `to_node`. The DAM model has no nodes.
- A **bus** is a point of a bus-branch model: one RAW record of the DAM model
  (`bus_number`, `bus_type`), or the CRR nodes that closed breakers join into one
  electrical point (`n_nodes`). `core.bus` holds both models' buses, keyed by
  `bus_key`. Matching, settlement point weights and loads are at bus level. DAM branches
  carry `from_bus_number` and `to_bus_number`.
- A **substation** is the physical yard, with buses at several voltage levels. Never
  "station", except inside a quoted ERCOT header.
- `out.network` rows are buses, because a solver's model is bus-branch. With CRR ties
  kept (the default) every node is a bus of one node and `bus_key` repeats across a tie
  group; with `contract_ties` the rows are `core.bus`. Its frames are `buses`, `bus_id`,
  `bus_index`, `settlement_point_buses`, `dropped_buses`.
- PSS/E calls every record a bus. Only the raw layer keeps that word (`raw.psse_bus`).
- An **electrical bus** is ERCOT's name for a pricing point in its price and mapping
  files. It appears in raw tables only.
- A **snapshot** is one model: a CRR month, a CRR annual sequence and term, or one DAM
  hour. Its id carries a **revision**, the posting order within the package it belongs
  to (`crr:monthly:2026-10:r1`, `dam:2026-10-14:he07:r1`).
- A **tie** is a CRR branch at PSS/E's minimum reactance: a closed breaker or switch.
- A **GTC** (generic transmission constraint) is a limit on the sum of flows over several
  branches, enforced as a base-case constraint in CRR, DAM and real time.

### Layers

Layer = folder = DuckDB schema. Columns use unit suffixes (`_mw`, `_mva`), `is_` for
booleans and `_code` for categoricals. Hourly facts carry `interval_start_utc` and ERCOT's
`delivery_date`, `hour_ending` and `dst_flag`, and join on the instant (`clock.py`).

- `archive/` holds the original ERCOT files, content-addressed by sha256, never edited or
  unzipped to disk. An `_Upd` file is a new document, a revision, not an overwrite.
- `raw` holds one typed Parquet table per source file, named for the file
  (`raw.crr_monitored_lines_and_transformers`): snake_case names and types, nothing else.
- `core` holds tidy tables shared by CRR and DAM, keyed by `snapshot_id`:
  - `snapshot`: one row per model with its revision; DAM rows carry `interval_start_utc`.
  - `bus`: one row per bus; `node`: one row per CRR node.
  - `branch` and `branch_rating`: a stable `branch_id`, bus keys at both ends, tie,
    service, monitored and secured flags; the RAW rates and the CRR CSV ratings side by side.
  - `contingency` and `contingency_outage`: each model's rows resolved to branch and bus
    keys, with `is_resolved`; a split-bus row names the end that moves (`split_end`).
  - `gtc` and `gtc_member`: CRR constraints with members from the CRR CSV; DAM hourly
    limits from the GTL workbook with `crr_gtc_id` from the manual crosswalk
    (`data/overrides/gtc_names.csv`) and no members.
  - `settlement_point` and `settlement_point_bus`: kind and bus weights summing to one.
  - `load`: one row per load with its node (CRR) or bus (DAM), status and MW; the DAM's
    zone, distribution factor and rollover flags.
  - `hourly_award`: every energy award of the 60-day disclosure as a signed MW at a
    settlement point; `session.injections(day)` sums it to the net injection `q`.
  - `match_branch`, `match_bus`, `match_contingency`: one CRR snapshot against one DAM
    snapshot, every row with its `match_method`, unmatched rows kept. Computed per
    session, in memory.
  - `diff_branch`, `diff_settlement_point`, `diff_load`: both models side by side with
    `same_*` verdicts.
  - `coverage` is a module: counts of what a snapshot's tables carry and lack.
- `out` is what consumers read: `out.network` (`session.network(snapshot_id, Options)`,
  assembled from core on every call; conventions in `docs/out-network.md`) and
  `out.injection` (`session.injections(day)`).
- `checks` tests the layers against what ERCOT published. Each writes a report under
  `data/reports/<check>/`.

### Provenance

The catalog (`catalog.duckdb`) has `remote_doc` (every listed document), `archive_blob`
(distinct bytes), `archive_member` (zip members by hash), `archive_source` (one row per
arrival), `run`, `artifact` (one row per written file) and `lineage` (artifact to members).
An artifact's key is the hash of the parser and layer versions, the blob and the table: an
existing key and file is skipped, a bumped `VERSION` rebuilds every artifact the module
produced. Every raw row carries its identity: `emil_id`, `doc_id`, `blob_sha256`,
`member_sha256`, `member_path`, and what the file name says (CRR `auction`, `term`,
`sequence`, `month`, `time_of_use`; DAM `operating_date`, `hour`). Files live at
`<layer>/<table>/emil_id=<EMIL>/<blob16>.parquet`; `session.raw(table)` and
`session.core(table)` scan a table across products, with the columns a product lacks null.
Builds run from a script file with a `__main__` guard, one build per data folder at a time.

### Domain facts behind the model

- The DAM package is one day holding 24 hourly models: a PSS/E RAW per hour with CSVs for
  contingencies, lines, transformers, loads, generators, settlement points and hubs, plus
  the day's settlement point contingencies, non-biddable resource nodes and a README.
  Hourly models renumber part of their buses, so no two hourly files are byte-identical;
  the equipment maps are identical across hours.
- The CRR model is node-breaker: thousands of branches at PSS/E's minimum reactance are
  closed breakers and switches, some monitored with real breaker ratings. The DAM model
  is bus-branch and has no branch below its own reactance floor. ERCOT's auction solves
  the CRR model with the ties, and so does `out.network` by default.
- Both RAWs already reflect outages: status OUT is the outage. The CRR Outages file is
  informational.
- CRR line names follow the RAW's from and to. CRR transformer names come from the mapping
  workbook's `Autos` sheet, matched to the RAW by (from, to, ckt), and run the other way
  for a large minority (`is_name_reversed`). A CSV's flow direction refers to the name.
- The DAM package carries no GTC file. GTC limits come from the GTL workbook
  (`NP3-766-M`, hourly DAM and real-time limits under readable names); the CRR CSV uses
  codes, so the crosswalk is manual. Member definitions are in `NP3-770-M`, one PDF per
  constraint, not yet read.
- The DAM's `Monitored?` and `Monitored and Secured?` columns are the CIM `DAM Monitored`
  and `DAM Secured` flags. Secured means enforced; monitored only means reported.
- The CRR's `BaseCaseRating` is a fixed fraction of the RAW's rate A on every monitored
  line, and its time-of-use blocks can carry identical ratings for a month.
- A DAM hub's weights follow Protocols 3.5.2: equal weight per hub bus, then equal weight
  per energized bus inside it; the two average hubs are built from the four regional
  hubs. Load zone weights are the in-service loads' distribution factors from the `Ld`
  file, with an out-of-service load's share rolled to the targets the file names. A
  combined-cycle train with units on several buses takes an equal share per unit,
  because ERCOT's rule (Protocols 4.6.1.4) weighs by registered limits that no file carries.
- The net injection `q` is not published. It is built from the 60-Day DAM Disclosure
  (`NP3-966-ER`): generation and storage awards at resource nodes, energy-only offer
  awards as positive, energy bid awards as negative, point-to-point awards positive at the
  source and negative at the sink. The disclosure arrives sixty days after the models.
- Matching CRR to DAM: settlement points first (every CRR source and sink name appears
  among DAM settlement points), then branches by the workbook's `Operations_Name` with
  the DAM's circuit suffix stripped, then buses through the ends of matched branches,
  then contingencies by name and by member set. Numbers and names never match across
  models. Manual overrides live in `data/overrides/`.
- An annual auction sells six consecutive six-month terms over three years, sequence six
  the furthest out. The monthly model is the closest to the DAM.
- The auction results post a shadow price per binding constraint and per settlement point
  for each time-of-use block. The constraint's is for one MW over the block's hours; the
  settlement point's is per MWh and is the marginal cost of injecting there, so a PTP
  Obligation clears at the source's price minus the sink's, and a linked three-block bid at
  the blocks' hour-weighted mean. The blocks are hours ending 7 to 22 on non-holiday
  weekdays, the same hours on weekends and NERC holidays, and the rest (Protocols 7.3(5)).
- EWS keeps nothing older than a product's display window: an unbounded listing returns
  exactly the window and an explicit older window returns "No reports found". History
  cannot be backfilled; a document not captured in time is gone. The Public API keeps
  years, so a gap there heals itself.

### EWS

- An API certificate is required and the user id starts with `API_`.
- A call needs mutual TLS and a WS-Security signature over the body, with a SHA-1 digest
  and an RSA-SHA1 signature; the server checks the digest first.
- The SOAPAction is a path, not a URN.
- `Header` and `Request` children are an `xsd:sequence`: element order matters.
- Message.xsd uses the "www.docs" WSS namespaces for ReplayDetection and the Security
  header uses the OASIS ones; both stay as they are.
- Replies are parsed by local name, because ERCOT has changed namespaces before.
- An empty listing is `ReplyCode=ERROR` with "No reports found", not an empty payload.

## Layout

The package reads in one direction; `tests/test_layers.py` fails on a backward import.
Each layer owns its `build`; `Session` strings them together and is the only writer.

```
src/ercot_mis/
  products.py, config.py, clock.py   EMIL product specs; data folder, identity, secrets; ERCOT's hours as UTC instants and the CRR time-of-use blocks
  ews.py            EWS client: build_request, sign, parse_reports, EwsClient; retrying() for stalled calls; probe
  public_api.py     Public API client: ID token, paged archive listing, paced calls
  store.py          the archive: content-addressed files, read-only, atomic; zip member hashing
  catalog.py        the DuckDB catalog; posting_order (TOP-11); writing(), ReadOnlyCatalog
  fetch.py          list_documents (a listing into the catalog), fetch (a document into the store), ingest (a local file)
  raw/              layer 1: one typed table per source file
    table.py        Column specs, Member, read_delimited, read_sheet, flag; ParseError
    psse.py         PSS/E v30 RAW -> psse_* tables, both dialects
    crr.py, dam.py  package members classified and parsed -> crr_* / dam_* tables
    crr_auction.py  CRR auction results (NP7-802-M, NP7-803-M) -> crr_auction_* tables
    gtl.py          the GTL workbook (NP3-766-M) -> gtl_hourly
    prices.py       DAM prices and pricing inputs (Public API)
    mappings.py     settlement point and hub mappings (NP4-160-SG, NP3-220-SG)
    disclosure.py   the 60-Day DAM Disclosure (NP3-966-ER)
    build.py        parse packages in a process pool, write Parquet with identity columns, register artifacts
  core/             layer 2: tidy keyed tables shared by CRR and DAM
    snapshot.py     snapshot ids and revisions
    bus.py          core.bus (both models) and core.node (CRR): equipment-based bus keys
    branch.py       core.branch and core.branch_rating
    contingency.py  core.contingency and core.contingency_outage
    gtc.py          core.gtc and core.gtc_member
    settlement_point.py  core.settlement_point and core.settlement_point_bus
    load.py         core.load
    award.py        core.hourly_award
    match.py        core.match_branch, core.match_bus, core.match_contingency
    diff.py         core.diff_branch, core.diff_settlement_point, core.diff_load
    coverage.py     counts of what a snapshot carries and lacks
    build.py        every core table per package
  out/              layer 3: what consumers read
    network.py      Network for one snapshot, in six steps; Options
    shift_factors.py  DcSystem: the one place shift factors are computed (the shift-factors extra)
    injection.py    one day's awards as q per settlement point and hour
  checks/           layer 4: the layers against ERCOT's results; each writes data/reports/<check>/
    binding.py      the DAM's binding rows of one hour resolved onto our network
    prices.py       the price identity per DAM hour
    crr_prices.py   the CRR auction's shadow prices per time-of-use block on the monthly CRR model
    flows.py        the awards through the network against ERCOT's flows
    injections.py   the awards balance per hour
    ldf.py          ERCOT's load distribution factors against the Ld file
    network.py      the DC solve, ties kept and contracted
    identity.py     how CRR and DAM keys line up
    register.py     a measurement for every register row no other check covers
  viewer/           a single-file network viewer; its output is CEII
  session.py        Session, the hub: list, fetch, ingest, probe; build_raw, build_core; raw, core, core_rows, raw_rows;
                    network, injections, match_*, diff_*, viewer, newest, hour_ids
  pull.py           the daily pull's bookkeeping: fetch_products, build_layers, at_risk
demo/tour.ipynb              the walkthrough of every call; pulls into demo/data/
scripts/check.py             runs one check: prices, flows, crr_prices, injections, ldf, network, identity, register
scripts/daily_pull.py        fetch, build, the price, flow and CRR price checks, the at-risk line; launchd template in scripts/launchd/
scripts/urgent_pull.py       EWS products only; run when the daily pull's notification says data is at risk
scripts/probe.py             what EWS offers for each product
scripts/validate_parsers.py  parse every archived package and check counts
scripts/check_confidential.py  the pre-commit guard; plain python3, no session
tests/                       synthetic tests; test_guard scans every tracked file
```

`VERSION` constants (each raw parser, every core table module, `core/build.py`,
`core/match.py`, `core/diff.py`) are cache keys: bump one when a module's output changes
and every artifact it produced is rebuilt. `numpy` and `scipy` belong to the
`shift-factors` extra, needed by `out.shift_factors` and the checks.

## Open work

Health comes first. `data/logs/last_run.json` says `ok`, `missed` (its flags say why) or
`urgent` (EWS data within a week of rolling off; run `scripts/urgent_pull.py`).
`data/logs/daily_pull.log` ends each run with `failed 0`, the at-risk line, a `price check`
line, a `flow check` line and a `crr price check` line. The job is scheduled for 07:00, but the Mac is asleep then and
launchd runs it on wake; the start times in the log show when.

The first four items are what stands between the repo and its two aims: reproducing what
the DAM cleared, and comparing the two network models.

1. **GTC members** (PRC-04). ERCOT publishes the DAM's GTC limits in the GTL workbook
   (NP3-766-M) and the member branches only in NP3-770-M, one PDF per constraint, which the
   repo does not read. The price check borrows the members from the CRR month's file and
   marks the rows whose member list is incomplete. The hours where a GTC binds are the
   hours the price check misses. Reading the PDFs is the user's call.
2. **What a contingency disconnects** (NAM-07, PRC-03). When a contingency cuts buses off
   from the grid, the network zeroes their settlement point weights and renormalizes the
   rest. ERCOT publishes which settlement points each contingency disconnects (the DAM
   `SpCtg` file) for few of the binding contingencies, and where it does, our list rarely
   agrees. The flow check misses most on rows applied under such a contingency. Find
   ERCOT's rule for a disconnected settlement point first, then measure against it.
3. **Matching** (MAT-01 to MAT-07). The CRR branches without a DAM match are mostly ties
   and out-of-service branches, which have no DAM counterpart. The ones that matter are
   the in-service, monitored CRR branches and the secured DAM branches with no match, and
   the unmatched buses that sit in the solved networks. `core.coverage.pair_coverage` and
   `checks/identity.py` measure them; the workbook's placeholder operations names are the
   first place to look.
4. **CRR auction results** (CRA-01 to CRA-04). The monthly auction's settlement point shadow
   prices are rebuilt from its binding constraints on the monthly CRR model, and the check
   runs on every month with results and a model. Most points land within a dollar per MWh. Two
   steps remain, in this order:
   a. The points that miss by dollars. For the worst points in one block, list the binding
      rows whose shift factor at that point is largest, and see what those rows share: a
      contingency outcome, a transformer with `is_name_reversed`, a GTC, a settlement point
      spread over several buses. The rows' `rows_by_outcome` is already in the report.
   b. The long-term auctions (NP7-802-M). They clear six months at once on the annual models,
      so the check needs the annual snapshot for each calendar period of the file. The daily
      pull is still archiving the older documents.
5. **The awards lag.** The 60-day disclosure arrives two months after the models, so the
   flow check runs on the newest day with both. Nothing to do but keep capturing.

The two models rate the same branch differently. `core.diff_branch` records both sides;
the difference is a result of the comparison, not a choice (RAT-04).

Then:

6. **Register rows the measurements reopened**: TOP-03 and TOP-04 (branches the CRR
   Outages file names are in service in the RAW), TOP-06 (the CRR network keeps more than
   one swing record), NAM-01 (a few CRR line names run from the to end).
7. **Combined-cycle trains on several buses** take an equal share per unit (SP-05). An
   ERCOT product that publishes unit limits would replace the equal split with Protocols
   4.6.1.4.
8. **Viewer**: the coverage table on a real pair; `diff_load` in the side panel; the DAM
   load column labelled LDF, because the DAM RAW gives every load zero MW.
9. **Smaller leftovers**: count the duplicate rating, match and GTC rows a de-duplication
   resolves (RAT-12); the small offset between the fitted and the published system price
   in hours with few binding rows (`system_price.fitted_median_minus_published`); the
   injections and LDF checks run only by hand.
10. **Planning-stage inputs** as the user asks, and the EWS report type ids for
    transmission outage reports.
11. **A cross-model network** on one bus set. The rule for a CRR bus that maps to two DAM
    buses is the user's.
12. **A second copy of the data.** One pull and one archive exist, on this Mac, excluded
    from Time Machine. EWS keeps DAM models for 31 days, so a disk loss is permanent. The
    `rsync` in the README, on a schedule, would give a second copy.

Open register rows: RAT-05, RAT-11, NAM-06, NAM-07, PRC-04, TOP-04, TOP-07, TOP-11, SP-06,
SP-07, MAT-06, MAT-07.

Decisions that are the user's: a daily wake for the Mac; Julia for the PowerFlowData.jl
cross-check; capturing every DAM day; where the second copy goes.

## Environment

- `uv sync`; `pyproject.toml` forces public PyPI, because this machine's default index is
  private.
- `uv run pytest -q`
- `git config core.hooksPath .githooks`, once per clone.
- Credentials: `~/.ercot/api.crt` and `~/.ercot/api.key`; the identity in `.env`; Public
  API secrets in `.env` or the macOS Keychain (`security add-generic-password -s ercot-mis
  -a <VAR> -w`).
- `data/` is excluded from Time Machine (`tmutil addexclusion data`).
- The launchd job is installed as `~/Library/LaunchAgents/ercot-mis.daily-pull.plist`.
