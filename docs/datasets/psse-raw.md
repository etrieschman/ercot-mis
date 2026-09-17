# PSS/E v30 RAW files (shared by CRR and DAM)

## What it is and why we use it
The network topology and impedances behind both markets: buses, loads, generators,
branches, transformers, switched shunts. CRR packages ship one RAW per month; DAM
packages ship one per operating hour.

## Source and capture
Inside the CRR (NP7-801-M, NP7-800-M) and DAM (NP4-500-SG) zips; never downloaded
on its own. Read straight from the archived zip.

## Package layout
- CRR: `...Common_NetworkModel_<MON>_<YYYY>_PeakWD.RAW` (annual) or
  `<YYYY>.<MON>.Monthly.Auction.NetworkModel_PeakWD.raw` (monthly).
- DAM: `DAM<mmddyyyy>_<HHH>.RAW`, one per hour.

## What we parse
`parsers/psse.py` -> `psse_case`, `psse_bus`, `psse_load`, `psse_generator`,
`psse_branch`, `psse_transformer`, `psse_area`, `psse_switched_shunt`,
`psse_impedance_correction`, `psse_zone`, `psse_owner`. Column names follow the PSS/E
manual (`i`, `j`, `ckt`, `ratea`, ...). Every table except `psse_case` ends with
`comment`.

## Decisions
- **Own parser, not a library.** No open Python parser reads ERCOT's v30 cleanly as
  tables (grg-pssedata is v33 only; VeraGridEngine parses into its own grid objects).
  The scope is bounded: one version, about eight populated sections, fixed layouts.
- **One tokenizer for both dialects**: a single-quoted string, or a run of characters
  other than blanks, commas and quotes; `/` outside quotes starts a comment. About
  0.3 s per file. Arrow's CSV reader can't split blank-separated fields where quoted
  names contain blanks, so it is used for CSVs only; Arrow still does all type casting.
- **Section names** come from `END OF <X> DATA` markers when present, otherwise from
  v30 order (16 separators, or 15 without VSC DC).
- **Quoted strings are trimmed** of PSS/E's fixed-width padding. Nothing else changes.
- **Fail loudly**: more fields than the layout, an unknown section name, a revision
  other than 30, a 3-winding transformer, or records in a section with no layout
  (DC lines, FACTS, multi-section lines) each raise `ParseError`.
- **Keep the `/*[...]*/` comment**: in CRR files it holds the element's ERCOT name,
  which is the `DeviceName` the CRR CSVs use for **lines**. Transformer records carry no
  comment; their CRR name is in the mapping workbook's `Autos` sheet, keyed by
  (from, to, ckt). See identity-and-matching.md.

## ERCOT quirks
| | CRR (PSS/ODMS export) | DAM (DAM study) |
|---|---|---|
| separators | commas | blanks |
| section markers | `0 / END OF X DATA, BEGIN Y DATA` | bare `0` |
| comments | `/*[name]*/` on most records | none |
| VSC DC section | present (empty) | absent |
| line endings | CRLF | LF |
| header | `IC, SBASE` with a comment; UTF-8 ® | `IC SBASE`; titles blank |

All transformers are 2-winding. Integers occasionally appear as `3.0`; they are
accepted when whole.

Bus numbers: CRR numbering is stable month to month; **DAM numbering is reassigned in
every hourly model** and unrelated to CRR's. DAM bus names are station names shared by
several buses. CRR has thousands of zero-impedance branches (bus ties); DAM has none.

## Validation
`scripts/validate_parsers.py` checks parsed records against data lines per section
(four lines per transformer) on every archived package.

## Open questions
- Cross-check against PowerFlowData.jl (needs Julia).
- ERCOT's reference/slack convention for CRR models (the DAM README names its slack bus).
