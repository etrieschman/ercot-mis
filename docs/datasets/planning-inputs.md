# Planning-stage inputs (candidates)

Datasets beyond the network models that describe what was known, offered and outaged
around an auction or a DAM run. This note is the result of the 2026-10-01 sweep of the
Public API's product catalogue (`GET /api/public-reports` lists every public product
with its EMIL ID, report type, posting frequency and archive depth). The products
below are in `products.py` as `track`: known and named, not pulled. Pulling one means
flipping it to `pull`, adding it to the daily pull, and writing its parser and note.

## What the Public API has

| purpose | products |
|---|---|
| The system price (removes the one free number in the price check) | NP4-523-CD DAM System Lambda |
| What the DAM does with settlement points that are cut off | NP4-200-CD De-Energized Settlement Points in Base Case; NP4-158-SG Electrically Similar Settlement Points; NP4-231-CD Electrical Bus Mapping for Heuristic Pricing |
| Offers, bids and awards, 60 days later (the injections for the flow check; the generation stack) | NP3-966-ER 60-Day DAM Disclosure; NP3-965-ER 60-Day SCED Disclosure |
| The same, aggregated, 2 days later | NP3-909-ER 2-Day DAM Bids and Offers; NP3-907-EX 2-Day DAM Energy Curves |
| PTP obligations cleared in the DAM, by settlement point | NP4-194-CD |
| Generation outages | NP3-233-CD Hourly Resource Outage Capacity (aggregate, forward-looking); NP1-346-ER Unplanned Resource Outages; NP3-161-CD planned outage capacity margin |
| Load | NP6-345-CD Actual System Load by Weather Zone; NP3-565-CD Seven-Day Load Forecast by Model and Weather Zone; NP4-159-CD Load Distribution Factors |
| Wind and solar | NP4-732-CD and NP4-737-CD (hourly actual and forecast) |
| Real-time counterpart of the DAM shadow prices | NP6-86-CD SCED Shadow Prices and Binding Transmission Constraints |

## What it does not have

- **Transmission outages** (the Outage Scheduler's proposed, approved, accepted and
  withdrawn reports, with submission times). Not in the Public API catalogue; they are
  MIS reports and need their EWS report type IDs from the EMIL. The CRR package's own
  Outages file is already parsed (`raw.crr_outages`) and carries planned and actual
  dates per outage.
- **CRR auction results** (awards, bids and offers, binding constraints, clearing
  prices). Not in the catalogue either; same route.
- **Network models before our first pull.** EWS keeps only the display window and the
  Public API carries no network model.

## Open questions

- EWS report type IDs and classifications for the transmission outage reports and the
  CRR auction results.
- Whether the 2-day reports add anything once the 60-day disclosure for the same day
  is in.
