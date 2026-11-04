"""ERCOT's settlement point mappings (NP4-160-SG, NP3-220-SG) as the ``sp_*``, ``hub_buses`` and ``electrical_buses`` tables.

The price files name electrical buses, ERCOT's word for a pricing point. These two products
map every electrical bus to its PSS/E name and number, voltage level, substation, load zone,
resource node and hub, and list each hub's electrical buses. A document is a zip of CSVs and
the header picks the table; the listing's posting time dates it (docs/datasets/settlement-point-mappings.md).
"""

from __future__ import annotations


import pyarrow as pa

from .table import Column, F, I, Member, classify_csv, parse_csv

VERSION = 2

TABLES: dict[str, tuple[Column, ...]] = {
    "sp_electrical_bus_mapping": (
        Column("ELECTRICAL_BUS"), Column("NODE_NAME"), Column("PSSE_BUS_NAME"), Column("VOLTAGE_LEVEL"), Column("SUBSTATION"),
        Column("SETTLEMENT_LOAD_ZONE"), Column("RESOURCE_NODE"), Column("HUB_BUS_NAME"), Column("HUB"), Column("PSSE_BUS_NUMBER", I)),
    "sp_resource_node_units": (Column("RESOURCE_NODE"), Column("UNIT_SUBSTATION"), Column("UNIT_NAME")),
    "sp_noie_mapping": (Column("PHYSICAL_LOAD"), Column("NOIE"), Column("VOLTAGE_NAME"), Column("SUBSTATION"), Column("ELECTRICAL_BUS")),
    "sp_ccp_resource_names": (Column("CCP_NAME"), Column("LOGICALREOURCENODENAME", name="logical_resource_node_name")),
    "sp_hub_and_dc_tie_names": (Column("NAME"),),
    "hub_buses": (Column("ELECTRICAL_BUS"), Column("ELECTRICAL_BUS_KV", F), Column("HUB_BUS_NAME"), Column("HUB")),
    "electrical_buses": (Column("ELECTRICAL_BUS"),),
}


classify_member = classify_csv


def parse_member(member: Member, data: bytes) -> dict[str, pa.Table]:
    return parse_csv(data, TABLES, "settlement point mappings")
