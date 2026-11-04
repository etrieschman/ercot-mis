"""CRR network model packages (NP7-801-M annual, NP7-800-M monthly) as the ``crr_*`` and ``psse_*`` tables.

A package holds, per month, a PSS/E RAW, CSVs of contingencies, monitored elements, generic
transmission constraints, sources and sinks and outages, and a mapping workbook. The CSVs
carry the same values as their XML twins, so the XML is archived and not parsed
(docs/datasets/crr-network-model.md).
"""

from __future__ import annotations

VERSION = 1  # bump when the tables change; every artifact built with an older version is rebuilt

import re
from datetime import date

import pyarrow as pa

from . import psse_raw
from .columns import F, Column, Member, extension, read_delimited, read_sheet

MONTHS = {m: n for n, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}

_ANNUAL = re.compile(r"(\d{4}\.\d(?:st|nd)\d)\.AnnualAuction\.Seq(\d+)\.", re.IGNORECASE)
_MONTHLY = re.compile(r"(\d{4})\.([A-Z]{3})\.Monthly\.Auction\.", re.IGNORECASE)
_MEMBER_MONTH = re.compile(r"_([A-Z]{3})_(\d{4})(?=[._])", re.IGNORECASE)
_TOU = re.compile(r"_(PeakWD|PeakWE|Off-?peak)(?=[._])", re.IGNORECASE)

# File-name token to kind; the first token found wins.
_KINDS = (
    ("NetworkModel", "network_model"),
    ("MonitoredLinesAndTransformers", "monitored_lines_and_transformers"),
    ("Non-ThermalConstraints", "non_thermal_constraints"),
    ("SourcesAndSinks", "sources_and_sinks"),
    ("Contingencies", "contingencies"),
    ("MappingDocument", "mapping_document"),
    ("DynamicRatings", "dynamic_ratings"),
    ("Outages", "outages"),
    ("OneLineDiagram", "one_line_diagram"),
    ("StationOneLines", "station_one_lines"),
    ("Station_OneLines", "station_one_lines"),
    ("KML_Readme", "readme"),
)

CONTINGENCIES = (Column("Contingency"), Column("DeviceName"), Column("DeviceType"), Column("Action"))
MONITORED_LINES_AND_TRANSFORMERS = (
    Column("DeviceName"), Column("DeviceType"), Column("BaseCaseRating", F),
    Column("EmergencyRating", F), Column("TimeOfUse"),
)
NON_THERMAL_CONSTRAINTS = (
    Column("Name"), Column("Limit", F), Column("DeviceName"), Column("DeviceType"),
    Column("FlowDirection"), Column("Factor", F),
)
SOURCES_AND_SINKS = (Column("Name"), Column("PriceNode"), Column("BusName"), Column("ParticipationFactor", F))
# Number cells in the mapping workbook are not all numeric, so they stay text.
MAPPING_LINES = (
    Column("CRR_Tag"), Column("Operations_Name"), Column("OP_EQCODE"), Column("LNNAME"),
    Column("EQNAME"), Column("From #"), Column("From Name"), Column("To #"),
    Column("To Name"), Column("Circuit ID"),
)
MAPPING_AUTOS = (
    Column("CRR Name"), Column("Operations_Name"), Column("PTNAME"), Column("From #"),
    Column("From Name"), Column("To #"), Column("To Name"), Column("ID"),
)
OUTAGES = tuple(Column(h) for h in (
    "RDFID", "ACTUAL_END_DATE", "ACTUAL_START_DATE", "CATEGORY", "EQUIPMENT_FROM_STAT_NAME_ACRN",
    "EQUIPMENT_FROM_STATION_NAME", "EQUIPMENT_HI_SUSTAINED_LIMIT", "EQUIPMENT_NAME",
    "EQUIPMENT_NORMAL_STATE", "EQUIPMENT_OUTAGE_STATE", "EQUIPMENT_TO_STAT_NAME_ACRN",
    "EQUIPMENT_TO_STATION_NAME", "EQUIPMENT_TYPE", "IDENTIFIER", "NEW_PLANNED_END_DATE",
    "NEW_PLANNED_START_DATE", "PLANNED_END_DATE", "PLANNED_START_DATE", "PROJECT_NAME",
    "REQUESTOR_ORG_NAME", "STATUS", "TYPE_ACRONYM", "VOLTAGE_LEVEL", "EMS_IMPORT_INHIBIT",
    "LATEST_END_DATE", "NATURE_OF_WORK", "OUTAGE_GROUP_LABEL", "TYPE",
))

# The (kind, format) pairs with a parser; every other member is archived only.
PARSED = {
    ("network_model", "raw"), ("contingencies", "csv"), ("monitored_lines_and_transformers", "csv"),
    ("non_thermal_constraints", "csv"), ("sources_and_sinks", "csv"), ("mapping_document", "xlsx"),
    ("outages", "txt"),
}


def classify_member(path: str) -> Member | None:
    """The member for a zip entry of a CRR package, from its path; None for a folder."""
    if path.endswith("/"):
        return None
    file = path.rsplit("/", 1)[-1]
    fmt = extension(file)
    kind = next((k for token, k in _KINDS if token.lower() in file.lower()), "unknown")
    auction = term = sequence = month = None
    if annual := _ANNUAL.search(file):
        auction, term, sequence = "annual", annual.group(1), int(annual.group(2))
        found = _MEMBER_MONTH.search(file)
        if found and found.group(1).upper() in MONTHS:
            month = date(int(found.group(2)), MONTHS[found.group(1).upper()], 1)
    elif monthly := _MONTHLY.search(file):
        if monthly.group(2).upper() in MONTHS:
            auction, month = "monthly", date(int(monthly.group(1)), MONTHS[monthly.group(2).upper()], 1)
    tou = _TOU.search(file)
    return Member(path, kind, fmt, (kind, fmt) in PARSED, {"auction": auction, "term": term, "sequence": sequence, "month": month,
                                                         "time_of_use": tou.group(1) if tou else None})


def parse_member(member: Member, data: bytes) -> dict[str, pa.Table]:
    """The raw tables of one member; none for a member without a parser."""
    label = f"CRR {member.kind}.{member.format}"
    match (member.kind, member.format):
        case ("network_model", "raw"):
            return psse_raw.parse_raw(data, label).tables
        case ("contingencies", "csv"):
            return {"crr_contingencies": read_delimited(data, CONTINGENCIES, label)}
        case ("monitored_lines_and_transformers", "csv"):
            return {"crr_monitored_lines_and_transformers": read_delimited(data, MONITORED_LINES_AND_TRANSFORMERS, label)}
        case ("non_thermal_constraints", "csv"):
            return {"crr_non_thermal_constraints": read_delimited(data, NON_THERMAL_CONSTRAINTS, label)}
        case ("sources_and_sinks", "csv"):
            return {"crr_sources_and_sinks": read_delimited(data, SOURCES_AND_SINKS, label)}
        case ("mapping_document", "xlsx"):
            return {
                "crr_mapping_lines": read_sheet(data, "Lines", MAPPING_LINES, label),
                "crr_mapping_autos": read_sheet(data, "Autos", MAPPING_AUTOS, label),
            }
        case ("outages", "txt"):
            # A monthly outage file is pipe-delimited; an annual one is a comma-separated header alone.
            delimiter = "|" if b"|" in data.partition(b"\n")[0] else ","
            return {"crr_outages": read_delimited(data, OUTAGES, label, delimiter=delimiter, quoting=False)}
    return {}
