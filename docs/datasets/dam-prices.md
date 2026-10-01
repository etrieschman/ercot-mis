# DAM prices (NP4-191-CD, NP4-183-CD, NP4-190-CD)

## What it is and why we use it

What the Day-Ahead Market published after it cleared: the binding constraints with
their shadow prices (NP4-191-CD), the price at every electrical bus (NP4-183-CD) and
at every settlement point (NP4-190-CD). They are not inputs to a network model; they
are the test of one. ERCOT's DAM has no loss component, so a price is the system
price minus the shadow-price-weighted shift factors of the binding constraints, and
`scripts/check_prices.py` holds our DAM network to that.

## Source and capture

Public API archive (`sources/public_api.py`), one document per product and delivery
date, posted once the DAM clears. The archive reaches back years, so nothing is lost
by starting late; the daily pull looks at the last few days and older days are
fetched by hand with `fetch(product, since=...)`. The archive listing carries a
document ID, a posting time and a link, and no size or operating date.

## Package layout

A zip with one CSV: every hour of one delivery date.

## What we parse

The header picks the table (`raw/prices.py`): `dam_shadow_prices`, `dam_lmps`,
`dam_settlement_point_prices`. `delivery_date` (`MM/DD/YYYY`), `hour_ending`
(`HH:00`, `24:00` last) and `dst_flag` stay as ERCOT writes them; numbers are cast.

## Decisions

- Files, not the API's JSON rows: the same archive-and-provenance path as every other
  product.
- No operating date on the raw identity columns: the delivery date is in the rows.

## ERCOT quirks

- A binding row names the constraint by the DAM model's branch name and the
  contingency by the DAM contingency name, both as in NP4-500-SG once punctuation is
  ignored; the base case has its own contingency name.
- A binding GTC is named by the code the CRR package uses, not by the GTL workbook's
  readable name, and has no substations.
- The published flow is unsigned and equals the limit. Direction is in
  `from_station` / `to_station`, which may run against the model's from-to. Inside one
  substation the two voltages tell the ends apart, and only to the tenth of a kV: a line
  between two nodes of one level differs in the tenths digit alone.
- Shadow prices are non-negative; they enter the price with a minus sign in the
  model's own orientation (settled by the data in `check_prices.py`, both signs tried).

## Validation

`scripts/check_prices.py` writes `data/reports/prices/<snapshot>.json` per DAM hour:
how many binding rows resolve to a branch, a contingency or a GTC, and the residual of
the price identity at the settlement points.

## Open questions

- What ERCOT does for a node a contingency cuts off (we solve the part still connected
  to the slack and give the rest no shift factor); the `SpCtg` file is the lead.
- Electrical bus names in the LMP file (ERCOT's term for its finest pricing points) are not
  the RAW's node names; the mapping
  (NP4-160-SG) is archived and not parsed, so the check prices settlement points only.
