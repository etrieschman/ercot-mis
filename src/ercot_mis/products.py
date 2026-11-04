"""The EMIL products ercot-mis knows about.

A product spec declares everything product-specific, so adding a dataset means
adding a spec, not a pipeline. ``is_fetched`` says whether ``fetch`` downloads a
product's documents; a product that is not fetched is only listed, so the catalog
records what ERCOT offers without taking any bytes.

Report type IDs are EWS's numeric handle for a product; they are only listed
where confirmed from ERCOT's EMIL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Classification = Literal["Public", "Secure", "ECEII"]
Source = Literal["ews", "public_api"]


@dataclass(frozen=True)
class Product:
    emil_id: str
    name: str
    classification: Classification
    source: Source
    is_fetched: bool
    report_type_id: int | None = None
    display_days: int | None = None


PRODUCTS: dict[str, Product] = {
    p.emil_id: p
    for p in (
        Product("NP7-801-M", "CRR Network Model (Long-Term)", "ECEII", "ews", True, 11204, 365),
        Product("NP7-800-M", "CRR Network Model (Monthly)", "ECEII", "ews", True, 11205, 365),
        Product(
            "NP4-500-SG",
            "Day-Ahead PSS/E Network Operations Model and Supporting Files",
            "ECEII", "ews", True, 13070, 31,
        ),
        Product(
            "NP4-160-SG",
            "Settlement Points List and Electrical Buses Mapping",
            "Public", "ews", True, 10008, 31,
        ),
        Product("NP3-220-SG", "Electrical Bus to Hub List", "Public", "ews", True, 10011, 31),
        Product("NP5-615-SG", "Standard Contingency List", "ECEII", "ews", True, 13006, 31),
        # GTC definitions and daily limits: the DAM model package (NP4-500-SG) carries no GTC
        # file; the CRR packages do. Protocols 3.10.7.6 posts both to the MIS Secure Area.
        Product("NP3-766-M", "Generic Transmission Limits", "ECEII", "ews", True, 11424, 31),
        Product("NP3-770-M", "Generic Transmission Constraints Methodology", "ECEII", "ews", True, 11425),
        Product("NP6-6-CD", "NSA Active Constraints", "ECEII", "ews", False, 12305),
        Product("SYS-608-CD", "SCED Resource Shift Factors", "Secure", "ews", False, 12354, 31),
        Product("NP3-966-ER", "60-Day DAM Disclosure Reports", "Public", "public_api", True, 13051),
        Product("NP4-183-CD", "DAM Hourly LMPs", "Public", "public_api", True, 12328),
        Product("NP4-190-CD", "DAM Settlement Point Prices", "Public", "public_api", True, 12331),
        Product("NP4-191-CD", "DAM Shadow Prices", "Public", "public_api", True, 12332),
        Product("NP4-159-CD", "Load Distribution Factors", "Public", "public_api", True, 12324),
        # Tracked from the Public API catalogue (2026-10-01 sweep): known, not pulled yet.
        # What each is for is in docs/datasets/planning-inputs.md.
        Product("NP4-523-CD", "DAM System Lambda", "Public", "public_api", True, 13113),
        Product("NP4-200-CD", "DAM De-Energized Settlement Points in Base Case", "Public", "public_api", True, 13063),
        Product("NP4-158-SG", "DAM Electrically Similar Settlement Points", "Public", "public_api", True, 13058),
        Product("NP4-231-CD", "Electrical Bus Mapping for Heuristic Pricing", "Public", "public_api", True, 13098),
        Product("NP4-194-CD", "DAM PTP Obligation Results by Settlement Point", "Public", "public_api", False, 13042),
        Product("NP3-965-ER", "60-Day SCED Disclosure Reports", "Public", "public_api", False, 13052),
        Product("NP3-909-ER", "2-Day DAM Bids and Offers Reports", "Public", "public_api", False, 13055),
        Product("NP3-907-EX", "2-Day DAM Energy Curves", "Public", "public_api", False, 25813),
        Product("NP3-233-CD", "Hourly Resource Outage Capacity", "Public", "public_api", False, 13103),
        Product("NP1-346-ER", "Unplanned Resource Outages Report", "Public", "public_api", False, 22912),
        Product("NP3-161-CD", "Available Resource Planned Outage Capacity Margin_7 Day Plus", "Public", "public_api", False, 22470),
        Product("NP6-345-CD", "Actual System Load by Weather Zone", "Public", "public_api", False, 13101),
        Product("NP3-565-CD", "Seven-Day Load Forecast by Model and Weather Zone", "Public", "public_api", False, 14837),
        Product("NP4-732-CD", "Wind Power Production - Hourly Averaged Actual and Forecasted Values", "Public", "public_api", False, 13028),
        Product("NP4-737-CD", "Solar Power Production - Hourly Averaged Actual and Forecasted Values", "Public", "public_api", False, 13483),
        Product("NP6-86-CD", "SCED Shadow Prices and Binding Transmission Constraints", "Public", "public_api", False, 12302),
    )
}


def get_product(key: str | int) -> Product:
    """Look a product up by EMIL ID (any case) or by EWS report type ID."""
    if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
        for product in PRODUCTS.values():
            if product.report_type_id == int(key):
                return product
    elif key.upper() in PRODUCTS:
        return PRODUCTS[key.upper()]
    raise KeyError(f"Unknown product {key!r}; known EMIL IDs: {', '.join(PRODUCTS)}")
