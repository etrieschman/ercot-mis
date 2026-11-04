# 60-Day DAM Disclosure (NP3-966-ER)

## What it is and why we use it

Sixty days after an operating day, ERCOT publishes everything the day-ahead market
received and awarded for it: every resource's three-part offer and award, energy-only
offers and bids with their awards, point-to-point obligation bids and awards, and the
ancillary service offers and awards. The awards are the injections and withdrawals the
DAM dispatched, so they are the input to the flow check: the awards through our network
should reproduce ERCOT's flow at every binding constraint (FLW-02). The offer and bid
curves are the generation stack for later work.

## Source and capture

The Public API archive, one document per operating day, posted daily with a 60-day lag.
`scripts/daily_pull.py` fetches it with a month-long lookback; older days are fetched by
hand. The archive reaches back years.

## Package layout

A zip of CSVs, one per table, each holding one delivery date and every hour. Seventeen
tables: resource data for generation, storage and load resources; ancillary service
offers for each; energy-only offers and their awards; energy bids and their awards;
point-to-point obligation bids and their awards; point-to-point obligations linked to
options and their awards; ancillary-service-only offers and awards; and self-arranged
ancillary services. Offer curves are wide (ten MW and price pairs per row).

## What we parse

Every member, into `dam_60d_<table>` (`raw/disclosure.py`). The date in the member name
is the rows' `operating_date`; the header picks the table and must match exactly.
Numbers (MW, prices, awards, limits, costs) are cast; identifiers, flags and indicators
stay text.

## Decisions

- **All seventeen tables**, not only the awards: the stack costs little to carry and the
  header check nothing.
- **Column specs are fixed in code.** A header ERCOT changes fails the parse instead of
  being guessed around.

## ERCOT quirks

- `Hour Ending` is written without a leading zero (`1:00`) and there is no DST flag.
- Energy bid awards are written as negative MW; storage awards are negative when the
  resource charges. Both are kept as written (INJ-01).
- The generation and storage resource tables share one header, so the header alone
  cannot tell them apart; the member name does.

## What core builds from it

`core.hourly_award` (`core/award.py`): one row per award with its hour in UTC, the
settlement point, a kind and a signed MW (INJ-01). `session.injections(day)` sums it to
the net injection per settlement point and hour; `checks/injections.py` checks that the
system nets to zero every hour (INJ-02).

## Validation

Row counts per table per day in `build_raw`'s result. `checks/flows.py` pushes the awards
through the hour's network and compares the flow on every binding constraint with
ERCOT's (FLW-02), counting what could not be placed (FLW-01) and the branches above
their base-case limit.
