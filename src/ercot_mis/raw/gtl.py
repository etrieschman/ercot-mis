"""Generic Transmission Limits (NP3-766-M): one document per operating day.

ERCOT posts two document shapes under this product, both listed with an ``xls``
format. The daily GTL workbook is really an xlsx: one sheet, ``Results``, one row
per hour, a ``Time`` column and then two columns per GTC: the real-time limit under
the GTC's name and the day-ahead limit under ``<name>\\nDAM``. The other shape is a
genuine xls with a ``DC Limits`` sheet (CENACE DC tie limits); it is archived and
not parsed here.

The whole document is one member (there is no zip), so identity comes from the
catalog's listing (``operating_date``), not from a member name.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

import polars as pl
import pyarrow as pa

from ..clock import local_timestamps_expr
from .table import Member, ParseError, extension

VERSION = 3

_SHEET = "Results"


def classify_document(path: str, data: bytes, operating_date: date | None = None) -> Member:
    """The shape is in the bytes: the GTL workbook is a zip (xlsx), the DC-tie file is not. The listing's day is the rows' identity."""
    identity = {"operating_date": operating_date}
    if zipfile.is_zipfile(io.BytesIO(data)):
        return Member(path, "gtl_hourly", "xlsx", True, identity)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # OLE2 compound file: legacy xls
        return Member(path, "dc_tie_limits", "xls", False, identity)
    return Member(path, "unknown", extension(path), False, identity)


def parse_member(member: Member, data: bytes) -> dict[str, pa.Table]:
    return {"gtl_hourly": parse_gtl(data, f"GTL {member.identity['operating_date']}")}


def parse_gtl(data: bytes, label: str = "GTL") -> pa.Table:
    """Long table ``gtl_hourly``: (interval_start, interval_start_utc, delivery_date, hour_ending, gtc_name, market, limit_mw).

    ``interval_start`` is the workbook's naive local timestamp and ``hour_ending`` its clock
    hour plus one; ``interval_start_utc`` is the instant (``clock.local_timestamps_expr``),
    which is what core joins on.
    """
    try:
        frame = pl.read_excel(io.BytesIO(data), sheet_name=_SHEET, infer_schema_length=0)
    except Exception as error:
        raise ParseError(f"{label}: cannot read sheet {_SHEET!r}: {error}") from None
    columns = [str(c) for c in frame.columns]
    if not columns or columns[0].strip().lower() != "time":
        raise ParseError(f"{label}: first column should be 'Time', found {columns[:1]}")
    names, dams = {}, {}
    for column in columns[1:]:
        text = column.strip()
        if text.upper().endswith("DAM"):
            base = text[:-3].strip()
            dams[base] = column
        else:
            names[text] = column
    if set(dams) != set(names):
        raise ParseError(f"{label}: RT and DAM columns do not pair up ({len(names)} RT, {len(dams)} DAM)")
    if frame.is_empty():
        raise ParseError(f"{label}: no hourly rows")
    text = frame[columns[0]].cast(pl.String).str.strip_chars()
    start = text.str.to_datetime("%Y-%m-%d %H:%M:%S", strict=False)
    if start.null_count():
        raise ParseError(f"{label}: {start.null_count()} 'Time' values are not 'YYYY-MM-DD HH:MM:SS' timestamps")
    utc = pl.DataFrame({"t": start}).select(local_timestamps_expr(pl.col("t")).alias("u"))["u"]
    parts = []
    for name in names:
        for market, column in (("rt", names[name]), ("dam", dams[name])):
            parts.append(pl.DataFrame({
                "interval_start": start, "interval_start_utc": utc, "delivery_date": start.dt.date(), "hour_ending": start.dt.hour().cast(pl.Int64) + 1,
                "gtc_name": pl.Series([name] * frame.height, dtype=pl.String),
                "market": pl.Series([market] * frame.height, dtype=pl.String),
                "limit_mw": frame[column].cast(pl.String).str.strip_chars().cast(pl.Float64, strict=False),
            }))
    return pl.concat(parts).to_arrow()
