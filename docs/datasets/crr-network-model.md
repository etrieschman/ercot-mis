# CRR Network Model (NP7-801-M annual, NP7-800-M monthly)

## What it is and why we use it
The network ERCOT uses to clear CRR auctions: the FTR model `f` in `ftr_align`.
Topology (PSS/E RAW), enforced ratings by time of use, contingencies, generic
transmission constraints (GTCs), source/sink definitions, and a workbook mapping CRR
element names to Network Operations Model names (the key for CRR <-> DAM matching).

## Source and capture
EWS, ECEII. Report types 11204 (annual) and 11205 (monthly). Display window 365 days
and **no archive beyond it**. Annual: ~17 documents a year (six sequences per auction
plus `_Upd` revisions), ~43 MB zipped each. Monthly: 12 a year, ~20 MB.
Captured by `scripts/daily_pull.py`.

## Package layout
- **Annual**: one folder per month of the term,
  `<term>.AnnualAuction.Seq<n>.<MON>[_Upd]/<term>.AnnualAuction.Seq<n>.Common_<Kind>_<MON>_<YYYY>[...].<ext>`,
  plus package-level `Mapping_Documents/`, `Oneline_Diagrams/`, `Outages/`,
  `DynamicRatings.xlsx`, `Station_OneLines.zip`. Six months per package.
- **Monthly**: flat, `<YYYY>.<MON>.Monthly.Auction.<Kind>.<ext>`.
- Kinds: `NetworkModel` (RAW, PeakWD only), `Contingencies`, `MonitoredLinesAndTransformers`,
  `Non-ThermalConstraints`, `SourcesAndSinks` (each as CSV and XML), `MappingDocument`
  (xlsx: `Lines`, `Autos`), `Outages`, `DynamicRatings`, one-line diagrams (KML, zip).

## What we parse
`parsers/crr.py`: `crr_contingencies`, `crr_monitored_lines_and_transformers`,
`crr_non_thermal_constraints`, `crr_sources_and_sinks`, `crr_mapping_lines`,
`crr_mapping_autos`, `crr_outages`, plus the `psse_*` tables from the RAW.
Archived, not parsed: the XML twins (CSV is canonical), one-line diagrams (images),
DynamicRatings (small; semantics not yet reviewed).

## Decisions
- **CSV over XML.** Checked on a full monthly package: identical row counts and values;
  XML only adds nesting the CSV encodes as repeated names.
- **Member metadata from paths**: auction type, term, sequence, month and TOU are parsed
  from member names (`classify_member`); the document's revision comes from the catalog.
- **Exact header checks**: a changed or reordered header raises `ParseError`.
- **Mapping-workbook bus numbers stay text** (see quirks).

## ERCOT quirks
- Device-type spelling differs by file: contingency CSV `LINE`/`XFMR`; monitored CSV
  `Line`/`XFMR`; GTC CSV and all XML `Line`/`Transformer`. Normalize in core.
- GTC CSV header has spaces after commas.
- About 15 of ~1,000 sources/sinks (load zones, hubs) carry MW-scale weights instead
  of fractions summing to 1. Normalize in core.
- Mapping-workbook `From #`/`To #` cells hold a text placeholder for unmatched rows.
- Monthly outages are pipe-delimited with 28 columns; annual `_None` outage files are a
  comma-separated header with no rows.
- Only a PeakWD RAW ships, while ratings carry PeakWD, PeakWE and Off-peak (identical
  across the three blocks in 2026-09).
- `BaseCaseRating` is 0.90 × the RAW rate A for every monitored line (2026-09).
- Transformer names in the contingency, monitored and GTC CSVs are the `Autos` sheet's
  `CRR Name`, not anything in the RAW; lines use the RAW comment.
- `Operations_Name` never equals a DAM branch name exactly; see identity-and-matching.md.
- 351 `Lines` rows share one placeholder `Operations_Name`; 2,250 RAW lines (mostly
  zero-impedance ties) have no workbook row.
- In `_Upd` annual packages, month folders and some file names gain `_Upd`/`_Upd<n>`.

## Validation
All 29 archived packages (17 annual, 12 monthly) parse with no problems, 2026-09-15:
every member kind recognized, RAW record counts consistent.

## Open questions
- DynamicRatings: parse, and how it modifies monitored ratings.
- How annual `_Upd` revisions relate to their originals (which members change).
- Snapshot ID rules for revisions (planned: order by posting time within a logical package).
