"""DAM prices and pricing inputs from the Public API archive.

Each archived document is a zip holding one CSV (one delivery date, all hours, except
where noted). The products share the shape and differ in columns, so the header picks
the table:

- ``dam_shadow_prices`` (NP4-191-CD): one row per binding constraint and hour, with the
  constraint's and the contingency's names, limit, flow, violation and shadow price;
- ``dam_lmps`` (NP4-183-CD): one row per electrical bus and hour;
- ``dam_settlement_point_prices`` (NP4-190-CD): one row per settlement point and hour;
- ``dam_system_lambda`` (NP4-523-CD): the system price, one row per hour;
- ``dam_deenergized_settlement_points`` (NP4-200-CD): settlement points with no
  connection in the DAM base case, per hour;
- ``dam_electrically_similar_settlement_points`` (NP4-158-SG): settlement points ERCOT
  treats as one electrical location, per hour, grouped by ``group_index``;
- ``heuristic_pricing_associations`` (NP4-231-CD): the electrical bus whose price a
  de-energized bus takes, by priority, for DAM and RTM; no date in the rows (the
  listing's posting time dates the document);
- ``load_distribution_factors`` (NP4-159-CD): each load's share of its zone per hour,
  posted for a period of weeks at a time (a large file).

Raw keeps ERCOT's text for ``delivery_date`` (``MM/DD/YYYY``) and ``hour_ending``
(``HH:00``, ``24:00`` for the last hour) and the ``dst_flag``; numbers are cast.
"""

from __future__ import annotations


import pyarrow as pa

from .table import Column, F, I, Member, classify_csv, parse_csv

VERSION = 2  # 2: the always-null operating_date and hour identity columns are gone

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
