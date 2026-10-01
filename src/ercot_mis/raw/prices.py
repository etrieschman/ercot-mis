"""DAM prices from the Public API archive: shadow prices, bus LMPs, settlement point prices.

Each archived document is a zip holding one CSV for one delivery date, all hours. The
three products share the shape and differ in columns, so the header picks the table:

- ``dam_shadow_prices`` (NP4-191-CD): one row per binding constraint and hour, with the
  constraint's and the contingency's names, limit, flow, violation and shadow price;
- ``dam_lmps`` (NP4-183-CD): one row per electrical bus and hour;
- ``dam_settlement_point_prices`` (NP4-190-CD): one row per settlement point and hour.

Raw keeps ERCOT's text for ``delivery_date`` (``MM/DD/YYYY``) and ``hour_ending``
(``HH:00``, ``24:00`` for the last hour) and the ``dst_flag``; numbers are cast.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

from .table import Column, F, I, ParseError, read_delimited

VERSION = 1

_TIME = (Column("DeliveryDate"), Column("HourEnding"))
_DST = Column("DSTFlag", name="dst_flag")
TABLES: dict[str, tuple[Column, ...]] = {
    "dam_shadow_prices": (
        *_TIME, Column("ConstraintID", I), Column("ConstraintName"), Column("ContingencyName"),
        Column("ConstraintLimit", F), Column("ConstraintValue", F), Column("ViolationAmount", F), Column("ShadowPrice", F),
        Column("FromStation"), Column("ToStation"), Column("FromStationkV", F, "from_station_kv"),
        Column("ToStationkV", F, "to_station_kv"), Column("DeliveryTime"), _DST),
    "dam_lmps": (*_TIME, Column("BusName"), Column("LMP", F), _DST),
    "dam_settlement_point_prices": (*_TIME, Column("SettlementPoint"), Column("SettlementPointPrice", F), _DST),
}
_BY_HEADER = {tuple(c.header for c in columns): table for table, columns in TABLES.items()}


@dataclass(frozen=True)
class PriceMember:
    path: str
    format: str
    operating_date: None = None  # a file's delivery date is in its rows, not its name
    hour: None = None

    @property
    def is_parsed(self) -> bool:
        return self.format == "csv"


def classify_member(path: str) -> PriceMember | None:
    if path.endswith("/"):
        return None
    return PriceMember(path, path.rsplit(".", 1)[-1].lower() if "." in path else "")


def parse_member(member: PriceMember, data: bytes) -> dict[str, pa.Table]:
    first = data.removeprefix(b"\xef\xbb\xbf").partition(b"\n")[0].decode("utf-8", "replace")
    header = tuple(h.strip() for h in first.rstrip("\r").split(","))
    table = _BY_HEADER.get(header)
    if table is None:
        raise ParseError(f"DAM prices: header matches no known table; found {list(header)}")
    return {table: read_delimited(data, TABLES[table], table)}
