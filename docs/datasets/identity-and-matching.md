# Identity and matching across CRR and DAM models

What the keys in ERCOT's files actually are, measured on archived packages with
`scripts/probe_keys.py` (counts and masked patterns only). This note governs how the
core layer identifies buses, branches, contingencies and constraints, and how CRR and
DAM models are matched. Re-run the probe when a new month arrives and update the
numbers here in the same commit.

Measured 2026-09-17 on the 2026-09 monthly CRR model and DAM 2026-09-15 hour 12.

## Buses

| fact | CRR | DAM |
|---|---|---|
| buses in the RAW | 10,998 | 10,311 |
| distinct RAW bus names | 10,720 | 5,781 |
| distinct (name, kV) | 10,720 | 7,749 |
| buses sharing a (name, kV) with another | 278 | 4,426 (up to 16 per group) |
| isolated buses (`ide = 4`) | 378 | 934 |
| RAW bus numbers shared between the two | 4,645, of which **0** carry the same name | |
| bus numbers stable across snapshots | yes (10,708 of 10,717 shared numbers keep their name month to month) | **no** (renumbered every hour: 1,153 of 5,885 uniquely named buses keep their number from one hour to the next, 41 across a month) |

- A DAM RAW bus name is the **station** name, shared by every bus at that station.
  The CSVs' "Station Name/PSS/E Bus Name" column equals it in every row.
- CRR bus names are 12-character electrical bus names, unique except for 278.
- **Consequence:** neither the PSS/E number nor the name identifies a bus across
  models, and in DAM the number does not even identify a bus across hours.
- What *is* stable in DAM, hour to hour and day to day: the (station, kV) reached by a
  generator name (1,612 of 1,612), a load name (7,759 of 7,759), a settlement point
  (1,036 of 1,036) and a branch name (8,636 of 8,636). Node identity in DAM is
  therefore derived from attached equipment names, not from the RAW.

## Branches

- CRR RAW lines carry their CRR name in the `/*[...]*/` comment (10,896 distinct). CRR
  RAW **transformers carry no comment**; their CRR name lives only in the mapping
  workbook's `Autos` sheet, which reaches the RAW by (from, to, ckt) for 2,736 of 2,820.
- The CSVs use those two vocabularies exactly: contingency, monitored and GTC line
  names match RAW line comments 100%; transformer names match `Autos` 100%.
- CRR has 2,860 branches with `x <= 1e-4` (bus ties, breakers), 1,232 of them
  monitored. DAM has none: the DAM model is bus-branch, CRR is closer to node-breaker.
  A CRR-to-DAM bus mapping is many-to-one after contracting bus ties.
- Mapping workbook `Lines`: 8,646 rows, every `CRR_Tag` a RAW line comment; 2,250 RAW
  lines have no row (mostly ties). 351 rows share one placeholder `Operations_Name`.
- `Operations_Name` vs DAM `Branch Name`: **0 exact**, 2,217 equal after dropping
  punctuation, and 6,037 more are a prefix of a DAM name (DAM appends a suffix).
  `Autos` `Operations_Name` matches DAM transformer names exactly for 1,518 of 2,737.
- DAM CSV (from, to, ckt) keys match the hour's RAW 100% for lines and transformers.
  CRR and DAM RAW (from, to, ckt) keys coincide 51 times: numbers are unrelated.

## Contingencies and constraints

- Names: 6,162 of 7,391 CRR contingencies have a DAM contingency of the same name.
- Vocabularies differ. CRR rows are LINE/XFMR device names with one action. DAM rows are
  Branch (10,602 keys, all in the hour's line/transformer CSVs), Load (5,546), Generator
  (1,302) and SettlementPoint (625) rows, and 283 rows are **split-bus** operations that
  change topology rather than remove an element.
- GTCs: the CRR package ships 25 (106 member rows, all members resolved). **The DAM
  package has no GTC file.** Per Nodal Protocols 3.10.7.6, GTC definitions and the
  day-ahead limits are posted to the MIS Secure Area as ECEII: `NP3-766-M` Generic
  Transmission Limits (daily xls, 31-day window, ~93 documents listed 2026-09-17) and
  `NP3-770-M` GTC Methodology (per-GTC studies and default limits). Each GTL is enforced
  as a **base-case** constraint in CRR, DAM and real time, with one DAM limit per
  operating day. Both products are now pulled; `NP6-6-CD` (real-time NSA active
  constraints, 5-minute) is tracked. Public `NP4-191-CD` gives only binding names and
  limits.
- DAM `Monitored?` / `Monitored and Secured?` combinations in one hour: 8,496 No/Yes,
  125 Yes/No, 7 Yes/Yes, 8 No/No for lines; 1,795 Yes/No and 1,025 No/Yes for
  transformers. These are the CIM branch flags `DAM Monitored` / `DAM Secured` from
  the NMMS modeling guidelines (defaults FALSE/TRUE, matching the 98% No/Yes). Working
  reading, to be falsified against `NP4-191-CD`: *Secured* = enforced by the DAM
  (base case at the normal rating, contingencies at the emergency rating);
  *Monitored*-only = flows reported, not enforced. The authoritative mapping is
  `PG7-116-M` (Certified).

## Ratings

- CRR `BaseCaseRating` = **0.90 × RAW rate A** for all 8,817 monitored lines (quantiles
  0.898 to 0.902). `EmergencyRating` median = rate A, 95th percentile 1.16 × rate A, with
  a handful of outliers above 100 ×.
- Time-of-use ratings were identical across PeakWD, PeakWE and Off-peak for all 9,839
  monitored devices this month.
- DAM RAW rate A is 0 for 25 lines; 375 lines and 146 transformers are out of service.

## Settlement points: the reliable bridge

All 996 CRR source/sink names appear among the 1,123 DAM settlement points. CRR
source/sink `BusName` is "number name", matching the RAW bus comment (4,915 distinct);
981 names have weights summing to 1, 15 (zones and hubs) carry MW-scale weights. DAM
settlement points resolve to a RAW bus for 770 of 1,123 (87 have no bus in this hour);
hubs resolve for 227 of 227.

## Decisions

- **Node identity is equipment-based.** A `node_key` is derived from (station, kV,
  attached branch/generator/load/settlement-point names); PSS/E numbers are per-snapshot
  attributes kept for joins within the snapshot only.
- **CRR bus ties are contracted** before any PTDF; the contraction is recorded
  (`core.bus_tie`) so monitored ties can be reported, not silently dropped.
- **Matching order**: settlement points and generator/load names first, then branches
  by workbook name with the documented normalization (punctuation-free, then prefix),
  then buses through matched branch endpoints, then contingencies by name with member
  sets compared in the matched vocabulary. Every match records `match_method`; every
  unmatched record is output.
- **Ratings are facts**: store the CRR CSV ratings and RAW rate A/B/C both; the 0.90
  derate is an observation to check monthly, not a rule to apply.

## Node keys in practice (`core/identity.py`)

Equipment-based keys measured on the same snapshots:

| comparison | by PSS/E number | by node key |
|---|---|---|
| DAM hour 12 vs hour 13 (10,311 buses) | 3,942 same bus | 10,309 shared keys, 2 + 1 unmatched |
| DAM 2026-09-15 vs 2026-08-15 | 319 | 9,937 shared, 374 + 304 unmatched |
| CRR 2026-09 vs 2026-10 after contraction | | 8,605 of 9,223 / 9,263 nodes shared |

CRR contraction: 1,250 tie groups covering 3,025 buses (largest 14), 10,998 buses
become 9,223 nodes. Ambiguous keys (identical attachment sets, usually isolated buses
at one station) get an ordinal suffix: 8 in DAM, 8 in CRR.

Over the whole archive (`build_core`, 2026-09-17): 930 snapshots (816 DAM hours over
34 days, 12 monthly and 102 annual CRR months), 9.6 M node rows, 77 MB. The 816 DAM
hours use 12,570 distinct node keys, 8,972 of which appear in every hour; the rest
come and go with outages and topology changes. Raw Parquet: 527 MB for 34 DAM days
(15.5 MB a day), 116 MB for all 29 CRR packages.

## Open questions

- The suffix rule relating `Operations_Name` to DAM `Branch Name`.
- How to treat the 1,232 monitored bus ties in a contracted CRR model.
- Verify the monitored/secured reading against binding constraints in `NP4-191-CD`.
- Parse `NP3-766-M` (xls) and check its GTC names against the CRR GTC names.
