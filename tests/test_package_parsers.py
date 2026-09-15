import io
from datetime import date

import pytest

from ercot_mis.parsers import ParseError, crr, dam, snake_case


def test_snake_case():
    assert snake_case("PSS/E From Bus Number") == "psse_from_bus_number"
    assert snake_case("BaseCaseRating") == "base_case_rating"
    assert snake_case("r (p.u)") == "r_pu"
    assert snake_case("Monitored?") == "monitored"
    assert snake_case("From #") == "from_number"
    assert snake_case("Conforming or NonConforming") == "conforming_or_non_conforming"
    assert snake_case("Raw MVAr LDF") == "raw_mvar_ldf"


# ------------------------------------------------------------------ CRR


@pytest.mark.parametrize(
    "path, expected",
    [
        ("2029.1st6.AnnualAuction.Seq6.JAN/2029.1st6.AnnualAuction.Seq6.Common_NetworkModel_JAN_2029_PeakWD.RAW",
         ("network_model", "raw", "annual", "2029.1st6", 6, date(2029, 1, 1), "PeakWD")),
        ("2029.1st6.AnnualAuction.Seq6.FEB_Upd/2029.1st6.AnnualAuction.Seq6.Common_SourcesAndSinks_FEB_2029_Upd1.CSV",
         ("sources_and_sinks", "csv", "annual", "2029.1st6", 6, date(2029, 2, 1), None)),
        ("2029.1st6.AnnualAuction.Seq6.Mapping_Documents/2029.1st6.AnnualAuction.Seq6.MappingDocument_MAR_2029.xlsx",
         ("mapping_document", "xlsx", "annual", "2029.1st6", 6, date(2029, 3, 1), None)),
        ("2026.OCT.Monthly.Auction.NetworkModel_PeakWD.raw",
         ("network_model", "raw", "monthly", None, None, date(2026, 10, 1), "PeakWD")),
        ("2026.OCT.Monthly.Auction.Non-ThermalConstraints.XML",
         ("non_thermal_constraints", "xml", "monthly", None, None, date(2026, 10, 1), None)),
        ("KML_Readme.txt", ("readme", "txt", None, None, None, None, None)),
        ("2026.OCT.Monthly.Auction.Something_New.csv", ("unknown", "csv", "monthly", None, None, date(2026, 10, 1), None)),
    ],
)
def test_classify_crr_members(path, expected):
    m = crr.classify_member(path)
    assert (m.kind, m.format, m.auction, m.term, m.sequence, m.month, m.time_of_use) == expected
    assert crr.classify_member("2029.1st6.AnnualAuction.Seq6.JAN/") is None


def test_crr_csvs():
    member = crr.classify_member("2026.OCT.Monthly.Auction.MonitoredLinesAndTransformers.CSV")
    data = b"DeviceName,DeviceType,BaseCaseRating,EmergencyRating,TimeOfUse\r\nA LINE 1,Line,100.5,120,PeakWD\r\nB XF 1,XFMR,50,,Off-peak\r\n"
    table = crr.parse_member(member, data)["crr_monitored_lines_and_transformers"]
    assert table.column_names == ["device_name", "device_type", "base_case_rating", "emergency_rating", "time_of_use"]
    assert table["base_case_rating"].to_pylist() == [100.5, 50.0]
    assert table["emergency_rating"].to_pylist() == [120.0, None]

    gtc = crr.classify_member("2026.OCT.Monthly.Auction.Non-ThermalConstraints.CSV")
    data = b"Name, Limit, DeviceName, DeviceType, FlowDirection, Factor\nGTC_A, 1500, A LINE 1, Line, From-To, 1\n"
    assert crr.parse_member(gtc, data)["crr_non_thermal_constraints"].to_pylist() == [
        {"name": "GTC_A", "limit": 1500.0, "device_name": "A LINE 1", "device_type": "Line",
         "flow_direction": "From-To", "factor": 1.0}]

    with pytest.raises(ParseError, match="header does not match"):
        crr.parse_member(member, b"DeviceName,Type\nA,Line\n")


def test_crr_mapping_workbook_and_outages():
    from openpyxl import Workbook

    book = Workbook()
    lines = book.active
    lines.title = "Lines"
    lines.append([c.header for c in crr.MAPPING_LINES])
    lines.append(["TAG_A", "OPS_A", "EQ", "LN", "EQN", 101, "ALPHA", 102, "BRAVO", "1"])
    autos = book.create_sheet("Autos")
    autos.append([c.header for c in crr.MAPPING_AUTOS])
    autos.append(["AUTO_A", "OPS_B", "PT", 103, "CHARLIE", 104, "DELTA", "T1"])
    buffer = io.BytesIO()
    book.save(buffer)

    member = crr.classify_member("2026.OCT.Monthly.Auction.MappingDocument.xlsx")
    tables = crr.parse_member(member, buffer.getvalue())
    assert tables["crr_mapping_lines"].to_pylist()[0]["from_number"] == "101"
    assert tables["crr_mapping_autos"].column_names[-1] == "id"

    outages = crr.classify_member("2026.OCT.Monthly.Auction.Outages_OCT_2026_OCT01.txt")
    header = "|".join(c.header for c in crr.OUTAGES)
    row = "|".join(["_{X}", "", "10/1/2026 00:00:00"] + ["v"] * (len(crr.OUTAGES) - 3))
    table = crr.parse_member(outages, f"{header}\n{row}\n".encode())["crr_outages"]
    assert table.num_rows == 1 and table["actual_end_date"].to_pylist() == [None]
    assert crr.parse_member(outages, f"{header}\n".encode())["crr_outages"].num_rows == 0
    comma_header = ",".join(c.header for c in crr.OUTAGES)  # annual "_None" files
    assert crr.parse_member(outages, f"{comma_header}\r\n".encode())["crr_outages"].num_rows == 0


# ------------------------------------------------------------------ DAM


def test_classify_dam_members():
    raw = dam.classify_member("DAM09162026_001.RAW")
    assert (raw.kind, raw.format, raw.operating_date, raw.hour) == ("network_model", "raw", date(2026, 9, 16), 1)
    assert dam.classify_member("DAM09162026_Ln_025.csv").hour == 25
    assert dam.classify_member("DAM09162026_SpCtg.csv").kind == "settlement_point_contingencies"
    assert dam.classify_member("DAM09162026_SpNb.csv").hour is None
    assert dam.classify_member("README_DAM09162026.txt").kind == "readme"
    assert dam.classify_member("DAM09162026_Zz_001.csv").kind == "unknown"


def test_dam_lines_and_loads():
    header = ", ".join(c.header for c in dam.LINES)
    row = "1, 101, 102, 1 , IN, Y, N, ALPHA, 138, BRAVO, 138, LINE_A, 0.001, 0.01, 0.02, 100, 110, 120"
    table = dam.parse_member(dam.classify_member("DAM09162026_Ln_001.csv"), f"{header}\n{row}\n".encode())["dam_lines"]
    record = table.to_pylist()[0]
    assert (record["hour"], record["psse_from_bus_number"], record["psse_ckt_id"], record["x_pu"], record["ratec"]) == (1, 101, "1", 0.01, 120.0)

    # The load file's header ends with a trailing comma its rows do not have.
    header = ",".join(c.header for c in dam.LOADS) + ","
    row = ",".join(["1", "101", "L1", "ALPHA", "138", "LOAD_A", "IN", "WZ", "LZ", "Y", "C", "0.5", "0.1", "N", "1", "LOAD_B", "1.0"] + [""] * 18)
    table = dam.parse_member(dam.classify_member("DAM09162026_Ld_001.csv"), f"{header}\n{row}\n".encode())["dam_loads"]
    assert table.num_columns == 35
    assert table.to_pylist()[0]["fraction_of_this_load_to_1st_target_load"] == 1.0
    assert table.to_pylist()[0]["10th_target_load_name"] is None
