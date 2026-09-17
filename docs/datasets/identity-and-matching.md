# Identity and matching across CRR and DAM models

How buses, branches, contingencies and constraints are identified within a model and
matched across models. Numbers are deliberately absent: `scripts/measure_identity.py`
measures everything stated here on real packages and writes a dated JSON report to
`data/reports/identity/`. Re-run it when a new CRR month or DAM day arrives; if a
statement below stops being true, change the code and the note together.

## What the keys in ERCOT's files are

**Buses.** A DAM RAW bus name is the *station* name, shared by every bus at that
station; the CSVs' "Station Name/PSS/E Bus Name" column is the same string. CRR bus
names are 12-character electrical bus names, unique except for a few collisions. CRR
and DAM bus numbers are unrelated numbering schemes. CRR numbers are stable month to
month; **DAM numbers are reassigned in every hourly model**. Neither number nor name
identifies a bus across models, and in DAM the number does not identify a bus across
hours.

**What is stable in DAM**: the (station, kV) reached by a generator name, a load
name, a settlement point name or a branch name, hour after hour and day after day.

**Lines.** CRR RAW lines carry their CRR name in the `/*[...]*/` comment, and the
contingency, monitored-element and GTC CSVs use exactly that name. DAM lines are
named in the `Ln` CSV, keyed to the RAW by (from, to, ckt).

**Transformers.** CRR RAW transformers carry **no** comment. Their CRR name lives
only in the mapping workbook's `Autos` sheet, which reaches the RAW by (from, to,
ckt) **in either orientation** (about half the sheet's rows list from and to swapped
relative to the RAW); the CSVs use the `Autos` name. DAM transformers are named in
the `Xf` CSV.

**Topology level.** CRR RAWs hold thousands of zero-impedance branches (bus ties,
breakers, jumpers), a share of them monitored; DAM RAWs hold none. CRR is closer to
node-breaker, DAM is bus-branch, so a CRR-to-DAM bus mapping is many-to-one.

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
direction). The DAM package has no GTC file. Per Nodal Protocols 3.10.7.6 the
definitions and day-ahead limits are ECEII products: `NP3-766-M` Generic Transmission
Limits (daily) and `NP3-770-M` GTC Methodology. Each GTL is enforced as a base-case
constraint in CRR, DAM and real time, one DAM limit per operating day.

**Monitored flags.** DAM `Monitored?` and `Monitored and Secured?` are the CIM branch
flags `DAM Monitored` / `DAM Secured` (NMMS modeling guidelines; defaults FALSE/TRUE,
which is why most lines are No/Yes). Working reading, to be falsified against binding
constraints in `NP4-191-CD`: *Secured* = enforced by the DAM (base case at the normal
rating, contingencies at the emergency rating); *Monitored*-only = reported, not
enforced. The authoritative mapping is `PG7-116-M` (Certified).

**Ratings.** CRR `BaseCaseRating` is a fixed fraction of the RAW rate A on every
monitored line (the report records the fraction); `EmergencyRating` is at least the
base rating with a long upper tail. Time-of-use blocks can be identical for a month.

**Settlement points.** Every CRR source/sink name appears among the DAM settlement
points; CRR `BusName` is "number name", matching the RAW bus comment. A minority of
source/sink names (zones, hubs) carry MW-scale weights instead of fractions summing
to one. Some DAM settlement points have no bus in a given hour.

## Decisions

- **Node identity is equipment-based** (`core/node.py`). A `node_key` is derived
  from (station, kV, attached branch/generator/load/settlement-point names); PSS/E
  numbers are per-snapshot attributes kept for joins within the snapshot only. Buses
  with identical attachment sets (isolated buses at one station) get an ordinal
  suffix and `is_ambiguous`.
- **CRR bus ties are contracted** (in-service branches with |x| at or below
  `TIE_REACTANCE`) before any PTDF; members and groups are recorded in `core.node`
  so monitored ties can be reported, not silently dropped.
- **Branch matching** (`core/match.py`, `session.match_branches`): compare names
  with punctuation and case removed, in this order, recording the first method that
  succeeds as `match_method`: `exact`; `ops+ckt` (operations name followed by the
  CRR circuit id); `prefix` (exactly one DAM name is the operations name plus at
  most two characters); otherwise `unmatched`, with the number of candidates. Every
  CRR branch is listed once; unmatched DAM branches are listed with a null CRR side.
- **Remaining matching order**: settlement points and generator/load names for
  nodes, then buses through matched branch endpoints, then contingencies by name
  with member sets compared in the matched vocabulary. Every match records
  `match_method`; every unmatched record is output.
- **Ratings are facts**: store the CRR CSV ratings and RAW rate A/B/C both; the
  derate fraction is an observation the report checks, not a rule the code applies.

## Open questions

- Disambiguating branches with several DAM circuit candidates (parallel circuits
  where the CRR and DAM circuit ids disagree).
- How to treat monitored bus ties in a contracted CRR model.
- Verify the monitored/secured reading against binding constraints in `NP4-191-CD`.
- Parse `NP3-766-M` (xls) and check its GTC names against the CRR GTC names.
