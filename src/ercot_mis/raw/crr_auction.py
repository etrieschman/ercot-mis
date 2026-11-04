"""CRR auction results (NP7-803-M monthly, NP7-802-M long-term): one table per CSV member.

The package holds the auction's bids and offers, the base-loaded CRRs, the binding
constraints with their shadow prices per calendar month and time-of-use block, the awards
(``MarketResults``) and the source and sink shadow prices, each as a CSV and an XML; the CSV
is parsed and the XML archived. The member name gives the identity: the auction (``monthly``
or ``annual``), the annual term and sequence, and the month (the auction month for a monthly
result; the base-loading member's month for an annual one). Devices and contingencies carry
the CRR model's own names (docs/datasets/crr-auction-results.md).
"""

from __future__ import annotations

import re
from datetime import date

import pyarrow as pa

from .crr import MONTHS
from .table import F, Column, Member, extension, read_delimited

VERSION = 1

_KIND = re.compile(r"Common_([A-Za-z]+)_\d{4}\.")
_ANNUAL = re.compile(r"(\d{4}\.\d(?:st|nd)\d)\.AnnualAuction\.Seq(\d+)", re.IGNORECASE)
_MONTHLY = re.compile(r"(\d{4})\.([A-Z]{3})\.Monthly\.Auction", re.IGNORECASE)
_MEMBER_MONTH = re.compile(r"_([A-Z]{3})_(\d{4})(?=[._])", re.IGNORECASE)

_PRICE = Column("ShadowPricePerMWH", F, "shadow_price_per_mwh")
TABLES: dict[str, tuple[Column, ...]] = {
    "crr_auction_bids_and_offers": (Column("Source"), Column("Sink"), Column("BidType"), Column("StartDate"), Column("EndDate"), Column("HedgeType"),
                                    Column("TimeOfUse"), Column("MW", F), Column("BidPricePerMWH", F, "bid_price_per_mwh"), _PRICE),
    "crr_auction_base_loading": (Column("CRR_ID"), Column("AccountHolder"), Column("Source"), Column("Sink"), Column("HedgeType"), Column("TimeOfUse"),
                                 Column("MW", F), _PRICE),
    "crr_auction_binding_constraints": (Column("DeviceName"), Column("DeviceType"), Column("Direction"), Column("Flow", F), Column("Limit", F),
                                        Column("Description"), Column("Contingency"), Column("CalendarPeriod"), Column("TimeOfUse"), Column("ShadowPrice", F)),
    "crr_auction_market_results": (Column("CRR_ID"), Column("OriginalCRR_ID"), Column("AccountHolder"), Column("HedgeType"), Column("BidType"),
                                   Column("CRRType", name="crr_type"), Column("Source"), Column("Sink"), Column("StartDate"), Column("EndDate"),
                                   Column("TimeOfUse"), Column("Bid24Hour", name="bid_24_hour"), Column("MW", F), _PRICE),
    "crr_auction_source_sink_shadow_prices": (Column("SourceSink"), Column("CalendarPeriod"), Column("TimeOfUse"), _PRICE),
}
# The member name's kind token, and the table it holds.
KINDS = {"AuctionBidsAndOffers": "crr_auction_bids_and_offers", "BaseLoading": "crr_auction_base_loading",
         "BindingConstraint": "crr_auction_binding_constraints", "MarketResults": "crr_auction_market_results",
         "SourceAndSinkShadowPrices": "crr_auction_source_sink_shadow_prices"}


def classify_member(path: str) -> Member | None:
    """The member for a zip entry of an auction results package; None for a folder."""
    if path.endswith("/"):
        return None
    file = path.rsplit("/", 1)[-1]
    fmt = extension(file)
    found = _KIND.search(file)
    kind = KINDS.get(found.group(1), "unknown") if found else "unknown"
    auction = term = sequence = month = None
    if annual := _ANNUAL.search(file):
        auction, term, sequence = "annual", annual.group(1), int(annual.group(2))
        if (m := _MEMBER_MONTH.search(file)) and m.group(1).upper() in MONTHS:
            month = date(int(m.group(2)), MONTHS[m.group(1).upper()], 1)
    elif (monthly := _MONTHLY.search(file)) and monthly.group(2).upper() in MONTHS:
        auction, month = "monthly", date(int(monthly.group(1)), MONTHS[monthly.group(2).upper()], 1)
    return Member(path, kind, fmt, kind != "unknown" and fmt == "csv", {"auction": auction, "term": term, "sequence": sequence, "month": month})


def parse_member(member: Member, data: bytes) -> dict[str, pa.Table]:
    return {member.kind: read_delimited(data, TABLES[member.kind], f"CRR auction {member.kind}")}
