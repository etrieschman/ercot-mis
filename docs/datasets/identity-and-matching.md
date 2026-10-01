# Identity and matching across CRR and DAM models

How buses, branches, contingencies and constraints are identified within a model and
matched across models. Numbers are deliberately absent: `scripts/measure_identity.py`
measures everything stated here on real packages and writes a dated JSON report to
`data/reports/identity/`. Re-run it when a new CRR month or DAM day arrives; if a
statement below stops being true, change the code and the note together.

## Vocabulary

Three words, used the same way in code, tables, the viewer and these notes (settled with
the user on 2026-10-01; it is the node-breaker convention):

- **node**: the finest connection point a model gives, one RAW record. PSS/E calls
  every RAW record a "bus", and columns that quote the file keep its word
  (`psse_bus_number`). In a CRR model nodes are bus sections between breakers; in a
  DAM model ERCOT has already merged them, so each node is a bus on its own.
- **bus**: nodes joined by closed breakers (in-service ties), one electrical point.
  `bus_key` identifies a bus; `core.node` has one row per node with the `bus_key` of
  the bus it belongs to. Buses are what the two models have in common, so matching
  (`core.match_bus`) and settlement point weights (`core.settlement_point_bus`) are
  at this level.
- **substation**: the physical yard holding buses at several voltage levels. DAM RAW
  names are substation names; CRR RAW names are node names, so `core.node.substation`
  holds the node's own name for a CRR model (the viewer assigns CRR substations
  through the bus match).

`out.network` calls its vertices nodes: by default they are the model's nodes, and
with `contract_ties=True` each vertex is a bus (`n_members` says how many nodes).

## What the keys in ERCOT's files are

**Buses.** A DAM RAW bus name is the *substation* name, shared by every bus at that
substation; the CSVs' "Station Name/PSS/E Bus Name" column is the same string. CRR bus
names are 12-character electrical bus names, unique except for a few collisions. CRR
and DAM bus numbers are unrelated numbering schemes. CRR numbers are stable month to
month; **DAM numbers are reassigned in every hourly model**. Neither number nor name
identifies a bus across models, and in DAM the number does not identify a bus across
hours.

**What is stable in DAM**: the (substation, kV) reached by a generator name, a load
name, a settlement point name or a branch name, hour after hour and day after day.

**Lines.** CRR RAW lines carry their CRR name in the `/*[...]*/` comment, and the
contingency, monitored-element and GTC CSVs use exactly that name. DAM lines are
named in the `Ln` CSV, keyed to the RAW by (from, to, ckt).

**Transformers.** CRR RAW transformers carry **no** comment. Their CRR name lives
only in the mapping workbook's `Autos` sheet, which reaches the RAW by (from, to,
ckt) **in either orientation** (a large minority of the sheet's rows list from and to
swapped relative to the RAW, and the CRR name follows the sheet's order); the CSVs
use the `Autos` name, and their flow directions refer to it. `core.branch` records
this as `is_name_reversed`. CRR line names always follow the RAW's order. DAM
transformers are named in the `Xf` CSV.

**Topology level.** CRR RAWs hold thousands of branches at PSS/E's minimum
reactance (`x = 0.0001`, `r = 0`) joining two buses of the same substation and voltage:
closed breakers, disconnect switches and bus-section jumpers, exported from a
node-breaker model. They are not alternative connections; they are the switching
devices that make several nodes one bus. Most carry the 9999
placeholder rating, some carry a real breaker rating and appear in the monitored
list, and some appear as contingency devices (a breaker opening). DAM RAWs hold no
branch below a **reactance floor** of `x = 0.0005`: every DAM branch that would be
shorter sits at exactly the floor, including the minority of CRR ties that the mapping
workbook names (they match a DAM branch at the floor) and CRR lines with a reactance
between the CRR minimum and the floor. So CRR-to-DAM bus mapping is many-to-one, a
CRR breaker outage usually has no DAM counterpart, and a reactance comparison across
models must raise both sides to the floor first.

**Base kV.** Both RAWs occasionally give a bus a base kV with a tenths digit that is
not a nominal level (`138.1`, `345.2`), more often in DAM than in CRR. It tells buses
of one substation apart; it is not a voltage. Compare voltage levels with the tenths
dropped, and expect `bus_key`s, which include the kV as written, to differ by it
only within a model, never across hours or months.

**Mapping workbook.** `Lines` maps every `CRR_Tag` (a RAW line comment) to an
`Operations_Name`; ties mostly have no row, and a placeholder `Operations_Name` marks
rows ERCOT could not map. A DAM `Branch Name` is the operations name followed by
**DAM's own circuit designator** (one or two characters), which usually but not
always equals the CRR circuit id; punctuation differs (single versus double
underscores). `Autos` operations names match DAM transformer names verbatim for a
majority.

**Contingencies.** Most CRR contingency names have a DAM contingency of the same
name, but the vocabularies differ: CRR rows are LINE/XFMR device names with one
action; DAM rows are Branch (by key), Load, Generator and SettlementPoint rows, and
some rows are *split-bus* operations that change topology rather than remove an
element.

**GTCs.** The CRR package ships its GTCs (name, limit, members with factor and
direction) under short codes. The DAM package has no GTC file; the daily GTL workbook
(`NP3-766-M`, see generic-transmission-limits.md) gives hourly day-ahead and
real-time limits under human-readable names, and nothing links the two name sets, so
a manual crosswalk lives in `data/overrides/gtc_names.csv`. Each GTL is enforced as a
base-case constraint in CRR, DAM and real time.

**Monitored flags.** DAM `Monitored?` and `Monitored and Secured?` are the CIM branch
flags `DAM Monitored` / `DAM Secured` (NMMS modeling guidelines; defaults FALSE/TRUE,
which is why most lines are No/Yes). Working reading, to be falsified against binding
constraints in `NP4-191-CD`: *Secured* = enforced by the DAM (base case at the normal
rating, contingencies at the emergency rating); *Monitored*-only = reported, not
enforced. The authoritative mapping is `PG7-116-M` (Certified).

**Ratings.** CRR `BaseCaseRating` is a fixed fraction of the RAW rate A on every
monitored line (the report records the fraction); `EmergencyRating` is at least the
base rating with a long upper tail. Time-of-use blocks can be identical for a month.
Against the DAM RAW's rate A on branches both models enforce, the CRR base rating is
a somewhat wider band of fractions (the DAM's own rate A differs from the CRR RAW's
on some branches), and each model enforces some branches the other does not;
`core.diff_branch` lists them all.

**Settlement points.** Every CRR source/sink name appears among the DAM settlement
points (the DAM has more: logical and PUN resource nodes, DC-tie zones); CRR
`BusName` is "number name", matching the RAW bus comment, and `PriceNode` is the
electrical bus. In the CRR file a resource node is one bus with factor one, while the
hubs (`HB_`) and load zones (`LZ_`) spread over many buses with MW-scale weights. In
the DAM the `Sp` file gives one bus per resource node (none for hubs and zones, and
none for logical resource nodes, which point at a combined-cycle settlement point),
the `Hb` file gives the hub buses, and the `Ld` file gives each load's zone and MW
distribution factor (summing to one for some zones and to MW for others). Two DAM hubs
are averages of the others (names ending `AVG`). Some DAM resource nodes have no bus in
a given hour.

## Decisions

- **Bus identity is equipment-based** (`core/node.py`). A `bus_key` is derived
  from (substation, kV, attached branch/generator/load/settlement-point names); PSS/E
  numbers are per-snapshot attributes kept for joins within the snapshot only. Nodes
  with identical attachment sets (isolated nodes at one substation) get an ordinal
  suffix and `is_ambiguous`; the suffix is not stable across snapshots, and does not
  need to be, because such buses are never part of the solved network.
- **CRR ties are contracted for identity, not for solving.** `core.node` groups
  nodes joined by in-service ties (|x| at or below `TIE_REACTANCE`) under one
  `bus_key`, which is what matching needs, since the DAM has already merged them. The
  network handed to a solver keeps the ties as branches by default, as ERCOT's auction
  does, and enforces the monitored ones; contraction is an option there
  (`docs/out-network.md`, measured by `scripts/check_network.py`).
- **Branch matching** (`core/match.py`, `session.match_branches`): compare names
  with punctuation and case removed, in this order, recording the first method that
  succeeds as `match_method`: `exact`; `ops+ckt` (operations name followed by the
  CRR circuit id); `prefix` (exactly one DAM name is the operations name plus at
  most two characters); `prefix+x` (several DAM names fit, but exactly one of those
  not claimed by another CRR branch has the same reactance, both sides raised to the
  DAM floor and compared within a relative tolerance); otherwise `unmatched`, with
  the number of prefix candidates. Every CRR branch is listed once; unmatched DAM
  branches are listed with a null CRR side. Reactance is also the check on the
  name-based methods: the report counts matched pairs whose reactance, kind and
  voltage level agree, and nearly all do once the floor is allowed for; the few that
  do not are for `diff_branch`, not for the matcher to hide.
- **Settlement points are one table in both models** (`core/settlement_point.py`):
  `core.settlement_point` (kind from the DAM type or, in CRR, from the `HB_`/`LZ_`/`DC`
  prefixes) and `core.settlement_point_bus` (weights normalized to sum to one, the
  raw weight kept). DAM hubs take their `Hb` buses at equal weight, the two average
  hubs are derived (bus average: every hub bus equally; hub average: each hub
  equally, then its buses), load zones take their in-service loads weighted by LDF,
  logical resource nodes borrow their combined-cycle point's bus. Cross-model
  identity is the name; `core.diff_settlement_point` (`session.diff_settlement_points`)
  translates the CRR buses through the bus match and records whether the bus sets
  agree and how much weight they share.
- **Bus matching** (`session.match_buses`): settlement points attached to exactly
  one bus on each side vote for (CRR bus, DAM bus) pairs, and a pair is accepted
  when every point on either bus agrees (zones and hubs touch many CRR buses and are
  skipped), then
  the endpoints of matched branches by vote (a pair is accepted when it is the top
  vote for both buses and either has two agreeing branches or both buses have a
  single matched branch; tied votes stay unmatched). Then `unmatched` with the number
  of competing candidates. Unmatched DAM buses are listed with a null CRR side.
- **Contingency matching** (`session.match_contingencies`): CRR outages are
  translated to DAM branch ids through the branch match first; then by name (case and
  whitespace ignored), then by an identical translated branch set when it points at
  exactly one DAM contingency. Every pair records how many branches each side
  outages, how many they share, how many DAM rows are loads, generators or
  settlement points (which CRR never lists) and whether the DAM side splits a bus.
  Many name matches do not share the same branch set. The report breaks the
  non-shared rows down: most are matching-coverage gaps (CRR breaker outages with no
  DAM device, transformers and lines without a DAM branch match), and only a small
  remainder are matched branches that the DAM contingency genuinely omits. Trust
  each model's own contingency rows; treat the join as partial and read
  `n_shared_branches` before assuming equivalence. Every match records
  `match_method`; every unmatched record is output.
- **Ratings are facts**: store the CRR CSV ratings and RAW rate A/B/C both; the
  derate fraction is an observation the report checks, not a rule the code applies.

## Open questions

- Branches still ambiguous after `prefix+x`: every free candidate disagrees on
  reactance, or two agree. Endpoint bus matches could settle some.
- Matched branches whose reactance, kind or voltage level disagree after the floor is
  allowed for: series devices, re-conductored lines, or wrong matches? Record them
  in `diff_branch` before deciding.
- Name-matched contingencies whose branch sets differ for reasons other than
  matching coverage (the small remainder the report isolates).
- Settlement points the two models put on different buses: a small share of resource
  nodes, and every hub and load zone, since the CRR spreads hubs and zones over far
  more buses than the DAM's hub-bus file and load list (the report gives the shared
  weight). Whether the difference is definition or bus resolution is open.
- DAM settlement points with no bus in the hour (de-energized resource nodes and
  logical resource nodes without a combined-cycle point) stay unresolved.
- How to treat monitored bus ties in a contracted CRR model.
- Verify the monitored/secured reading against binding constraints in `NP4-191-CD`.
- The GTL workbook's GTC names against the CRR GTC codes: the crosswalk is manual
  (generic-transmission-limits.md); nothing published links the two.
