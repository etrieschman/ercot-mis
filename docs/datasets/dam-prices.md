# DAM prices and pricing inputs (NP4-191-CD, NP4-183-CD, NP4-190-CD, NP4-523-CD, NP4-200-CD, NP4-158-SG, NP4-231-CD, NP4-159-CD)

## What it is and why we use it

What the Day-Ahead Market published after it cleared: the binding constraints with their
shadow prices (NP4-191-CD), the price at every electrical bus (NP4-183-CD; electrical bus
is ERCOT's word for its finest pricing point) and the price at every settlement point
(NP4-190-CD). They are the test of our DAM network. The DAM has no loss term, so a price
is the system price minus each binding constraint's shadow price times the point's shift
factor on it, and `checks/prices.py` holds our network to that identity (PRC-01).

Beside them, the inputs ERCOT publishes about how it formed the prices: the system price
(NP4-523-CD, the shadow price of the power balance constraint), the settlement points
de-energized in the base case (NP4-200-CD), the settlement points it treats as
electrically similar (NP4-158-SG), the electrical bus mapping it prices a de-energized
bus from (NP4-231-CD, Protocols 4.5.1(8)(a)), and the load distribution factors that
weight a load zone (NP4-159-CD).

## Source and capture

The Public API archive (`public_api.py`): one document per product and delivery date,
posted once the DAM clears. The archive reaches back years, so the daily pull looks
back a month and older days are fetched by hand with `fetch(product, since=...)`. The
archive listing gives a document id, a posting time and a link, without a size or an
operating date.

## Package layout

A zip with one CSV holding every hour of one delivery date. The heuristic mapping has
no date in its rows (DAM and SCED rows side by side, with a type and a priority). The
load distribution factors come several weeks at a time in one CSV.

## What we parse

The header picks the table (`raw/prices.py`): `dam_shadow_prices`, `dam_lmps`,
`dam_settlement_point_prices`, `dam_system_lambda`, `dam_deenergized_settlement_points`,
`dam_electrically_similar_settlement_points`, `heuristic_pricing_associations` and
`load_distribution_factors`. `delivery_date` (`MM/DD/YYYY`), `hour_ending` (`HH:00`,
`24:00` last) and `dst_flag` stay as ERCOT writes them; numbers are cast. The raw rows
carry no operating date: the delivery date is in the rows.

## Decisions

- **Files, not the API's JSON rows**: the same archive and provenance path as every
  other product.
- **The published system price is the offset** (NP4-523-CD). Where it is not archived
  the median of the fitted prices stands in, and the report says which was used.
- **Shift factors enter at full precision** (PRC-06). ERCOT's two-percent rule decides
  whether a constraint is resolvable, not how a price is formed.
- **A DAM hub is a two-level average** (SP-01, Protocols 3.5.2): equal weight per Hub
  Bus, then equal weight per energized bus inside it.
- **Settlement points a contingency cuts off** follow Protocols 4.6.1.2 and 4.5.1(8)(b):
  under that contingency a point's weights are renormalized over the buses still
  connected, and a point with no bus left has no sensitivity (PRC-03). The report carries
  the residual under that rule and under plain weights.

## ERCOT quirks

- A binding row names the constraint by the DAM model's branch name and the contingency
  by the DAM contingency name, both as in NP4-500-SG once punctuation is ignored; the
  base case has a contingency name of its own.
- A binding GTC is named by the code the CRR package uses, not by the GTL workbook's
  readable name, and has no substations.
- The published flow is unsigned and equals the limit. Direction is in `from_station`
  and `to_station` (ERCOT's header), which may run against the model's from-to. Inside a
  substation the two voltages tell the ends apart, to the tenth of a kV (PRC-02).
- Shadow prices are non-negative and enter the price with a minus sign in the model's
  own orientation (PRC-01).
- Hours join DAM models on `interval_start_utc`, built from the delivery date, the hour
  ending and `dst_flag`; the flag marks the repeated hour of the long day (NAM-08).
- The system price file writes the hour without a leading zero (`1:00`); the price files
  write `01:00`. The check reads the hour as a number.
- Electrically similar settlement points are published as groups per hour. Some groups
  carry prices that differ by more than a cent, so the grouping is not a price identity;
  `checks/prices.py` counts them.

## Validation

`checks/prices.py` writes one report per DAM hour under `data/reports/prices/`: how many
binding rows resolve to a branch, a contingency or a GTC and how each was applied; the
residual at the settlement points, overall, by kind and for combined-cycle trains (SP-05);
ERCOT's `SpCtg` cut-off lists against ours (NAM-07); the base-case de-energized list
against the points our network has no bus for (NAM-07, TOP-05); and the price spread
inside each electrically similar group. `checks/ldf.py` compares the published load
distribution factors with the `Ld` file's substation shares for one hour (SP-02).

## Open questions

- The de-energized list names points the check never prices, because their buses are
  dropped from the network. Pricing them needs the heuristic mapping (NP4-231-CD)
  resolved from electrical bus names to DAM buses through NP4-160-SG.
- Electrical bus names in the LMP file are not the RAW's bus names. The mapping
  (NP4-160-SG) is parsed but not joined to DAM buses, so the check prices settlement
  points only.
