# Generic Transmission Limits (NP3-766-M) and GTC Methodology (NP3-770-M)

## What it is and why we use it

A generic transmission constraint (GTC) is a limit on the sum of flows over several
branches, enforced as a base-case constraint in CRR, DAM and real time. The DAM package
ships no GTC file. Nodal Protocols 3.10.7.6 posts the definitions and the day-ahead
limits to the MIS Secure Area as ECEII: NP3-766-M carries the limits, NP3-770-M the
per-constraint studies that define the members.

## Source and capture

EWS, ECEII. Report types 11424 (limits) and 11425 (methodology). NP3-766-M has a 31-day
display window and `scripts/daily_pull.py` fetches it daily. NP3-770-M is posted when a
constraint changes.

## Package layout

NP3-766-M lists every document as `xls`, but two shapes arrive:

- the daily **GTL workbook**, an xlsx with one sheet, `Results`: one row per hour, a
  `Time` column with the interval start as a naive local timestamp, then two columns per
  GTC, the real-time limit under the GTC's name and the day-ahead limit under the name
  plus a line break and `DAM`;
- a legacy xls with a `DC Limits` sheet (DC tie limits), posted more often.

NP3-770-M is one zip of PDF, PPTX and database files.

## What we parse

`raw/generic_transmission_limits.py` writes `gtl_hourly`: `interval_start_utc`, `gtc_name`, `market` (`rt` or
`dam`) and `limit_mw`. The document is not a zip, so it is parsed whole and its shape is
told from the bytes. Archived and not parsed: the DC tie limits and all of NP3-770-M.

## Decisions

- **The hour comes from the file, not the listing.** The listing's operating date is the
  posting date, a couple of days before the delivery date. The `Time` column is naive
  local time, so the long day's repeated hour appears twice; the second copy is the
  repeated hour, and `interval_start_utc` is what core joins to a DAM hour (NAM-08).
- **The latest posting per delivery date wins** when core looks a day up (TOP-11). Posting
  time comes from the catalog's listing; document ids do not follow posting order.
- **Names are crosswalked by hand.** The workbook uses readable GTC names, the CRR CSV
  short codes, and nothing published links them. The crosswalk is
  `data/overrides/gtc_names.csv` (`gtl_name,crr_gtc_id`), never committed. `core.gtc`
  carries the CRR id as `crr_gtc_id` on DAM rows so the member definitions can be
  borrowed from the CRR model of the same constraint (PRC-04).
- **DAM `core.gtc_member` stays empty** until NP3-770-M's definitions are read.

## Validation

`scripts/validate_parsers.py` checks that every archived document is one of the two
shapes, that every workbook parses, that each delivery day has one row per hour and
market for every GTC, and that the day-ahead and real-time columns pair up. `build_raw`
fails on a workbook whose columns do not pair.

## Open questions

- The member branches of each GTC. NP3-770-M gives them in one PDF per constraint;
  reading it, or finding a structured source, is undecided (PRC-04).
- Whether the DC tie limit documents are worth a table.
