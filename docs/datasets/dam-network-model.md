# DAM Network Model (NP4-500-SG)

## What it is and why we use it

The network the Day-Ahead Market cleared on. A package holds one PSS/E model per
operating hour and the CSVs that map ERCOT's CIM model onto each RAW: substation names,
branch names, monitored flags, settlement points, hub buses, load distribution factors
and contingency definitions. Each RAW record is a bus.

## Source and capture

EWS, ECEII. Report type 13070. The display window is 31 days and EWS keeps nothing
older, so `scripts/daily_pull.py` fetches every day. One package per operating day,
posted the day before.

## Package layout

Flat. Per hour `HHH` (`001` to `024`; `023` on the short DST day, `025` on the long one,
where the extra hour is the third): a RAW and CSVs for `Ctg` (contingencies), `Gn`
(generators), `Hb` (hub buses), `Ld` (loads), `Ln` (lines), `Sp` (settlement points) and
`Xf` (transformers). Once per day: `SpCtg` (the settlement points a contingency
disconnects), `SpNb` (non-biddable resource nodes) and a README.

## What we parse

`raw/dam.py` writes `dam_contingencies`, `dam_generators`, `dam_hub_buses`, `dam_loads`,
`dam_lines`, `dam_settlement_points`, `dam_transformers`,
`dam_settlement_point_contingencies` and `dam_non_biddable_resource_nodes`, and
`raw/psse.py` writes the `psse_*` tables from each RAW. The README is archived and not
parsed.

## Decisions

- **One snapshot per hour**, `dam:<date>:he<HH>:r<n>`, because each hour is its own
  model with its own contingency set.
- **Column names** are ERCOT's headers in snake case; a few long or misspelt headers get
  explicit names in the column specs.
- **Identity by equipment.** PSS/E numbers are reassigned in every hourly model, so no
  two hourly files are byte-identical and a bus number does not identify a bus across
  hours. The equipment attached to a bus (generator, load, settlement point and branch
  names, each resolving to a substation and kV) is the same from hour to hour, so core
  keys every bus by it (`bus_key`, identity-and-matching.md) and every branch by name.
  Raw stays one table per file; core stays one row per hour with keys that agree across
  hours.

## ERCOT quirks

- The RAW is the blank-separated dialect without a VSC DC section (psse-raw.md).
- The README states that the DAM solves a lossless DC power flow, that the RAW already
  includes the outage scheduler (a branch with status OUT is normally open or on outage),
  that bus shunt, load and generator MW and MVAr in the RAW are zero, that the MMS-DAM
  slack is the RAW's type 3 bus, and that a blank or "De-energized" settlement point
  status means de-energized.
- **Contingency files differ by hour**: a few names come and go across a day.
- `SpCtg` lists, for all hours, the single-bus settlement points each contingency
  disconnects. `checks/prices.py` compares it with the points our solve leaves without a
  connected bus (NAM-07).
- The load CSV header ends with a trailing comma its rows lack.
- **Logical resource nodes (combined-cycle trains) have no bus in `Sp`** and no
  combined-cycle settlement point either, although the column exists. The `Gn` file
  places them: every unit of a train names the train's logical resource node and its own
  resource node and bus. Protocols 4.6.1.4 weighs the units by their High Reasonability
  Limit from Resource Registration, which no file we pull carries, so core gives each
  in-service unit an equal share (SP-05). On one bus the weights do not matter;
  `checks/prices.py` reports the two cases apart. A train with no unit listed in an hour
  is one `Sp` marks de-energized.
- A split-bus contingency row has two split-bus columns: the end of the branch that
  stays holds its bus number, the end that moves to the new bus section holds a note
  instead of a number (kept as text). `core.contingency_outage.split_end` says which end
  moves, `out.network` lists the moves per contingency, and `out.shift_factors` applies
  them when it solves that contingency (NAM-09).
- Headers have spaces after commas; values are trimmed.
- **Reactance floor**: no branch has |x| below `core.match.DAM_REACTANCE_FLOOR`, and a
  sizeable set sits at exactly that value (breakers and short lines the CRR model carries
  at or below its tie reactance). A few reactances are negative (series capacitors).
- **Base kV with a tenths digit** (138.1, 345.2) tells buses of one substation apart; it
  is not a voltage. Drop the tenths to compare voltage levels.
- The package has no GTC file. DAM GTC limits come from NP3-766-M and the member
  definitions from NP3-770-M (generic-transmission-limits.md).
- `Monitored?` and `Monitored and Secured?` are the CIM `DAM Monitored` and `DAM Secured`
  flags (defaults FALSE and TRUE). Secured means enforced; `out.network` limits secured
  branches (RAT-08).

## Validation

`scripts/validate_parsers.py` checks that every hourly model parses and that the RAW's
branch, transformer, load and generator counts equal the `Ln`, `Xf`, `Ld` and `Gn` row
counts in every hour.

## Open questions

- Whether the `Ld` file's distribution factors match the published load distribution
  factors (NP4-159-CD) in every hour. `checks/ldf.py` compares one hour at a time (SP-02).
