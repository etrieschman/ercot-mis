# Identity and matching across CRR and DAM models

How buses, nodes, branches, contingencies and constraints are identified within a model
and matched across the two. `checks/identity.py` and `checks/register.py` measure every
statement here on real packages and write their reports under `data/reports/`.

## Vocabulary

A point of a node-breaker model is a node; a point of a bus-branch model is a bus. The
same words are used in code, tables, the viewer and these notes.

- **node**: one RAW record of the CRR model, the finest point its closed breakers
  separate. `core.node` (CRR only) has one row per node with the `bus_key` of the bus it
  belongs to; CRR branches carry `from_node` and `to_node`. The DAM model is bus-branch
  and has no nodes.
- **bus**: one electrical point. In the DAM model every RAW record is a bus
  (`bus_number`, `bus_type`). In the CRR model a bus is the nodes joined by closed
  breakers (in-service ties), `n_nodes` of them. `core.bus` has one row per bus of either
  model, keyed by `bus_key`. Buses are what the two models share, so matching
  (`core.match_bus`), settlement point weights (`core.settlement_point_bus`) and loads are
  at bus level. DAM branches carry `from_bus_number` and `to_bus_number`; every branch
  carries `from_bus_key` and `to_bus_key`.
- **substation**: the yard holding buses at several voltage levels. DAM RAW names are
  substation names; CRR RAW names are node names. `core.bus.raw_name` is the name as the
  file gives it (a CRR bus joins its nodes' names) and `core.bus.substation` is filled
  where that name is a substation (DAM). The viewer assigns CRR substations through the
  bus match (TOP-12).
- **tie**: an in-service CRR branch at or below `core.bus.TIE_REACTANCE`: a closed
  breaker, disconnect switch or jumper exported from the node-breaker model (TOP-10).

PSS/E calls every RAW record a bus, and only the raw layer keeps that word
(`raw.psse_bus`). `out.network` has one row per bus. For a CRR model with ties kept,
ERCOT's auction topology, every node is a bus of one node and `bus_key` repeats across a
tie group; with `contract_ties=True` the rows are `core.bus` (docs/out-network.md).

## What the keys in ERCOT's files are

**RAW records.** A DAM RAW name is the substation name, shared by every bus at that
substation; the CSVs' "Station Name/PSS/E Bus Name" column is the same string. CRR node
names are twelve-character electrical bus names (ERCOT's word), unique except for a few
collisions. CRR and DAM numbers are unrelated. CRR numbers hold from month to month; DAM
numbers are reassigned in every hourly model. Neither number nor name identifies a bus
across models.

**What holds in DAM**: the substation and kV reached by a generator name, a load name, a
settlement point name or a branch name, hour after hour and day after day (TOP-08).

**Lines.** CRR RAW lines carry their CRR name in the `/*[...]*/` comment, and the
contingency, monitored-element and GTC CSVs use that name. DAM lines are named in the
`Ln` CSV, keyed to the RAW by (from, to, ckt).

**Transformers.** CRR RAW transformers carry no comment. Their CRR name is in the mapping
workbook's `Autos` sheet, which reaches the RAW by (from, to, ckt) in either orientation:
a large minority of the sheet's rows list from and to swapped relative to the RAW, and
the CRR name follows the sheet's order. The CSVs use the `Autos` name, and their flow
directions refer to it; `core.branch.is_name_reversed` records the orientation (NAM-02).
CRR line names follow the RAW's order (NAM-01). DAM transformers are named in the `Xf`
CSV.

**Topology level.** The CRR RAW holds thousands of ties joining two nodes of the same
substation and voltage. They are not alternative connections; they are the switching
devices that make several nodes one bus. Most carry PSS/E's placeholder rating, some
carry a real breaker rating and appear in the monitored list, and some appear as
contingency devices. The DAM RAW holds no branch below `core.match.DAM_REACTANCE_FLOOR`:
every DAM branch that would be shorter sits at the floor, including the minority of CRR
ties the workbook names (they match a DAM branch at the floor). So CRR buses map
many-to-one onto DAM buses, a CRR breaker outage usually has no DAM counterpart, and a
reactance comparison across models raises both sides to the floor first
(`core.match.reactance_agrees`).

**Base kV.** Both RAWs sometimes give a record a base kV with a tenths digit that is not
a nominal level (138.1, 345.2), more often in DAM. It tells points of one substation
apart; it is not a voltage. Voltage levels are compared with the tenths dropped. A
`bus_key` includes the kV as written, so it differs by the tenths only within a model.

**Mapping workbook.** `Lines` maps every CRR tag (a RAW line comment) to an
`Operations_Name`; ties mostly have no row, and one placeholder operations name marks the
rows ERCOT could not map (MAT-06). A DAM branch name is the operations name followed by
DAM's own circuit designator (one or two characters), which usually equals the CRR
circuit id; punctuation differs. `Autos` operations names match DAM transformer names
verbatim for a majority.

**Contingencies.** Most CRR contingency names have a DAM contingency of the same name,
but the vocabularies differ: a CRR row names a LINE or XFMR device with one action; a
DAM row is a Branch (by key), Load, Generator or SettlementPoint row, and some rows are
split-bus operations that change topology rather than remove an element (NAM-09).

**GTCs.** The CRR package ships its GTCs (name, limit, members with factor and
direction) under short codes. The DAM package has no GTC file; the GTL workbook
(NP3-766-M) gives hourly day-ahead and real-time limits under readable names, and nothing
published links the two name sets, so the crosswalk is manual
(generic-transmission-limits.md).

**Monitored flags.** DAM `Monitored?` and `Monitored and Secured?` are the CIM branch
flags `DAM Monitored` and `DAM Secured` (defaults FALSE and TRUE). Secured means enforced:
the base case at the normal rating, contingencies at the emergency rating (RAT-08);
monitored only means reported. `checks/prices.py` counts the binding rows by these flags
(RAT-09).

**Ratings.** CRR `BaseCaseRating` is a fixed fraction of the CRR RAW's rate A on every
monitored line; `EmergencyRating` is at least the base rating. Against the DAM RAW's
rate A on branches both models enforce, the CRR base rating is a band of fractions, and
the CRR emergency rating almost never equals the DAM's rate B. Each model enforces some
branches the other does not. `core.diff_branch` records both sides of every matched
branch; the difference is a result of the comparison, not a choice (RAT-04).

**Settlement points.** Every CRR source and sink name appears among the DAM settlement
points; the DAM has more (logical and PUN resource nodes, DC-tie zones). The CRR
`BusName` is "number name", matching the RAW comment. In the CRR file a resource node is
one bus with factor one, while hubs (`HB_`) and load zones (`LZ_`) spread over many buses
with MW-scale weights. In the DAM the `Sp` file gives one bus per resource node (none for
hubs, zones and logical resource nodes), the `Hb` file gives the hub buses, and the `Ld`
file gives each load's zone and MW distribution factor. Two DAM hubs are averages of the
others (names ending `AVG`). Some DAM resource nodes have no bus in a given hour.

## Decisions

- **Bus identity is equipment-based** (`core/bus.py`, TOP-02). A `bus_key` is derived
  from the RAW name, the kV and the sorted attached branch, generator, load and
  settlement point names. PSS/E numbers are per-snapshot attributes kept for joins within
  the snapshot. Buses with identical attachment sets (isolated buses at one substation)
  get an ordinal suffix and `is_ambiguous` (TOP-07); the suffix is not stable across
  snapshots and does not need to be, because such buses are never in the solved network.
- **CRR ties are contracted for identity, not for solving** (TOP-01, TOP-09). `core.bus`
  joins the CRR nodes connected by in-service ties into one bus with one `bus_key`, which
  is what matching needs, since the DAM has already merged them. The network handed to a
  solver keeps the ties as branches by default, as ERCOT's auction does, and enforces the
  monitored ones; contraction is an option there, measured by `checks/network.py`.
- **Branch matching** (`core/match.py`, `session.match_branches`; MAT-01, MAT-02, MAT-05):
  compare names with punctuation and case removed, in this order, and record the first
  method that succeeds as `match_method`: `exact`; `ops+ckt` (the operations name followed
  by the CRR circuit id); `prefix` (exactly one DAM name is the operations name plus at
  most two characters); `prefix+x` (several DAM names fit, and exactly one not claimed by
  another CRR branch has the same reactance after the floor); else `unmatched`, with the
  number of prefix candidates. Every CRR branch is listed once; unmatched DAM branches
  are listed with a null CRR side. `checks/identity.py` counts the matched pairs whose
  reactance, kind and voltage level agree; the ones that do not are for `diff_branch`.
- **Settlement points are one table in both models** (`core/settlement_point.py`):
  `core.settlement_point` (kind from the DAM type text, or in CRR from the `HB_`, `LZ_`
  and `DC` prefixes; SP-07) and `core.settlement_point_bus` (weights normalized to sum to
  one, the raw weight kept; SP-03). DAM hubs follow Protocols 3.5.2 (SP-01), load zones
  take their in-service loads weighted by distribution factor with the `Ld` file's
  rollover rule (SP-02), and logical resource nodes take their units' buses (SP-05).
  Cross-model identity is the name; `core.diff_settlement_point` translates the CRR buses
  through the bus match and records whether the bus sets agree and how much weight they
  share.
- **Bus matching** (`session.match_buses`; MAT-03): settlement points attached to exactly
  one bus on each side vote for (CRR bus, DAM bus) pairs, and a pair is accepted when
  every point on either bus agrees (hubs and zones touch many CRR buses and are skipped).
  Then the endpoints of matched branches vote: a pair is accepted when it is the top vote
  for both buses and either has two agreeing branches or both buses have a single matched
  branch; tied votes stay unmatched. The rest is `unmatched` with the number of competing
  candidates; unmatched DAM buses are listed with a null CRR side.
- **Contingency matching** (`session.match_contingencies`; MAT-04): CRR outages are
  translated to DAM branch ids through the branch match; then by name (case and
  whitespace ignored), then by an identical translated branch set that points at exactly
  one DAM contingency. Every pair records how many branches each side outages, how many
  they share, how many DAM rows are loads, generators or settlement points (which CRR
  never lists) and whether the DAM side splits a bus. Many name matches do not share the
  same branch set; `checks/identity.py` breaks the difference down by cause. Read
  `n_shared_branches` before treating a pair as equivalent.
- **Ratings are facts** (RAT-04, RAT-06): the CRR CSV ratings and the RAW rate A, B and C
  are stored side by side in `core.branch_rating`, and `out.network` enforces each model's
  own.
- **Builds are not bit-identical.** `core.settlement_point_bus.weight` is a sum of raw
  weights divided by their total; polars sums floats in parallel, so two builds of one
  package can differ in the last bits. Nothing downstream resolves that precision.

## Open questions

- CRR branches ambiguous after `prefix+x`: every free candidate disagrees on reactance,
  or two agree. Endpoint bus matches could settle some.
- Matched branches whose reactance, kind or voltage level disagree after the floor:
  series devices, re-conductored lines, or wrong matches. `diff_branch` records them.
- Name-matched contingencies whose branch sets differ for reasons other than matching
  coverage (`checks/identity.py` isolates them).
- Hubs and load zones sit on different bus sets in the two models, because the CRR
  spreads them over far more buses than the DAM's hub-bus file and load list
  (`core.diff_settlement_point` gives the shared weight). Whether the difference is
  definition or bus resolution is open.
- DAM settlement points with no bus in the hour (de-energized resource nodes and logical
  resource nodes without a listed unit) stay unresolved.
- How to treat monitored ties in a contracted CRR model.
- The GTL workbook's GTC names against the CRR GTC codes: the crosswalk is manual;
  nothing published links the two.
