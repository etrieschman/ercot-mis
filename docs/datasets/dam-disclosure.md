# 60-Day DAM Disclosure (NP3-966-ER)

## What it is and why we use it

Sixty days after an operating day, ERCOT publishes everything the day-ahead market
received and awarded for it: every resource's three-part offer and award, energy-only
offers and bids with their awards, point-to-point obligation bids and awards, and the
ancillary service offers and awards. The awards are the injections and withdrawals the
DAM actually dispatched, so they are the input to the flow check (awards through our
network; every binding constraint at its limit, nothing enforced above it). The offer
and bid curves are the generation stack for later work.

## Source and capture

Public API archive, one document per operating day, posted daily with a 60-day lag.
Pulled daily since 2026-10-06 with the month-long lookback; older days can be fetched
by hand. The archive reaches back years. The first days whose models we also hold are
reached in mid-October 2026.

## Package layout

A zip of CSVs named `60d_DAM_<table>-DD-MON-YY.csv`, one delivery date each, all
hours. Seventeen tables: resource data for generation, storage and load resources;
ancillary service offers for each; energy-only offers and their awards; energy bids
and their awards; point-to-point obligation bids and their awards; point-to-point
obligations linked to options and their awards; ancillary-service-only offers and
awards; self-arranged ancillary services. Offer curves are wide (ten MW and price
pairs per row).

## What we parse

Every member, into `dam_60d_<table>` (`raw/disclosure.py`). The file name's date is
the `operating_date`; the header picks the table and must match exactly. Numbers (MW,
prices, awards, limits, costs) are cast; identifiers, flags and indicators stay text.

## Decisions

- All seventeen tables, not only the awards: the stack is cheap to carry and the
  header check costs nothing.
- Column specs were generated from the real headers once and are fixed in code, so a
  change by ERCOT fails the parse instead of being guessed around.

## ERCOT quirks

- `Hour Ending` is written without a leading zero (`1:00`) and there is no DST flag.
- Energy bid awards are written as negative MW; storage awards are negative when the
  resource charges. Both are kept as written.
- The generation and storage resource tables share one header exactly, so the header
  alone cannot tell them apart; the member name does (`_SAME_HEADER`). Before 2026-10-06
  the storage rows landed in the generation table.

## Validation

Row counts per table per day in `build_raw`'s result; the flow check (not built yet)
is the test of the awards' meaning.

## What core builds from it

`core.hourly_award` (`core/award.py`): one row per award with its hour in UTC, the
settlement point, a kind and a signed MW (INJ-01); `session.injections(day)` sums it to
the net injection per settlement point and hour, and `scripts/check_injections.py`
checks that the system nets to zero per hour (INJ-02).

## Open questions

- The first disclosure day that overlaps an archived DAM model (around 2026-10-15): push
  the injections through `out.network` and compare flows with limits and binding rows.
