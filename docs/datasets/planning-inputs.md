# Planning-stage inputs

Datasets beyond the network models that describe what was known, offered and outaged
around an auction or a DAM run. The Public API's product catalogue
(`GET /api/public-reports`) lists every public product with its EMIL id, report type,
posting frequency and archive depth. The products below are in `archive/products.py` with
`is_fetched=False`: listed, so the catalog records what ERCOT offers, and not downloaded.
Fetching one means setting `is_fetched=True`, adding it to the daily pull, and writing
its parser and note.

## What the Public API has

| purpose | products |
|---|---|
| The system price | NP4-523-CD DAM System Lambda (fetched; dam-prices.md) |
| What the DAM does with settlement points that are cut off | NP4-200-CD De-Energized Settlement Points; NP4-158-SG Electrically Similar Settlement Points; NP4-231-CD Electrical Bus Mapping for Heuristic Pricing (fetched; dam-prices.md) |
| Offers, bids and awards, 60 days later | NP3-966-ER 60-Day DAM Disclosure (fetched; dam-disclosure.md); NP3-965-ER 60-Day SCED Disclosure |
| The same, aggregated, two days later | NP3-909-ER 2-Day DAM Bids and Offers; NP3-907-EX 2-Day DAM Energy Curves |
| PTP obligations cleared in the DAM, by settlement point | NP4-194-CD |
| Generation outages | NP3-233-CD Hourly Resource Outage Capacity; NP1-346-ER Unplanned Resource Outages; NP3-161-CD planned outage capacity margin |
| Load | NP6-345-CD Actual System Load by Weather Zone; NP3-565-CD Seven-Day Load Forecast; NP4-159-CD Load Distribution Factors (fetched; dam-prices.md) |
| Wind and solar | NP4-732-CD and NP4-737-CD, hourly actual and forecast |
| Real-time counterpart of the DAM shadow prices | NP6-86-CD SCED Shadow Prices and Binding Transmission Constraints |

## What the Public API does not have

- **Transmission outages**: the Outage Scheduler's proposed, approved, accepted and
  withdrawn reports with submission times. They are MIS reports and need their EWS
  report type ids from the EMIL. The CRR package's own Outages file is parsed
  (`raw.crr_outages`) and carries planned and actual dates per outage.
- **CRR auction results**: awards, bids and offers, binding constraints and clearing
  prices. Same route. Nothing checks the CRR model against what the auction cleared
  until they are archived.
- **Network models before the first pull.** EWS keeps only the display window, and the
  Public API carries no network model.

## Open questions

- The EWS report type ids and classifications of the transmission outage reports and
  the CRR auction results.
- Whether the two-day reports add anything once the 60-day disclosure for the same day
  is in.
