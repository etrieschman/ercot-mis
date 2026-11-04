# CRR Auction Results (NP7-803-M monthly, NP7-802-M long-term)

## What it is and why we use it

After each CRR auction ERCOT posts what cleared: the awards, the bids and offers, the
binding constraints with their shadow prices, and the shadow price of every settlement
point, per time-of-use block (Nodal Protocols 7.5.3.1). The results are the external test
of the CRR network model: `checks/crr_prices.py` rebuilds the settlement point shadow prices
from the binding constraints and our shift factors on the monthly model, as
`checks/prices.py` does for the DAM.

## Source and capture

EWS, Public. Report types 11201 (monthly) and 11203 (long-term). The display windows are
long, so the daily pull captures every posting. The Public API lists no CRR products.

## Package layout

One zip per auction, each table as a CSV and an XML with the same rows:

- `Common_BindingConstraint_<auction>_AUCTION`: one row per binding constraint, calendar
  period and time-of-use block: `DeviceName`, `DeviceType`, `Direction`, `Flow`, `Limit`,
  `Description`, `Contingency`, `CalendarPeriod`, `TimeOfUse`, `ShadowPrice`;
- `Common_SourceAndSinkShadowPrices_<auction>_AUCTION`: one row per settlement point,
  calendar period and block: `SourceSink`, `CalendarPeriod`, `TimeOfUse`,
  `ShadowPricePerMWH`;
- `Common_MarketResults_<auction>_AUCTION`: one row per awarded CRR: `CRR_ID`,
  `OriginalCRR_ID`, `AccountHolder`, `HedgeType`, `BidType`, `CRRType`, `Source`, `Sink`,
  `StartDate`, `EndDate`, `TimeOfUse`, `Bid24Hour`, `MW`, `ShadowPricePerMWH`;
- `Common_AuctionBidsAndOffers_<auction>_AUCTION`: every bid and offer with its price and
  the auction's shadow price;
- `Common_BaseLoading_<auction>_AUCTION_<month>`: the CRRs already held going into the
  auction, one file per month of the auction period.

`<auction>` is `<year>.<MON>.Monthly.Auction` or `<year>.<term>.AnnualAuction.Seq<n>`; the
term is `1st6` or `2nd6`. A long-term auction's files carry six calendar periods.

## What we parse

The CSVs, into `raw.crr_auction_binding_constraints`, `raw.crr_auction_source_sink_shadow_prices`,
`raw.crr_auction_market_results`, `raw.crr_auction_bids_and_offers` and
`raw.crr_auction_base_loading`, with snake_case names and typed numbers. The XMLs are kept
in the archive and not parsed. Every row carries `auction` (`monthly` or `annual`), `term`,
`sequence` and `month` from the file name, as the network model's rows do.

## Decisions

- **The binding constraint's shadow price is for one MW over the block** (CRA-01). The
  settlement point shadow price is per MWh. Nothing posted states the constraint's unit;
  `clock.block_hours` counts the block's hours and the check divides by them.
- **The settlement point shadow price is the marginal cost of injecting** (CRA-02), the
  negative of a DAM price's congestion term. A PTP Obligation clears at source minus sink;
  the check measures that identity on every award.
- **Time-of-use blocks** are Protocols 7.3(5)'s, with NERC's holidays (CRA-04). A linked
  three-block bid (`Bid24Hour`) clears at the hour-weighted mean of the blocks.
- **Devices, contingencies and directions** follow the CRR model's names (CRA-03): a
  binding row's device is a branch name or GTC code, its contingency a CRR contingency name
  or null for the base case, and `Direction` refers to the device name.

## ERCOT quirks

- `Direction` is blank on GTC rows, which have no orientation of their own.
- `Contingency` is null on base-case rows.
- Options' clearing prices are not a difference of settlement point shadow prices: an
  option is evaluated on positive flows only (Protocols 7.3(2)).

## Validation

`tests/test_raw_parsers.py` classifies the member names; `checks/crr_prices.py` writes
`data/reports/crr_prices/<month>.json` with the residual per block and the clearing price
identities. `uv run python scripts/check.py crr_prices` runs the newest month.

## Open questions

- The reference bus of the auction's shift factors is not posted; the check removes it as
  the median of the fitted prices.
- The long-term auctions clear six months at once on the annual models; the check runs on
  monthly auctions only.
- A minority of resource nodes miss by dollars per MWh in every block. Whether the cause
  is a binding row applied wrongly, a settlement point's bus, or the auction's own
  approximations is open.
