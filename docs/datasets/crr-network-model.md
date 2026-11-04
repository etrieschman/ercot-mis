# CRR Network Model (NP7-801-M annual, NP7-800-M monthly)

## What it is and why we use it

The network ERCOT clears CRR auctions on. A package carries the topology (a PSS/E RAW),
the enforced ratings per time-of-use block, the contingencies, the generic transmission
constraints (GTCs: limits on the sum of flows over several branches), the source and
sink definitions, and a workbook that maps CRR element names to Network Operations
Model names. That workbook is the key for matching CRR to DAM.

## Source and capture

EWS, ECEII. Report types 11204 (annual) and 11205 (monthly). The display window is one
year and EWS keeps nothing older, so `scripts/daily_pull.py` fetches every document
inside the window. An annual auction posts six sequences a year plus `_Upd` revisions;
the monthly auction posts one document a month.

## Package layout

- **Annual**: one folder per month of the term, named by term, sequence and month, plus
  package-level mapping workbooks, one-line diagrams, outages, dynamic ratings and
  substation one-lines. Six months per package.
- **Monthly**: flat, one file per kind.
- Kinds: the network model (RAW, PeakWD only), contingencies, monitored lines and
  transformers, non-thermal constraints (the GTCs), sources and sinks (each as CSV and
  XML), the mapping workbook (sheets `Lines` and `Autos`), outages, dynamic ratings and
  one-line diagrams.

## What we parse

`raw/crr_network_model.py` writes `crr_contingencies`, `crr_monitored_lines_and_transformers`,
`crr_non_thermal_constraints`, `crr_sources_and_sinks`, `crr_mapping_lines`,
`crr_mapping_autos` and `crr_outages`, and `raw/psse_raw.py` writes the `psse_*` tables from
the RAW. Archived and not parsed: the XML twins (the CSV is canonical), the one-line
diagrams (images), and the dynamic ratings workbook (monthly ambient-temperature
assumptions; the monitored CSV already carries the ratings they produce).

## Decisions

- **CSV over XML.** Checked on a full monthly package: the same rows and values. The XML
  adds nesting that the CSV encodes as repeated names.
- **Member identity comes from the path.** Auction type, term, sequence, month and
  time-of-use block are read from member names (`classify_member`); the document's
  revision comes from the catalog's posting order (TOP-11).
- **Headers are checked exactly.** A changed or reordered header raises `ParseError`.
- **Workbook node numbers stay text**, because unmatched rows hold a text placeholder.
- **The Outages file is recorded, not applied.** The RAW already reflects the outage
  scheduler for its own snapshot (TOP-03). The file spans the month; of the branches it
  names through a workbook operations name, most are out of service in the RAW and some
  are not. `checks/register.py` measures it (TOP-03, TOP-04).
- **Transformer name orientation is recorded.** `core.branch.is_name_reversed` is true
  when the `Autos` name lists the RAW's ends the other way round, so a CSV flow
  direction, which refers to the name, can be turned into the RAW's orientation (NAM-02).

## ERCOT quirks

- Device types are spelled differently per file: the contingency CSV says `LINE` and
  `XFMR`, the monitored CSV `Line` and `XFMR`, the GTC CSV and every XML `Line` and
  `Transformer`. Core normalizes them.
- The GTC CSV header has spaces after commas.
- **Temporary topology is labelled.** The workbook's operations equipment code is a name
  for standing equipment and a sentence for branches ERCOT added as temporary split-bus
  topology for outages. `core.branch.is_temporary` carries it. The ties that join a
  temporary node to the standing one have no workbook row, so a temporary node is one a
  temporary branch touches.
- **Switch outages cannot be tied to RAW ties by name.** The Outages file lists breakers
  and disconnects by substation and device name; the RAW names a tie only by its two
  nodes, and the workbook gives an operations name to a minority of ties, none of them a
  device name from the Outages file.
- The RAW names a load only by its node (the comment holds node number, node name, load
  id and owner), so `core.load` builds `load_id` from the node name and the load id. A
  few annual RAWs repeat a load record (same node and id, sometimes a different MW);
  core keeps both rows.
- Loads out of service keep their MW in the CRR RAW; the DAM RAW zeroes them.
- Some sources and sinks (load zones, hubs) carry MW-scale weights rather than fractions
  that sum to one. Core normalizes them.
- Monthly outage files are pipe-delimited with 28 columns; annual outage files named
  `_None` are a comma-separated header with no rows.
- Only a PeakWD RAW ships, while the ratings carry PeakWD, PeakWE and Off-peak blocks,
  which can be identical for a month (RAT-06, RAT-07).
- `BaseCaseRating` is a fixed fraction of the RAW's rate A on every monitored line;
  `checks/identity.py` records the fraction.
- The contingency, monitored and GTC CSVs name transformers by the `Autos` sheet's CRR
  name, which is not in the RAW; lines use the RAW comment. The sheet's `From #` and
  `To #` are swapped relative to the RAW for a large minority of rows.
- An `Operations_Name` never equals a DAM branch name exactly; see
  identity-and-matching.md. Several `Lines` rows share one placeholder operations name,
  and RAW ties mostly have no workbook row.
- In `_Upd` annual packages, month folders and some file names gain `_Upd` or `_Upd<n>`.

## Validation

`scripts/validate_parsers.py` checks that every member kind is recognized and that RAW
record counts are consistent, on every archived package.

## Open questions

- How an annual `_Upd` revision relates to its original: which members change. Snapshots
  order revisions by posting time within a logical package (TOP-11); whether a revision
  should replace its original by default is undecided.
