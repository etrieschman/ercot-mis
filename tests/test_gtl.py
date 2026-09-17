import io

import polars as pl
import pytest
from openpyxl import Workbook

from ercot_mis.raw import ParseError, gtl


def _workbook(headers, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "Results"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_gtl_workbook_becomes_a_long_table():
    data = _workbook(["Time", "GTC_A", "GTC_A\nDAM", "GTC_B", "GTC_B\nDAM"],
                     [["2026-09-15 00:00:00", 500, 450, "", 800], ["2026-09-15 01:00:00", 510, 460, 900, 810]])
    member = gtl.classify_document("doc.xls", data)
    assert member.kind == "gtl_hourly" and member.is_parsed
    table = pl.from_arrow(gtl.parse_gtl(data))
    assert table.columns == ["interval_start", "delivery_date", "hour_ending", "gtc_name", "market", "limit_mw"] and table.height == 8
    row = table.filter((pl.col("gtc_name") == "GTC_A") & (pl.col("market") == "dam") & (pl.col("hour_ending") == 2)).to_dicts()[0]
    assert row["limit_mw"] == 460.0 and str(row["delivery_date"]) == "2026-09-15"
    assert table.filter((pl.col("gtc_name") == "GTC_B") & (pl.col("market") == "rt") & (pl.col("hour_ending") == 1))["limit_mw"][0] is None


def test_gtl_structure_errors():
    with pytest.raises(ParseError, match="do not pair up"):
        gtl.parse_gtl(_workbook(["Time", "GTC_A", "GTC_B\nDAM"], [["2026-09-15 00:00:00", 1, 2]]))
    with pytest.raises(ParseError, match="'Time'"):
        gtl.parse_gtl(_workbook(["Hour", "GTC_A", "GTC_A\nDAM"], [["2026-09-15 00:00:00", 1, 2]]))
    with pytest.raises(ParseError, match="timestamps"):
        gtl.parse_gtl(_workbook(["Time", "GTC_A", "GTC_A\nDAM"], [[1, 1, 2]]))
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
    assert gtl.classify_document("doc.xls", ole).kind == "dc_tie_limits"
    assert not gtl.classify_document("doc.xls", ole).is_parsed
