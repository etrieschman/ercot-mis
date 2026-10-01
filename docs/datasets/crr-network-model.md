# CRR Network Model (NP7-801-M annual, NP7-800-M monthly)

## What it is and why we use it
The network ERCOT uses to clear CRR auctions: the FTR model `f` in `ftr_align`.
Topology (PSS/E RAW), enforced ratings by time of use, contingencies, generic
transmission constraints (GTCs), source/sink definitions, and a workbook mapping CRR
element names to Network Operations Model names (the key for CRR <-> DAM matching).

## Source and capture
EWS, ECEII. Report types 11204 (annual) and 11205 (monthly). Display window one year
and **no archive beyond it**. Annual: several documents a year (six sequences per
auction plus `_Upd` revisions); monthly: one a month. Captured by `scripts/daily_pull.py`.

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
`raw/crr.py`: `crr_contingencies`, `crr_monitored_lines_and_transformers`,
`crr_non_thermal_constraints`, `crr_sources_and_sinks`, `crr_mapping_lines`,
`crr_mapping_autos`, `crr_outages`, plus the `psse_*` tables from the RAW.
Archived, not parsed: the XML twins (CSV is canonical), one-line diagrams (images),
DynamicRatings (one small sheet of monthly ambient-temperature assumptions behind the
dynamic line ratings; the monitored CSV already carries the resulting ratings).

## Decisions
- **CSV over XML.** Checked on a full monthly package: identical row counts and values;
  XML only adds nesting the CSV encodes as repeated names.
- **Member metadata from paths**: auction type, term, sequence, month and TOU are parsed
  from member names (`classify_member`); the document's revision comes from the catalog.
- **Exact header checks**: a changed or reordered header raises `ParseError`.
- **Mapping-workbook node numbers stay text** (see quirks).
- **The Outages file is informational.** The RAW already reflects the outage
  scheduler: every outage whose equipment name matches a workbook operations name is
  out of service in the RAW (`scripts/measure_identity.py` does not measure this yet;
  checked by hand on one month, see docs/assumptions.md). Core does not apply it.
- **Transformer name orientation is recorded**: `core.branch.is_name_reversed` is
  true when the `Autos` name lists the RAW's ends the other way round, so a CSV flow
  direction, which refers to the name, can be turned into the RAW orientation.

## ERCOT quirks
- Device-type spelling differs by file: contingency CSV `LINE`/`XFMR`; monitored CSV
  `Line`/`XFMR`; GTC CSV and all XML `Line`/`Transformer`. Normalize in core.
- GTC CSV header has spaces after commas.
- **Temporary topology is labeled.** The mapping workbook's operations equipment code
  is a name for standing equipment and a sentence for branches ERCOT added as temporary
  split-bus topology for outages. `core.branch.is_temporary` carries it. The ties that
  join such a temporary node to the standing one have no workbook row of their own, so a
  temporary node is one a temporary branch touches.
- **Switch outages cannot be tied to RAW ties by name.** The Outages file lists
  breakers and disconnects by substation and device name with their normal and outage
  state; the RAW names a tie only by its two nodes, and the workbook gives an
  operations name to a minority of ties, none of them a device name from the Outages
  file. Why a tie is open can be told per substation (does the substation have a switch
  outage in the model's month), not per device.
- The RAW names a load only by its node (the comment is node number, node name, load id,
  owner), so `core.load` builds `load_id` from the node name and the load id. A few
  annual RAWs repeat a load record (same node and id, sometimes a different MW); core
  keeps both rows, so `load_id` is not strictly unique there.
- Loads out of service keep their MW in the CRR RAW; the DAM RAW zeroes them.
- A few sources/sinks (load zones, hubs) carry MW-scale weights instead of fractions
  summing to 1. Normalize in core.
- Mapping-workbook `From #`/`To #` cells hold a text placeholder for unmatched rows.
- Monthly outages are pipe-delimited with 28 columns; annual `_None` outage files are a
  comma-separated header with no rows.
- Only a PeakWD RAW ships, while ratings carry PeakWD, PeakWE and Off-peak (which can
  be identical for a month).
- `BaseCaseRating` is a fixed fraction of the RAW rate A on every monitored line.
- Transformer names in the contingency, monitored and GTC CSVs are the `Autos` sheet's
  `CRR Name`, not anything in the RAW; lines use the RAW comment. The sheet's `From #`
  and `To #` are swapped relative to the RAW for about half the rows.
- `Operations_Name` never equals a DAM branch name exactly; see identity-and-matching.md.
- Some `Lines` rows share one placeholder `Operations_Name`; RAW lines that are
  zero-impedance ties mostly have no workbook row.
- In `_Upd` annual packages, month folders and some file names gain `_Upd`/`_Upd<n>`.

## Validation
`scripts/validate_parsers.py`: every member kind recognized, RAW record counts
consistent, on every archived package.

## Open questions
- How annual `_Upd` revisions relate to their originals (which members change).
- Whether an `_Upd` revision should replace its original in `out` by default, or only
  when asked (snapshots already order revisions by posting time within a logical package).
