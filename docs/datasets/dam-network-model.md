# DAM Network Model (NP4-500-SG)

## What it is and why we use it
The network the Day-Ahead Market cleared on: the DAM model `g` in `ftr_align`. One
PSS/E model per operating hour plus CSVs mapping ERCOT's CIM model onto each RAW
(station names, branch names, monitored flags, settlement points, hubs, load
distribution, contingency definitions).

## Source and capture
EWS, ECEII. Report type 13070. Display window 31 days and **no archive beyond it**, so
every day is captured daily by `scripts/daily_pull.py` (decision: capture every day for
now). One package per operating day, posted the day before; `scripts/probe.py` reports
sizes.

## Package layout
Flat. Per hour `HHH` (`001`-`024`): `DAM<mmddyyyy>_<HHH>.RAW` and
`DAM<mmddyyyy>_<Kind>_<HHH>.csv` for `Ctg`, `Gn`, `Hb`, `Ld`, `Ln`, `Sp`, `Xf`. Once per
day: `DAM<mmddyyyy>_SpCtg.csv`, `DAM<mmddyyyy>_SpNb.csv`, `README_DAM<mmddyyyy>.txt`.

## What we parse
`raw/dam.py`: `dam_contingencies`, `dam_generators`, `dam_hub_buses`, `dam_loads`,
`dam_lines`, `dam_settlement_points`, `dam_transformers`,
`dam_settlement_point_contingencies`, `dam_non_biddable_resource_nodes`, plus the
`psse_*` tables from each RAW. Archived, not parsed: the README.

## Decisions
- **Snapshot per hour**: `dam:<date>:he<HH>:r<n>`, because each hour is its own model.
- **Column names**: ERCOT's long human headers, snake_cased; a few very long or
  misspelled ones get explicit names (`equipment_type`, `contingency_operation`,
  `split_bus_*`, `number_of_energized_components`, `load_zone`, `dc_tie`,
  `resource_node_settlement_point_name`).
- **No blob dedup; identity by equipment**: no two hourly files are byte-identical,
  because PSS/E bus numbers are reassigned in every hourly model. The equipment maps
  (generator, load, settlement point and branch name to station and kV) are identical
  across hours, so the core layer keys every hour's buses by the equipment attached to
  them (`node_key`, see identity-and-matching.md) and every hour's branches by name;
  raw stays one table per file, and core stays one row per hour with keys that agree
  across hours rather than a per-day dictionary.

## ERCOT quirks
- The RAW is the blank-separated dialect with no VSC DC section (see psse-raw.md).
- The README states: lossless DC power flow; the RAW already incorporates the outage
  scheduler (a branch with status OUT is either normally open or on outage); bus shunt,
  load and generator MW/MVAr in the RAW are all zero; it names the MMS-DAM slack bus
  (the RAW's type-3 bus); a blank or "De-energized" settlement point status means
  de-energized; 23 or 25 hourly files on DST days, the extra hour being the third.
- **Contingency files differ by hour** (a few names come and go across a day), so
  each hour is its own snapshot with its own contingency set.
- `SpCtg` lists, for all hours, the single-bus settlement points a contingency
  disconnects: which resource nodes a contingency islands. Parsed
  (`dam_settlement_point_contingencies`), not yet used by core.
- The load CSV header ends with a trailing comma its rows lack.
- A split-bus contingency row gives two split-bus columns: the end of the branch that
  stays holds its bus number, the end that moves to the new bus section holds a note
  instead of a number (kept as text). `scripts/check_prices.py` applies the move;
  core only flags it (`has_split_bus`).
- Headers have spaces after commas; values are trimmed.
- **Bus numbers change every hour** and are unrelated to CRR numbering; RAW bus names are
  station names (see identity-and-matching.md).
- **Reactance floor**: no branch has |x| below 0.0005 pu; a sizeable set sits at exactly
  that value (breakers and short lines the CRR model carries at 0.0001 or less). A few
  reactances are negative (series capacitors).
- **Base kV with a tenths digit** (138.1, 345.2) distinguishes buses at one station; it
  is not a voltage. Drop the tenths to compare voltage levels.
- No GTC file: DAM GTC definitions and daily limits come from NP3-770-M and NP3-766-M.
- `Monitored?` and `Monitored and Secured?` are the CIM `DAM Monitored`/`DAM Secured` flags
  (defaults FALSE/TRUE); see identity-and-matching.md for the working reading.

## Validation
`scripts/validate_parsers.py`: every hourly model parses, and in every hour the RAW's
branch, transformer, load and generator counts equal the `Ln`, `Xf`, `Ld`, `Gn` CSV
row counts.

## Open questions
- Hour numbering and timestamps on DST days (verify on 2026-11-01, the long day).
- Whether `Ld` raw LDFs match NP4-159-CD load distribution factors.
