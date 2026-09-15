import re

import pyarrow as pa
import pytest

from ercot_mis.parsers import ParseError
from ercot_mis.parsers.psse import parse_raw

# A synthetic two-bus case in the CRR (PSS/ODMS) dialect.
MARKED = """\
0,   100.00     / PSS(R)E 30 RAW created by a test
TEST CASE LINE ONE
TEST CASE LINE TWO
    1,'ALPHA 1     ',  138.0000,3.0,     0.000,     0.000,   1,   1,1.00000,   0.0000,   1 /*[ALPHA BUS 1]*/
    2,'BRAVO 2     ',  138.0000,1,     0.000,     0.000,   1,   1,1.00000,   0.0000,   1 /*[BRAVO BUS 2]*/
0 / END OF BUS DATA, BEGIN LOAD DATA
    2,'1 ',1,   1,   1,     0.000,     0.000,     0.000,     0.000,     0.000,     0.000,   1 /*[LOAD 2]*/
0 / END OF LOAD DATA, BEGIN GENERATOR DATA
    1,'1 ',     0.000,     0.000,     0.000,     0.000,1.00000,     0,   100.000, 0.00000E+0, 1.00000E+0, 0.00000E+0, 0.00000E+0,1.00000,1,  100.0,     0.000,     0.000,   1,1.0000,   0,1.0000,   0,1.0000,   0,1.0000 /*[GEN 1]*/
0 / END OF GENERATOR DATA, BEGIN BRANCH DATA
    1,     2,'1 ', 0.00100, 0.01000,   0.00000,  100.00,  110.00,  120.00,   0.00000,   0.00000,   0.00000,   0.00000,1,   0.00,   1,1.0000,   0,1.0000,   0,1.0000,   0,1.0000 /*[1 ALPHA 2 BRAVO 1]*/
0 / END OF BRANCH DATA, BEGIN TRANSFORMER DATA
    1,     2,     0,'T1',1,1,1, 0.00000E+0, 0.00000E+0,2,'XFMR ONE    ',1,   1,1.0000,   0,1.0000,   0,1.0000,   0,1.0000 /*[1 ALPHA 2 BRAVO T1]*/
 0.00000E+0, 5.00000E-2,   100.00
1.00000,   0.000,   0.000,   200.00,   220.00,   240.00, 0,     0, 1.10000, 0.90000, 1.10000, 0.90000,  33, 0, 0.00000, 0.00000
1.00000,   0.000
0 / END OF TRANSFORMER DATA, BEGIN AREA DATA
   1,     1,     0.000,    10.000,'AREA1       '
0 / END OF AREA DATA, BEGIN TWO-TERMINAL DC DATA
0 / END OF TWO-TERMINAL DC DATA, BEGIN VSC DC LINE DATA
0 / END OF VSC DC LINE DATA, BEGIN SWITCHED SHUNT DATA
    2,1,1.05000,0.95000,     0,100.00,'            ',    0.00,   1,    10.00 /*[SHUNT 2]*/
0 / END OF SWITCHED SHUNT DATA, BEGIN IMPEDANCE CORRECTION DATA
0 / END OF IMPEDANCE CORRECTION DATA, BEGIN MULTI-TERMINAL DC DATA
0 / END OF MULTI-TERMINAL DC DATA, BEGIN MULTI-SECTION LINE DATA
0 / END OF MULTI-SECTION LINE DATA, BEGIN ZONE DATA
   1,'ZONE1       ' /*[ZONE 1]*/
0 / END OF ZONE DATA, BEGIN INTER-AREA TRANSFER DATA
0 / END OF INTER-AREA TRANSFER DATA, BEGIN OWNER DATA
   1,'OWNER1      '
0 / END OF OWNER DATA, BEGIN FACTS DEVICE DATA
0 / END OF FACTS DEVICE DATA
"""


def _outside_quotes(line: str, fn) -> str:
    parts = re.split(r"('[^']*')", line)
    return "".join(p if p.startswith("'") else fn(p) for p in parts)


def to_bare(marked: str) -> str:
    """The same case in the DAM dialect: blanks, bare 0 separators, no comments, no VSC section."""
    lines = marked.splitlines()
    out = [_outside_quotes(lines[0].split("/")[0], lambda p: p.replace(",", " ")), "", ""]
    for line in lines[3:]:
        if "END OF VSC DC LINE DATA" in line:
            continue
        body = _outside_quotes(line, lambda p: p.split("/")[0]) if "/" in line else line
        body = body.split("/*")[0]
        out.append("0" if body.strip() == "0" else _outside_quotes(body, lambda p: p.replace(",", " ")))
    return "\n".join(out) + "\n"


def test_marked_dialect():
    case = parse_raw(MARKED.replace("\n", "\r\n").encode(), "test")
    assert case.dialect == "marked"
    t = case.tables
    assert t["psse_bus"].num_rows == 2 and t["psse_transformer"].num_rows == 1
    assert t["psse_bus"]["name"].to_pylist() == ["ALPHA 1", "BRAVO 2"]
    assert t["psse_bus"]["ide"].to_pylist() == [3, 1]  # "3.0" accepted as an integer
    assert t["psse_bus"].schema.field("basekv").type == pa.float64()
    assert t["psse_branch"]["comment"].to_pylist() == ["1 ALPHA 2 BRAVO 1"]
    assert t["psse_branch"]["ratea"].to_pylist() == [100.0]
    xf = t["psse_transformer"].to_pylist()[0]
    assert (xf["ckt"], xf["x1_2"], xf["rata1"], xf["ntp1"], xf["windv2"], xf["comment"]) == ("T1", 0.05, 200.0, 33, 1.0, "1 ALPHA 2 BRAVO T1")
    assert t["psse_switched_shunt"]["rmidnt"].to_pylist() == [""]
    assert t["psse_switched_shunt"]["n2"].to_pylist() == [None]
    assert t["psse_case"].to_pylist()[0]["sbase"] == 100.0
    assert case.line_counts["transformer"] == 4 and case.line_counts["vsc_dc_line"] == 0
    assert "psse_vsc_dc_line" not in t


def test_both_dialects_parse_to_the_same_tables():
    marked = parse_raw(MARKED.encode(), "marked")
    bare = parse_raw(to_bare(MARKED).encode(), "bare")
    assert bare.dialect == "bare"
    for name, table in marked.tables.items():
        if name == "psse_case":
            continue
        other = bare.tables[name]
        assert other.drop_columns(["comment"]).equals(table.drop_columns(["comment"])), name
        assert other["comment"].null_count == other.num_rows


def test_three_winding_transformers_are_refused():
    three = MARKED.replace("    1,     2,     0,'T1'", "    1,     2,     3,'T1'")
    with pytest.raises(ParseError, match="3-winding"):
        parse_raw(three.encode())


def test_populated_sections_without_a_layout_are_refused():
    dc = MARKED.replace(
        "0 / END OF TWO-TERMINAL DC DATA",
        "   1,1,0.0,500.0,500.0,0.0,0.0,0.0,'I',0.0,20,1.0\n0 / END OF TWO-TERMINAL DC DATA",
    )
    with pytest.raises(ParseError, match="two_terminal_dc section has 1 records"):
        parse_raw(dc.encode())


def test_structure_errors_name_the_problem():
    with pytest.raises(ParseError, match="line 4 has 12 fields"):
        parse_raw(MARKED.replace("0.0000,   1 /*[ALPHA BUS 1]*/", "0.0000,   1, 9 /*[ALPHA BUS 1]*/").encode())
    bare = to_bare(MARKED).splitlines()
    bare.remove("0")  # one separator too few
    with pytest.raises(ParseError, match="14 bare section separators"):
        parse_raw("\n".join(bare).encode())
    with pytest.raises(ParseError, match="unknown section"):
        parse_raw(MARKED.replace("END OF ZONE DATA", "END OF WIDGET DATA").encode())
    with pytest.raises(ParseError, match="revision 33"):
        parse_raw(MARKED.replace("0,   100.00", "0,   100.00, 33", 1).encode())
