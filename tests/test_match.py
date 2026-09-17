import polars as pl

from ercot_mis.core import match


def test_match_branches_tries_exact_then_circuit_then_unique_prefix():
    crr = pl.DataFrame({"branch_id": ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "A1"], "ckt": ["1", "2", "1", "1", "1", "1", "9", "T1"]})
    lines = pl.DataFrame({"crr_tag": ["T1", "T2", "T3", "T4", "T5", "T6", "T7"] + ["P"] * 6,
                          "operations_name": ["LINE_A", "LINE_B", "LINE_C", "LINE_D", "LINE_E", "NONAME", "LINE_D"] + ["<none>"] * 6})
    autos = pl.DataFrame({"crr_name": ["A1"], "operations_name": ["AUTO_X"]})
    dam = pl.DataFrame({"branch_id": ["LINE__A", "LINE_B2", "LINE_C_1", "LINE_D1", "LINE_D2", "AUTO_X", "ORPHAN"]})
    result = match.match_branches(crr, lines, autos, dam)
    by = {r["crr_branch_id"]: r for r in result.to_dicts() if r["crr_branch_id"]}
    assert (by["T1"]["dam_branch_id"], by["T1"]["match_method"]) == ("LINE__A", "exact")
    assert (by["T2"]["dam_branch_id"], by["T2"]["match_method"]) == ("LINE_B2", "ops+ckt")
    assert (by["T3"]["dam_branch_id"], by["T3"]["match_method"]) == ("LINE_C_1", "ops+ckt")
    assert (by["T4"]["dam_branch_id"], by["T4"]["match_method"]) == ("LINE_D1", "ops+ckt")  # LINE_D2 stays unmatched
    assert by["T5"]["match_method"] == "unmatched" and by["T5"]["n_candidates"] == 0
    assert by["T7"]["match_method"] == "unmatched" and by["T7"]["n_candidates"] == 2  # LINE_D1 and LINE_D2 both fit
    assert by["T6"]["match_method"] == "unmatched"
    assert (by["A1"]["dam_branch_id"], by["A1"]["match_method"]) == ("AUTO_X", "exact")
    orphan = result.filter(pl.col("dam_branch_id") == "ORPHAN").to_dicts()[0]
    assert orphan["crr_branch_id"] is None and orphan["match_method"] == "unmatched"
    assert result.filter(pl.col("dam_branch_id") == "LINE_D2").to_dicts()[0]["match_method"] == "unmatched"
    assert list(result.columns) == list(match.COLUMNS)


def test_prefix_match_when_unique():
    crr = pl.DataFrame({"branch_id": ["T1"], "ckt": ["1"]})
    lines = pl.DataFrame({"crr_tag": ["T1"], "operations_name": ["LINE_Z"]})
    autos = pl.DataFrame({"crr_name": [], "operations_name": []}, schema={"crr_name": pl.String, "operations_name": pl.String})
    dam = pl.DataFrame({"branch_id": ["LINE_ZA"]})
    row = match.match_branches(crr, lines, autos, dam).to_dicts()[0]
    assert (row["dam_branch_id"], row["match_method"]) == ("LINE_ZA", "prefix")
