"""PSS/E version 30 RAW files, in both dialects ERCOT publishes.

**CRR network models** (exported by PSS/ODMS) separate fields with commas, close each
section with ``0 / END OF <X> DATA, BEGIN <Y> DATA``, follow most records with a
``/*[...]*/`` comment holding the element's ERCOT name, and use CRLF line endings.

**DAM network models** (written by the DAM study) separate fields with blanks, close
each section with a bare ``0``, carry no comments, and omit the VSC DC section.

Field layouts are the same, so one tokenizer reads both: a token is a single-quoted
string or a run of characters other than blanks, commas and quotes, and ``/`` outside
quotes starts a comment. Section names come from the ``END OF`` comments when present,
and otherwise from v30 section order.

Output tables are ``psse_case``, ``psse_bus``, ``psse_load``, ``psse_generator``,
``psse_branch``, ``psse_transformer``, ``psse_area``, ``psse_switched_shunt``,
``psse_impedance_correction``, ``psse_zone`` and ``psse_owner``. Column names follow
the PSS/E manual (``i``, ``j``, ``ckt``, ``ratea``...), quoted strings are trimmed of
PSS/E's padding, and every table except ``psse_case`` ends with ``comment``. Sections
ERCOT leaves empty (DC lines, FACTS devices, multi-section lines, inter-area
transfers) and 3-winding transformers raise ``ParseError`` if they ever hold records.
"""

from __future__ import annotations

# Bump when the tables this parser produces change (columns, types, values). The raw
# layer's cache key includes it, so every artifact built with an older version is rebuilt.
VERSION = 1

import re
from dataclasses import dataclass

import pyarrow as pa

from .table import F, I, S, ParseError, cast_column

_LINE = re.compile(r"^((?:[^'/]|'[^']*')*)(?:/(.*))?$")
_TOKEN = re.compile(r"'([^']*)'|([^\s,']+)")
_END = re.compile(r"END\s+OF\s+(.+?)\s+DATA", re.IGNORECASE)

ORDER = (
    "bus", "load", "generator", "branch", "transformer", "area", "two_terminal_dc",
    "vsc_dc_line", "switched_shunt", "impedance_correction", "multi_terminal_dc",
    "multi_section_line", "zone", "inter_area_transfer", "owner", "facts_device",
)
_MARKERS = {
    "BUS": "bus", "LOAD": "load", "GENERATOR": "generator", "BRANCH": "branch",
    "TRANSFORMER": "transformer", "AREA": "area", "AREA INTERCHANGE": "area",
    "TWO-TERMINAL DC": "two_terminal_dc", "VSC DC LINE": "vsc_dc_line",
    "SWITCHED SHUNT": "switched_shunt", "IMPEDANCE CORRECTION": "impedance_correction",
    "MULTI-TERMINAL DC": "multi_terminal_dc", "MULTI-SECTION LINE": "multi_section_line",
    "ZONE": "zone", "INTER-AREA TRANSFER": "inter_area_transfer", "OWNER": "owner",
    "FACTS DEVICE": "facts_device", "FACTS CONTROL DEVICE": "facts_device",
}

Layout = tuple[tuple[str, pa.DataType], ...]


def _owners() -> Layout:
    return tuple(field for k in range(1, 5) for field in ((f"o{k}", I), (f"f{k}", F)))


LAYOUTS: dict[str, Layout] = {
    "bus": (("i", I), ("name", S), ("basekv", F), ("ide", I), ("gl", F), ("bl", F),
            ("area", I), ("zone", I), ("vm", F), ("va", F), ("owner", I)),
    "load": (("i", I), ("id", S), ("status", I), ("area", I), ("zone", I), ("pl", F),
             ("ql", F), ("ip", F), ("iq", F), ("yp", F), ("yq", F), ("owner", I)),
    "generator": (("i", I), ("id", S), ("pg", F), ("qg", F), ("qt", F), ("qb", F), ("vs", F),
                  ("ireg", I), ("mbase", F), ("zr", F), ("zx", F), ("rt", F), ("xt", F),
                  ("gtap", F), ("stat", I), ("rmpct", F), ("pt", F), ("pb", F)) + _owners(),
    "branch": (("i", I), ("j", I), ("ckt", S), ("r", F), ("x", F), ("b", F), ("ratea", F),
               ("rateb", F), ("ratec", F), ("gi", F), ("bi", F), ("gj", F), ("bj", F),
               ("st", I), ("len", F)) + _owners(),
    "area": (("i", I), ("isw", I), ("pdes", F), ("ptol", F), ("arname", S)),
    "switched_shunt": (("i", I), ("modsw", I), ("vswhi", F), ("vswlo", F), ("swrem", I),
                       ("rmpct", F), ("rmidnt", S), ("binit", F))
                      + tuple(f for k in range(1, 9) for f in ((f"n{k}", I), (f"b{k}", F))),
    "impedance_correction": (("i", I),) + tuple(f for k in range(1, 12) for f in ((f"t{k}", F), (f"f{k}", F))),
    "zone": (("i", I), ("zoname", S)),
    "owner": (("i", I), ("owname", S)),
}

# A two-winding transformer is four lines.
TRANSFORMER_LINES: tuple[Layout, ...] = (
    (("i", I), ("j", I), ("k", I), ("ckt", S), ("cw", I), ("cz", I), ("cm", I), ("mag1", F),
     ("mag2", F), ("nmetr", I), ("name", S), ("stat", I)) + _owners(),
    (("r1_2", F), ("x1_2", F), ("sbase1_2", F)),
    (("windv1", F), ("nomv1", F), ("ang1", F), ("rata1", F), ("ratb1", F), ("ratc1", F),
     ("cod1", I), ("cont1", I), ("rma1", F), ("rmi1", F), ("vma1", F), ("vmi1", F),
     ("ntp1", I), ("tab1", I), ("cr1", F), ("cx1", F)),
    (("windv2", F), ("nomv2", F)),
)

Row = tuple[int, str, str | None]  # (line number, record text, comment)


@dataclass(frozen=True)
class PsseCase:
    dialect: str  # "marked": sections named by END OF comments; "bare": bare 0 separators
    tables: dict[str, pa.Table]
    line_counts: dict[str, int]  # data lines per section, for validating record counts


def parse_raw(data: bytes, label: str = "RAW") -> PsseCase:
    """Parse a PSS/E v30 RAW file in either ERCOT dialect."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    lines = text.splitlines()
    if len(lines) < 3:
        raise ParseError(f"{label}: needs three case header lines")

    sections: list[tuple[str | None, list[Row]]] = []
    current: list[Row] = []
    for number, line in enumerate(lines[3:], start=4):
        match = _LINE.match(line)
        if match is None:
            raise ParseError(f"{label}: line {number} has an unbalanced quote")
        body, comment = match.groups()
        stripped = body.strip()
        if stripped == "0":
            found = _END.search(comment or "")
            sections.append((_section_name(found.group(1), label, number) if found else None, current))
            current = []
        elif stripped.upper() == "Q":
            break
        elif stripped:
            current.append((number, body, comment))
    if current:
        raise ParseError(f"{label}: {len(current)} records after the last section separator")

    names = _resolve_names(sections, label)
    tables = {"psse_case": _case_table(lines[:3], label)}
    counts = {}
    for name, (_, rows) in zip(names, sections):
        counts[name] = len(rows)
        if name == "transformer":
            tables["psse_transformer"] = _transformers(rows, label)
        elif name in LAYOUTS:
            tables[f"psse_{name}"] = _table(rows, LAYOUTS[name], f"{label} {name}")
        elif rows:
            raise ParseError(f"{label}: the {name} section has {len(rows)} records, and there is no layout for it yet")
    marked = any(name is not None for name, _ in sections)
    return PsseCase("marked" if marked else "bare", tables, counts)


def _section_name(text: str, label: str, number: int) -> str:
    key = " ".join(text.upper().split())
    if key not in _MARKERS:
        raise ParseError(f"{label}: line {number} closes an unknown section {key!r}")
    return _MARKERS[key]


def _resolve_names(sections: list[tuple[str | None, list[Row]]], label: str) -> list[str]:
    named = [name for name, _ in sections]
    if any(name is not None for name in named):
        if None in named:
            raise ParseError(f"{label}: mixes named and bare section separators")
        if len(set(named)) != len(named):
            raise ParseError(f"{label}: a section appears twice")
        return named  # type: ignore[return-value]
    if len(sections) == len(ORDER):
        return list(ORDER)
    if len(sections) == len(ORDER) - 1:
        return [name for name in ORDER if name != "vsc_dc_line"]
    raise ParseError(
        f"{label}: {len(sections)} bare section separators; v30 has 16, or 15 without the VSC DC section"
    )


def _tokens(body: str) -> list[str]:
    return [bare if bare else quoted.strip() for quoted, bare in _TOKEN.findall(body)]


def _comment(comment: str | None) -> str | None:
    if not comment:
        return None
    text = comment.strip().removeprefix("*").removesuffix("*/").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return text.strip() or None


def _arrays(token_rows: list[list[str]], layout: Layout, label: str, numbers: list[int]) -> list:
    width = len(layout)
    padded = []
    for number, tokens in zip(numbers, token_rows):
        if len(tokens) > width:
            raise ParseError(f"{label}: line {number} has {len(tokens)} fields; the v30 layout has {width}")
        padded.append(tokens + [None] * (width - len(tokens)))
    columns = list(zip(*padded)) if padded else [()] * width
    return [
        cast_column(pa.array(list(values), S), type, f"{label}.{name}")
        for values, (name, type) in zip(columns, layout)
    ]


def _table(rows: list[Row], layout: Layout, label: str) -> pa.Table:
    numbers = [number for number, _, _ in rows]
    arrays = _arrays([_tokens(body) for _, body, _ in rows], layout, label, numbers)
    comments = pa.array([_comment(comment) for _, _, comment in rows], S)
    return pa.table(arrays + [comments], names=[name for name, _ in layout] + ["comment"])


def _transformers(rows: list[Row], label: str) -> pa.Table:
    parts: list[list[list[str]]] = [[], [], [], []]
    numbers: list[list[int]] = [[], [], [], []]
    comments = []
    index = 0
    while index < len(rows):
        number, body, comment = rows[index]
        first = _tokens(body)
        if len(first) < 3:
            raise ParseError(f"{label} transformer: line {number} is too short to start a record")
        if first[2] not in ("0", "0.0"):
            raise ParseError(
                f"{label} transformer: line {number} starts a 3-winding transformer (K != 0), which is not supported yet"
            )
        if index + 4 > len(rows):
            raise ParseError(f"{label} transformer: the record at line {number} is cut short")
        for part in range(4):
            line_number, line_body, _ = rows[index + part]
            parts[part].append(first if part == 0 else _tokens(line_body))
            numbers[part].append(line_number)
        comments.append(_comment(comment))
        index += 4

    arrays, names = [], []
    for part, layout in enumerate(TRANSFORMER_LINES):
        arrays += _arrays(parts[part], layout, f"{label} transformer", numbers[part])
        names += [name for name, _ in layout]
    return pa.table(arrays + [pa.array(comments, S)], names=names + ["comment"])


def _case_table(header: list[str], label: str) -> pa.Table:
    match = _LINE.match(header[0])
    body, comment = match.groups() if match else (header[0], None)
    tokens = _tokens(body)
    if len(tokens) >= 3 and tokens[2] not in ("30", "30.0"):
        raise ParseError(f"{label}: PSS/E revision {tokens[2]}; this parser reads version 30")
    return pa.table({
        "ic": cast_column(pa.array([tokens[0] if tokens else None], S), I, f"{label}.ic"),
        "sbase": cast_column(pa.array([tokens[1] if len(tokens) > 1 else None], S), F, f"{label}.sbase"),
        "title1": pa.array([header[1].strip()], S),
        "title2": pa.array([header[2].strip()], S),
        "header_comment": pa.array([comment.strip() if comment else None], S),
    })
