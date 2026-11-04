"""DAM prices and pricing inputs from the Public API archive, one table per product.

Each document is a zip with one CSV, and the header picks the table: ``dam_shadow_prices``
(NP4-191-CD), ``dam_lmps`` (NP4-183-CD), ``dam_settlement_point_prices`` (NP4-190-CD),
``dam_system_lambda`` (NP4-523-CD), ``dam_deenergized_settlement_points`` (NP4-200-CD),
``dam_electrically_similar_settlement_points`` (NP4-158-SG), ``heuristic_pricing_associations``
(NP4-231-CD) and ``load_distribution_factors`` (NP4-159-CD). ERCOT's ``delivery_date``,
``hour_ending`` and ``dst_flag`` stay text; numbers are cast (docs/datasets/dam-prices.md).
"""

from __future__ import annotations


import pyarrow as pa

from .columns import Column, F, I, Member, classify_csv, parse_csv

VERSION = 2

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
    "dam_system_lambda": (*_TIME, Column("SystemLambda", F), _DST),
    "dam_deenergized_settlement_points": (*_TIME, Column("SettlementPoint"), _DST),
    "dam_electrically_similar_settlement_points": (*_TIME, Column("SettlementPoint"), Column("GroupIndex", I), Column("UpdateTime"), _DST),
    "heuristic_pricing_associations": (Column("MarketType"), Column("FromEBName", name="from_electrical_bus"),
                                       Column("ToEBName", name="to_electrical_bus"), Column("Type"), Column("Priority", I)),
    "load_distribution_factors": (Column("LdfDate", name="ldf_date"), Column("LdfHour", name="ldf_hour"), Column("SubStation", name="substation"),
                                  Column("DistributionFactor", F), Column("LoadID", name="load_id"), Column("MVARDistributionFactor", F,
                                                                                                            "mvar_distribution_factor"),
                                  Column("MRIDLoad", name="mrid_load"), _DST),
}


classify_member = classify_csv


def parse_member(member: Member, data: bytes) -> dict[str, pa.Table]:
    return parse_csv(data, TABLES, "DAM prices")
