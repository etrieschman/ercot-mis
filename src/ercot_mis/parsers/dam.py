"""DAM network model packages (NP4-500-SG): one operating day, one model per hour.

Each package holds, per hour ``HHH`` (``001``-``024``; 23 on the short DST day, 25 on
the long one, where the extra hour is the third): ``DAMmmddyyyy_HHH.RAW`` (PSS/E v30,
blank-separated) and CSVs mapping the CIM model to that RAW, ``_Ctg_`` (contingencies),
``_Gn_`` (generators), ``_Hb_`` (hub buses), ``_Ld_`` (loads), ``_Ln_`` (lines), ``_Sp_``
(settlement points) and ``_Xf_`` (transformers). Once per day: ``_SpCtg`` (settlement
points a contingency disconnects), ``_SpNb`` (non-biddable resource nodes) and a README.

ERCOT's README states that the DAM solves a lossless DC power flow, that the RAW files
already include scheduled outages, and that all bus shunt, load and generator MW/MVAr
values in the RAW are zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import pyarrow as pa

from . import psse
from ._common import F, I, Column, read_delimited

_HOURLY = re.compile(r"^DAM(\d{8})_(?:([A-Za-z]+)_)?(\d{3})\.(raw|csv)$", re.IGNORECASE)
_DAILY = re.compile(r"^DAM(\d{8})_(SpCtg|SpNb)\.csv$", re.IGNORECASE)
_README = re.compile(r"^README_DAM(\d{8})\.txt$", re.IGNORECASE)

_KINDS = {
    None: "network_model", "ctg": "contingencies", "gn": "generators", "hb": "hub_buses",
    "ld": "loads", "ln": "lines", "sp": "settlement_points", "xf": "transformers",
    "spctg": "settlement_point_contingencies", "spnb": "non_biddable_resource_nodes",
}

HOUR = Column("Hour", I)
STATION = Column("Station Name/PSS/E Bus Name")

LINES = (
    HOUR, Column("PSS/E From Bus Number", I), Column("PSS/E To Bus Number", I), Column("PSS/E Ckt Id"),
    Column("Branch Status"), Column("Monitored?"), Column("Monitored and Secured?"),
    Column("From Station Name/PSS/E Bus Name"), Column("From PSS/E KV", F),
    Column("To Station Name/PSS/E Bus Name"), Column("To PSS/E KV", F), Column("Branch Name"),
    Column("r (p.u)", F), Column("x (p.u)", F), Column("b (p.u)", F),
    Column("RATEA", F), Column("RATEB", F), Column("RATEC", F),
)
TRANSFORMERS = (
    HOUR, Column("PSS/E From Bus Number", I), Column("PSS/E To Bus Number", I), Column("PSS/E Ckt Id"),
    Column("Transformer Status"), Column("Monitored?"), Column("Monitored and Secured?"),
    Column("From Station Name/PSS/E Bus Name"), Column("From PSS/E KV", F),
    Column("To Station Name/PSS/E Bus Name"), Column("To PSS/E KV", F), Column("Branch Name"),
    Column("r (p.u)", F), Column("x (p.u)", F), Column("Off Nominal Turns Ratio", F),
    Column("RATEA", F), Column("RATEB", F), Column("RATEC", F),
)
CONTINGENCIES = (
    HOUR, Column("Contingency Name"),
    Column("Equipment Type (Branch/Load/Generator/SettlementPoint)", name="equipment_type"),
    Column("Contingency Operation (Outage or Split Bus)", name="contingency_operation"),
    Column("PSS/E GenOrLoadOrSP Bus Number", I), Column("PSS/E GenOrLoad Id"),
    Column("Station Name/PSSE Bus Name"), Column("PSS/E KV", F),
    Column("PSS/E From Bus Number", I), Column("PSS/E To Bus Number", I), Column("PSS/E Ckt Id"),
    Column("From Station Name/PSS/E Bus Name"), Column("From PSS/E KV", F),
    Column("To Station Name/PSS/E Bus Name"), Column("To PSS/E KV", F),
    # The split-bus number columns hold either a bus number or a sentence-like note, so they stay text.
    Column("Split Bus (contingency) PSS/E Bus Number (From PSS/E Bus Number in case branches "
           "or PSS/E Bus number in case of Load and Generator)", name="split_bus_psse_bus_number"),
    Column("Split Bus (contingency) From Station Name/PSS/E Bus Name", name="split_bus_from_station_name_psse_bus_name"),
    Column("Split Bus (contingency) From PSS/E KV", F, "split_bus_from_psse_kv"),
    Column("Split Bus (contingency) PSS/E To Bus Number - Only for Branches", name="split_bus_psse_to_bus_number"),
    Column("Split Bus (contingency) To Station Name/PSS/E Bus Name", name="split_bus_to_station_name_psse_bus_name"),
    Column("Split Bus (contingency) To PSS/E KV", F, "split_bus_to_psse_kv"),
)
GENERATORS = (
    HOUR, Column("PSS/E Bus Number", I), Column("PSS/E Gen Id"), STATION, Column("PSS/E KV", F),
    Column("Generator Name"), Column("Generator Status"),
    Column("Resource Node Settlment Point Name", name="resource_node_settlement_point_name"),
    Column("Resource Node PSS/E Bus Number", I), Column("Resource Node Station Name/PSS/E Bus Name"),
    Column("Resource Node PSS/E KV", F), Column("Combined Cycle - Train Name"),
    Column("Combined Cycle - Logical Resource Node Settlement Point Name"),
    Column("Generator in Load Zone"), Column("Resource Node in Load Zone"),
    Column("DC Tie (if applicable)", name="dc_tie"),
)
_ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th")
LOADS = (
    HOUR, Column("PSS/E Bus Number", I), Column("PSS/E Load Id"), STATION, Column("PSS/E KV", F),
    Column("Load Name"), Column("Load Status"), Column("Weather Zone Name"), Column("Load Zone Name"),
    Column("Ercot Load?"), Column("Conforming or NonConforming"), Column("Raw MW LDF", F),
    Column("Raw MVAr LDF", F), Column("Load Rollover Capable?"), Column("Number of Target Loads", I),
) + tuple(
    column for n in _ORDINALS
    for column in (Column(f"{n} Target Load Name"), Column(f"Fraction of this Load to {n} Target Load", F))
)
SETTLEMENT_POINTS = (
    HOUR, Column("Settlement Point Name"), Column("Settlement Point Type"), Column("Status"),
    Column("Number of energized components - (i)Buses (Hub & PUN Resource Nodes) (ii)Loads (LZ) "
           "(iii)Generators (Resource Nodes & Logical Resource Nodes)", I, "number_of_energized_components"),
    Column("PSS/E Bus Number", I), STATION, Column("PSS/E KV", F), Column("Combined Cycle Train Name"),
    Column("Combined Cycle Settlement Point"),
    Column("Load Zone (for Resource Node & PUN Resource Node)", name="load_zone"),
)
HUB_BUSES = (
    HOUR, Column("PSS/E Bus Number", I), STATION, Column("PSS/E KV", F), Column("Bus Status"),
    Column("Hub Bus Name"), Column("Hub Name"),
)
SETTLEMENT_POINT_CONTINGENCIES = (
    HOUR, Column("Contingency Name"),
    Column("Contingency Operation (Outage or Split Bus)", name="contingency_operation"),
    Column("PSS/E Bus Number", I), Column("Station Name/PSSE Bus Name"), Column("PSS/E KV", F),
    Column("Settlement Point Name"),
)
NON_BIDDABLE_RESOURCE_NODES = (Column("Resource Node"),)

_CSV_COLUMNS = {
    "contingencies": CONTINGENCIES, "generators": GENERATORS, "hub_buses": HUB_BUSES, "loads": LOADS,
    "lines": LINES, "settlement_points": SETTLEMENT_POINTS, "transformers": TRANSFORMERS,
    "settlement_point_contingencies": SETTLEMENT_POINT_CONTINGENCIES,
    "non_biddable_resource_nodes": NON_BIDDABLE_RESOURCE_NODES,
}


@dataclass(frozen=True)
class DamMember:
    path: str
    kind: str  # "network_model", "lines", ...; "readme"; "unknown"
    format: str
    operating_date: date | None
    hour: int | None  # 1-based study hour; None for once-a-day files

    @property
    def is_parsed(self) -> bool:
        return self.kind == "network_model" or self.kind in _CSV_COLUMNS


def _operating_date(mmddyyyy: str) -> date:
    return date(int(mmddyyyy[4:]), int(mmddyyyy[:2]), int(mmddyyyy[2:4]))


def classify_member(path: str) -> DamMember | None:
    """Describe a zip member of a DAM package from its name; None for folders."""
    if path.endswith("/"):
        return None
    file = path.rsplit("/", 1)[-1]
    fmt = file.rsplit(".", 1)[-1].lower() if "." in file else ""
    if hourly := _HOURLY.match(file):
        token = hourly.group(2).lower() if hourly.group(2) else None
        kind = _KINDS.get(token, "unknown")
        if (kind == "network_model") != (fmt == "raw"):
            kind = "unknown"
        return DamMember(path, kind, fmt, _operating_date(hourly.group(1)), int(hourly.group(3)))
    if daily := _DAILY.match(file):
        return DamMember(path, _KINDS[daily.group(2).lower()], fmt, _operating_date(daily.group(1)), None)
    if readme := _README.match(file):
        return DamMember(path, "readme", fmt, _operating_date(readme.group(1)), None)
    return DamMember(path, "unknown", fmt, None, None)


def parse_member(member: DamMember, data: bytes) -> dict[str, pa.Table]:
    """Parse one member into raw tables; members without a parser return no tables."""
    label = f"DAM {member.kind} hour {member.hour}" if member.hour else f"DAM {member.kind}"
    if member.kind == "network_model":
        return psse.parse_raw(data, label).tables
    if member.kind in _CSV_COLUMNS:
        return {f"dam_{member.kind}": read_delimited(data, _CSV_COLUMNS[member.kind], label)}
    return {}
