"""The EMIL products ercot-mis knows about.

A product spec declares everything product-specific, so adding a dataset means
adding a spec, not a pipeline. ``take`` says what ercot-mis does with a product:
``pull`` downloads its documents, ``track`` only lists them so availability is
recorded without fetching any bytes.

Report type IDs are EWS's numeric handle for a product; they are only listed
where confirmed from ERCOT's EMIL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Classification = Literal["Public", "Secure", "ECEII"]
Source = Literal["ews", "public_api"]
Take = Literal["pull", "track"]


@dataclass(frozen=True)
class Product:
    emil_id: str
    name: str
    classification: Classification
    source: Source
    take: Take
    report_type_id: int | None = None
    display_days: int | None = None


PRODUCTS: dict[str, Product] = {
    p.emil_id: p
    for p in (
        Product("NP7-801-M", "CRR Network Model (Long-Term)", "ECEII", "ews", "pull", 11204, 365),
        Product("NP7-800-M", "CRR Network Model (Monthly)", "ECEII", "ews", "pull", 11205, 365),
        Product(
            "NP4-500-SG",
            "Day-Ahead PSS/E Network Operations Model and Supporting Files",
            "ECEII", "ews", "pull", 13070, 31,
        ),
        Product(
            "NP4-160-SG",
            "Settlement Points List and Electrical Buses Mapping",
            "Public", "ews", "pull", 10008, 31,
        ),
        Product("NP3-220-SG", "Electrical Bus to Hub List", "Public", "ews", "pull", 10011, 31),
        Product("NP5-615-SG", "Standard Contingency List", "ECEII", "ews", "pull", 13006, 31),
        # GTC definitions and daily limits: the DAM model package (NP4-500-SG) carries no GTC
        # file; the CRR packages do. Protocols 3.10.7.6 posts both to the MIS Secure Area.
        Product("NP3-766-M", "Generic Transmission Limits", "ECEII", "ews", "pull", 11424, 31),
        Product("NP3-770-M", "Generic Transmission Constraints Methodology", "ECEII", "ews", "pull", 11425),
        Product("NP6-6-CD", "NSA Active Constraints", "ECEII", "ews", "track", 12305),
        Product("SYS-608-CD", "SCED Resource Shift Factors", "Secure", "ews", "track", 12354, 31),
        Product("NP3-966-ER", "60-Day DAM Disclosure Reports", "Public", "public_api", "pull"),
        Product("NP4-183-CD", "DAM Hourly LMPs", "Public", "public_api", "pull"),
        Product("NP4-190-CD", "DAM Settlement Point Prices", "Public", "public_api", "pull"),
        Product("NP4-191-CD", "DAM Shadow Prices", "Public", "public_api", "pull"),
        Product("NP4-159-CD", "Load Distribution Factors", "Public", "public_api", "pull"),
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
