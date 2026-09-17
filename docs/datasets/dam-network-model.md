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
`parsers/dam.py`: `dam_contingencies`, `dam_generators`, `dam_hub_buses`, `dam_loads`,
`dam_lines`, `dam_settlement_points`, `dam_transformers`,
`dam_settlement_point_contingencies`, `dam_non_biddable_resource_nodes`, plus the
`psse_*` tables from each RAW. Archived, not parsed: the README.

## Decisions
- **Snapshot per hour**: `dam:<date>:he<HH>:r<n>`, because each hour is its own model.
- **Column names**: ERCOT's long human headers, snake_cased; a few very long or
  misspelled ones get explicit names (`equipment_type`, `contingency_operation`,
  `split_bus_*`, `number_of_energized_components`, `load_zone`, `dc_tie`,
  `resource_node_settlement_point_name`).
- **No blob dedup, but semantic dedup**: no two hourly files are byte-identical, partly
  because PSS/E bus numbers are reassigned in every hourly model. The equipment maps
  (generator, load, settlement point and branch name to station and kV) are identical
  across hours, so the core layer keeps one element dictionary per day plus hourly state,
  while raw stays one table per file.

## ERCOT quirks
- The RAW is the blank-separated dialect with no VSC DC section (see psse-raw.md).
- The README states: lossless DC power flow; RAW includes scheduled outages; bus shunt,
  load and generator MW/MVAr in the RAW are all zero; it names the slack bus; 23 or 25
  hourly files on DST days, the extra hour being the third.
- The load CSV header ends with a trailing comma its rows lack.
- Split-bus bus-number columns in the contingency CSV sometimes hold a note instead of a
  number; kept as text.
- Headers have spaces after commas; values are trimmed.
- **Bus numbers change every hour** and are unrelated to CRR numbering; RAW bus names are
  station names (see identity-and-matching.md).
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
