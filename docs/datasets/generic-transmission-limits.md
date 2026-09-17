# Generic Transmission Limits (NP3-766-M) and GTC Methodology (NP3-770-M)

## What it is and why we use it
The DAM network model package ships no GTC file. Nodal Protocols 3.10.7.6 post GTC
definitions and day-ahead limits to the MIS Secure Area as ECEII: NP3-766-M carries
the limits, NP3-770-M the per-GTC studies. Each GTL is enforced as a base-case
constraint in CRR, DAM and real time, so the DAM polytope needs these rows.

## Source and capture
EWS, ECEII. Report types 11424 (limits) and 11425 (methodology). NP3-766-M has a
31-day display window and is captured daily by `scripts/daily_pull.py`; NP3-770-M is
posted as needed.

## Package layout
NP3-766-M lists every document as `xls`, but two shapes arrive:
- the daily **GTL workbook**, really an xlsx: one sheet `Results`, one row per hour,
  a `Time` column holding the interval start as `YYYY-MM-DD HH:MM:SS`, then two
  columns per GTC, the real-time limit under the GTC's name and the day-ahead limit
  under `<name>` + newline + `DAM`;
- a legacy xls with a `DC Limits` sheet (DC tie limits), posted more often.
NP3-770-M is one zip of PDF, PPTX and database files.

## What we parse
`raw/gtl.py` -> `gtl_hourly` (`interval_start`, `delivery_date`, `hour_ending`,
`gtc_name`, `market` in {rt, dam}, `limit_mw`). The document is not a zip, so it is
parsed whole; the shape is detected from the bytes. Archived, not parsed: the DC tie
limits and all of NP3-770-M.

## Decisions
- **The delivery date comes from the file**, not from the listing: the listing's
  operating date is the posting date, a couple of days earlier.
- **Latest document per delivery date wins** when the core layer looks up a day.
- **Names are crosswalked by hand.** The workbook uses human-readable GTC names, the
  CRR CSV uses short codes, and nothing in either file links them. The crosswalk is a
  manual override in `data/overrides/gtc_names.csv` (`gtl_name,crr_gtc_id`), never
  committed; `core.gtc` carries the CRR id as `crr_gtc_id` for DAM rows so the member
  definitions can be borrowed from the CRR model of the same constraint.
- DAM `core.gtc_member` stays empty until NP3-770-M definitions are parsed.

## ERCOT quirks
- One document format label for two file formats; detect by content.
- Hours are interval starts; `hour_ending` is derived as start hour + 1.

## Validation
`scripts/validate_parsers.py` does not cover this product yet; `build_raw` fails
loudly on a workbook whose columns do not pair up.

## Open questions
- Whether the DC-tie-limit documents are worth a table.
- Parsing NP3-770-M member definitions (PDF/PPTX) or finding a structured source.
