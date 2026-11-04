# PSS/E v30 RAW files (inside the CRR and DAM packages)

## What it is and why we use it

The RAW file is the network behind each market: the points PSS/E calls buses, with
their loads, generators, branches, transformers and switched shunts. A CRR package
ships one RAW per month; a DAM package ships one per operating hour. In the CRR RAW a
record is a node (a point of a node-breaker model); in the DAM RAW a record is a bus (a
point of a bus-branch model). The raw layer keeps PSS/E's word for both.

## Source and capture

The RAW arrives inside the CRR (NP7-801-M, NP7-800-M) and DAM (NP4-500-SG) zips. It
is read from the archived zip, never downloaded on its own.

## Package layout

- CRR: one RAW per month folder (annual) or one per package (monthly), PeakWD only.
- DAM: one RAW per hour, `DAM<mmddyyyy>_<HHH>.RAW`.

## What we parse

`raw/psse.py` writes `psse_case`, `psse_bus`, `psse_load`, `psse_generator`,
`psse_branch`, `psse_transformer`, `psse_area`, `psse_switched_shunt`,
`psse_impedance_correction`, `psse_zone` and `psse_owner`. Column names follow the
PSS/E manual (`i`, `j`, `ckt`, `ratea`). Every table except `psse_case` ends with
`comment`.

## Decisions

- **Our own parser.** No Python library reads ERCOT's version 30 as tables. The scope
  is one version, about eight populated sections and fixed layouts.
- **One tokenizer for both dialects.** A token is a single-quoted string or a run of
  characters other than blanks, commas and quotes; `/` outside quotes starts a comment.
  Arrow's CSV reader cannot split blank-separated fields whose quoted names contain
  blanks, so Arrow casts types and does not tokenize.
- **Section names** come from the `END OF <X> DATA` markers when the file has them,
  otherwise from the version 30 section order.
- **Quoted strings are trimmed** of PSS/E's fixed-width padding. Nothing else is changed.
- **A file the layout does not cover fails the parse.** More fields than the layout, an
  unknown section name, a revision other than 30, a three-winding transformer, and
  records in a section with no layout (DC lines, FACTS devices, multi-section lines)
  each raise `ParseError`.
- **The `/*[...]*/` comment is kept.** In CRR files it holds the element's ERCOT name,
  which is the device name the CRR CSVs use for lines. Transformer records carry no
  comment; their CRR name is in the mapping workbook's `Autos` sheet, keyed by
  (from, to, ckt). See identity-and-matching.md.

## ERCOT quirks

| | CRR (PSS/ODMS export) | DAM (DAM study) |
|---|---|---|
| separators | commas | blanks |
| section markers | `0 / END OF X DATA, BEGIN Y DATA` | bare `0` |
| comments | `/*[name]*/` on most records | none |
| VSC DC section | present, empty | absent |
| line endings | CRLF | LF |
| header | `IC, SBASE` with a comment | `IC SBASE`, titles blank |

Every transformer is two-winding. An integer field sometimes reads `3.0`; it is accepted
when whole.

CRR node numbers hold from month to month. DAM bus numbers are reassigned in every hourly
model and are unrelated to CRR's. A DAM RAW name is a substation name shared by several
buses. The CRR RAW holds thousands of branches at PSS/E's minimum reactance (ties:
closed breakers and switches); the DAM RAW holds no branch below its reactance floor
(`core.match.DAM_REACTANCE_FLOOR`).

## Validation

`scripts/validate_parsers.py` checks the record count of every section against the data
lines (four lines per transformer) on every archived package.

## Open questions

- A cross-check against PowerFlowData.jl needs Julia.
- ERCOT's slack convention for CRR models. The DAM README names its slack bus; the CRR
  RAW marks one swing node per island (TOP-06).
