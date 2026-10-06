# DAM prices and pricing inputs (NP4-191-CD, NP4-183-CD, NP4-190-CD, NP4-523-CD, NP4-200-CD, NP4-158-SG, NP4-231-CD, NP4-159-CD)

## What it is and why we use it

What the Day-Ahead Market published after it cleared: the binding constraints with
their shadow prices (NP4-191-CD), the price at every electrical bus (NP4-183-CD) and
at every settlement point (NP4-190-CD). They are not inputs to a network model; they
are the test of one. ERCOT's DAM has no loss component, so a price is the system
price minus the shadow-price-weighted shift factors of the binding constraints, and
`scripts/check_prices.py` holds our DAM network to that.

Beside them, the inputs ERCOT publishes for how it formed those prices: the system
price itself (NP4-523-CD, the shadow price of the power balance constraint), the
settlement points de-energized in the base case (NP4-200-CD), the settlement points it
treats as electrically similar (NP4-158-SG), the electrical-bus-to-electrical-bus
mapping it prices a de-energized bus from (NP4-231-CD, Protocols 4.5.1(8)(a)), and the
load distribution factors that weight a load zone (NP4-159-CD). ERCOT's own settlement
point mappings in electrical-bus vocabulary (NP4-160-SG, NP3-220-SG) are parsed by
`raw/mappings.py` and sit beside them.

## Source and capture

Public API archive (`sources/public_api.py`), one document per product and delivery
date, posted once the DAM clears. The archive reaches back years, so nothing is lost
by starting late; the daily pull looks at the last few days and older days are
fetched by hand with `fetch(product, since=...)`. The archive listing carries a
document ID, a posting time and a link, and no size or operating date.

## Package layout

A zip with one CSV: every hour of one delivery date. The heuristic mapping has no date
in its rows (DAM and SCED rows side by side, a type and a priority); the load
distribution factors come a few weeks at a time in one large CSV.

## What we parse

The header picks the table (`raw/prices.py`): `dam_shadow_prices`, `dam_lmps`,
`dam_settlement_point_prices`, `dam_system_lambda`, `dam_deenergized_settlement_points`,
`dam_electrically_similar_settlement_points`, `heuristic_pricing_associations`,
`load_distribution_factors`. `delivery_date` (`MM/DD/YYYY`), `hour_ending`
(`HH:00`, `24:00` last) and `dst_flag` stay as ERCOT writes them; numbers are cast.

## Decisions

- Files, not the API's JSON rows: the same archive-and-provenance path as every other
  product.
- No operating date on the raw identity columns: the delivery date is in the rows.
- The check uses the published system price when it is archived and falls back to the
  fitted median difference before that; the report says which, and how far the fit was
  from the published value (a diagnostic of the remaining misses, not of the price).
- Shift factors enter at full precision (PRC-06): ERCOT's two-percent rule is about
  whether a constraint is resolvable, not about pricing.
- A DAM hub is a two-level average (SP-01, Protocols 3.5.2): per Hub Bus, then per
  energized bus inside it. Under the flat average no hub reproduced within a cent; under
  the protocol's rule hubs reproduce to the cent in hours without a GTC miss.
- Points a contingency cuts off follow Protocols 4.6.1.2 and 4.5.1(8)(b) (PRC-03); the
  report carries the residual under that rule and under the plain reading.

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
- The system price file writes the hour without a leading zero (`1:00`); the price files
  write `01:00`. The check reads the hour as a number.
- Electrically similar settlement points are published as groups per hour; a few groups
  a day carry published prices that differ by more than a cent, so the grouping is not
  an exact price identity.

## Validation

`scripts/check_prices.py` writes `data/reports/prices/<snapshot>.json` per DAM hour:
how many binding rows resolve to a branch, a contingency or a GTC, and the residual of
the price identity at the settlement points.

## Open questions

- The de-energized list (NP4-200-CD) names points the check never prices, because their
  buses are dropped from the network; pricing them needs the heuristic mapping
  (NP4-231-CD) resolved from electrical bus names to nodes through NP4-160-SG.
- The published load distribution factors (NP4-159-CD) against the `Ld` file's shares.
- Electrical bus names in the LMP file (ERCOT's term for its finest pricing points) are not
  the RAW's node names; the mapping
  (NP4-160-SG) is archived and not parsed, so the check prices settlement points only.
