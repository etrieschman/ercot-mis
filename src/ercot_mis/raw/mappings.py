"""Settlement point mappings in ERCOT's electrical-bus vocabulary (NP4-160-SG, NP3-220-SG).

The price files name electrical buses, not the RAW's nodes. These two public EWS
products carry the mapping ERCOT itself publishes, as zips of CSVs; the header picks
the table:

- ``sp_electrical_bus_mapping`` (NP4-160-SG): one row per electrical bus with its node
  name, PSS/E name and number, voltage level, substation, load zone, resource node and
  hub, where it belongs to one;
- ``sp_resource_node_units``, ``sp_noie_mapping``, ``sp_ccp_resource_names``,
  ``sp_hub_and_dc_tie_names`` (NP4-160-SG): the smaller lists in the same package;
- ``hub_buses`` (NP3-220-SG): the electrical buses that make up each hub, with their kV;
- ``electrical_buses`` (NP3-220-SG): the full list of electrical bus names.

Both products are posted whole, without an operating date; the listing's posting time
dates a document. ERCOT's one misspelt header is kept as the raw column
``logical_resource_node_name``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

from .table import Column, F, I, read_delimited, table_for_header

VERSION = 1

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


@dataclass(frozen=True)
class MappingMember:
    path: str
    format: str
    operating_date: None = None  # the package carries no date; the listing does
    hour: None = None

    @property
    def is_parsed(self) -> bool:
        return self.format == "csv"


def classify_member(path: str) -> MappingMember | None:
    if path.endswith("/"):
        return None
    return MappingMember(path, path.rsplit(".", 1)[-1].lower() if "." in path else "")


def parse_member(member: MappingMember, data: bytes) -> dict[str, pa.Table]:
    table = table_for_header(data, TABLES, "settlement point mappings")
    return {table: read_delimited(data, TABLES[table], table)}
