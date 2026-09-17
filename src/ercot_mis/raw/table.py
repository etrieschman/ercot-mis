"""Shared parsing pieces: column specs, casting, and delimited-file reading."""

from __future__ import annotations

VERSION = 1  # bump when reading or casting changes what every parser produces

import io
import re
from collections.abc import Sequence
from typing import NamedTuple

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

I, F, S = pa.int64(), pa.float64(), pa.string()


class ParseError(ValueError):
    """A source file lacks the structure its parser expects. The message says where."""


class Column(NamedTuple):
    header: str
    type: pa.DataType = S
    name: str | None = None  # defaults to snake_case(header)

    @property
    def column_name(self) -> str:
        return self.name or snake_case(self.header)


def snake_case(header: str) -> str:
    """``PSS/E From Bus Number`` -> ``psse_from_bus_number``; ``BaseCaseRating`` -> ``base_case_rating``."""
    text = header.strip().replace("PSS/E", "psse").replace("PSSE", "psse").replace("p.u", "pu")
    text = text.replace("&", " and ").replace("#", " number ")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()


def cast_column(values: pa.Array | pa.ChunkedArray, type: pa.DataType, label: str):
    """Cast text to ``type``. Integers written as ``3.0`` are accepted when they are whole."""
    if type == S:
        return pc.cast(values, S)
    try:
        return pc.cast(values, type)
    except pa.ArrowInvalid:
        if pa.types.is_integer(type):
            try:
                floats = pc.cast(values, F)
                if pc.all(pc.equal(pc.floor(floats), floats)).as_py() in (True, None):
                    return pc.cast(floats, type)
            except pa.ArrowInvalid:
                pass
    raise ParseError(f"{label}: values cannot be read as {type}")


def finish_text_table(table: pa.Table, columns: Sequence[Column], label: str) -> pa.Table:
    """Trim text, turn empty strings into nulls, and cast each column to its spec's type."""
    arrays = []
    for column, source in zip(columns, table.columns):
        text = pc.utf8_trim_whitespace(pc.cast(source, S))
        text = pc.if_else(pc.equal(text, ""), pa.scalar(None, S), text)
        arrays.append(cast_column(text, column.type, f"{label}.{column.column_name}"))
    return pa.table(arrays, names=[c.column_name for c in columns])


def empty_table(columns: Sequence[Column]) -> pa.Table:
    return pa.table({c.column_name: pa.array([], c.type) for c in columns})


def read_delimited(
    data: bytes,
    columns: Sequence[Column],
    label: str,
    *,
    delimiter: str = ",",
    quoting: bool = True,
) -> pa.Table:
    """Read a delimited file whose header must match ``columns`` exactly (after trimming).

    Tolerates a byte-order mark, CRLF line endings, and a trailing delimiter on the
    header line (ERCOT's DAM load file has one).
    """
    data = data.removeprefix(b"\xef\xbb\xbf")
    first, _, rest = data.partition(b"\n")
    header = [h.strip() for h in first.decode("utf-8", "replace").rstrip("\r").split(delimiter)]
    expected = [c.header for c in columns]
    if len(header) == len(expected) + 1 and header[-1] == "":
        header = header[:-1]
    if header != expected:
        raise ParseError(f"{label}: header does not match; expected {expected}, found {header}")
    if not rest.strip():
        return empty_table(columns)

    names = [c.column_name for c in columns]
    sample = rest.partition(b"\n")[0].rstrip(b"\r")
    extra = max(0, sample.count(delimiter.encode()) + 1 - len(names))
    read_names = names + [f"_extra_{k}" for k in range(extra)]
    try:
        table = pacsv.read_csv(
            io.BytesIO(data),
            read_options=pacsv.ReadOptions(column_names=read_names, skip_rows=1),
            parse_options=pacsv.ParseOptions(delimiter=delimiter, quote_char='"' if quoting else False),
            convert_options=pacsv.ConvertOptions(
                column_types={n: S for n in read_names},
                strings_can_be_null=False,
                include_columns=names,
            ),
        )
    except pa.ArrowInvalid as error:
        raise ParseError(f"{label}: {error}") from None
    return finish_text_table(table, columns, label)


def read_sheet(data: bytes, sheet: str, columns: Sequence[Column], label: str) -> pa.Table:
    """Read one worksheet whose header row must match ``columns`` exactly."""
    import polars as pl

    try:
        frame = pl.read_excel(io.BytesIO(data), sheet_name=sheet, infer_schema_length=0)
    except Exception as error:  # fastexcel raises its own types for a missing sheet
        raise ParseError(f"{label}: cannot read sheet {sheet!r}: {error}") from None
    header = [str(h).strip() for h in frame.columns]
    expected = [c.header for c in columns]
    if header != expected:
        raise ParseError(f"{label} [{sheet}]: header does not match; expected {expected}, found {header}")
    return finish_text_table(frame.to_arrow(), columns, f"{label} [{sheet}]")
