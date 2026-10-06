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


def test_prefix_with_several_candidates_is_settled_by_free_candidates_and_reactance():
    # T1 claims LINE_Z1 by circuit. T2 has candidates LINE_Z1 (taken), LINE_Z2 (x agrees) and LINE_ZA (x differs).
    # T3's only free candidates both disagree on x. T4 is a CRR tie (x = 1e-4) whose DAM twin sits at the floor.
    crr = pl.DataFrame({"branch_id": ["T1", "T2", "T3", "T4"], "ckt": ["1", "7", "7", "9"], "x_pu": [0.01, 0.02, 0.05, 0.0001]})
    lines = pl.DataFrame({"crr_tag": ["T1", "T2", "T3", "T4"], "operations_name": ["LINE_Z", "LINE_Z", "LINE_Y", "TIE_Q"]})
    autos = pl.DataFrame({"crr_name": [], "operations_name": []}, schema={"crr_name": pl.String, "operations_name": pl.String})
    dam = pl.DataFrame({"branch_id": ["LINE_Z1", "LINE_Z2", "LINE_ZA", "LINE_Y1", "LINE_Y2", "TIE_Q1", "TIE_Q2"],
                        "x_pu": [0.01, 0.0201, 0.03, 0.01, 0.02, 0.0005, 0.003]})
    result = match.match_branches(crr, lines, autos, dam)
    by = {r["crr_branch_id"]: r for r in result.to_dicts() if r["crr_branch_id"]}
    assert (by["T1"]["dam_branch_id"], by["T1"]["match_method"]) == ("LINE_Z1", "ops+ckt")
    assert (by["T2"]["dam_branch_id"], by["T2"]["match_method"], by["T2"]["n_candidates"]) == ("LINE_Z2", "prefix+x", 3)
    assert by["T3"]["match_method"] == "unmatched" and by["T3"]["n_candidates"] == 2
    assert (by["T4"]["dam_branch_id"], by["T4"]["match_method"]) == ("TIE_Q1", "prefix+x")
    assert result.filter(pl.col("match_method") != "unmatched")["dam_branch_id"].is_unique().all()


def test_reactance_agreement_allows_for_the_dam_floor():
    frame = pl.DataFrame({"c": [0.0001, 0.0003, 0.02, 0.02, -0.01], "d": [0.0005, 0.0005, 0.0201, 0.03, 0.01]})
    assert frame.select(match.reactance_agrees(pl.col("c"), pl.col("d")))["c"].to_list() == [True, True, True, False, True]


def _nodes(keys, attachments):
    return pl.DataFrame({"bus_key": keys, "attachments": attachments})


def _branch(ids, ends):
    return pl.DataFrame({"branch_id": ids, "from_bus_key": [e[0] for e in ends], "to_bus_key": [e[1] for e in ends]})


def test_match_buses_by_settlement_point_then_branch_endpoints():
    # CRR: c1 -L1- c2 -L2- c3 ; c4 isolated ; SP_A on c1, zone Z on c2 and c3 (skipped: not 1:1)
    crr_nodes = _nodes(["c1", "c2", "c3", "c4"], ["B:L1|S:SP_A", "B:L1|B:L2|S:Z", "B:L2|S:Z", ""])
    dam_nodes = _nodes(["d1", "d2", "d3", "d9"], ["B:DL1|S:SP_A", "B:DL1|B:DL2", "B:DL2", "S:OTHER"])
    crr_branch = _branch(["L1", "L2"], [("c1", "c2"), ("c2", "c3")])
    dam_branch = _branch(["DL1", "DL2"], [("d2", "d1"), ("d3", "d2")])  # reversed orientations
    matches = pl.DataFrame({"crr_branch_id": ["L1", "L2"], "dam_branch_id": ["DL1", "DL2"], "match_method": ["exact", "prefix"]})
    result = match.match_buses(crr_nodes, dam_nodes, crr_branch, dam_branch, matches)
    by = {r["crr_bus_key"]: r for r in result.to_dicts() if r["crr_bus_key"]}
    assert (by["c1"]["dam_bus_key"], by["c1"]["match_method"], by["c1"]["settlement_point"], by["c1"]["n_votes"]) == ("d1", "settlement_point", "SP_A", 1)
    assert (by["c2"]["dam_bus_key"], by["c2"]["match_method"], by["c2"]["n_votes"]) == ("d2", "branch_endpoints", 2)
    assert (by["c3"]["dam_bus_key"], by["c3"]["match_method"]) == ("d3", "branch_endpoints")  # degree 1 on both sides
    assert by["c4"]["match_method"] == "unmatched" and by["c4"]["n_candidates"] == 0
    dam_only = result.filter(pl.col("crr_bus_key").is_null())
    assert dam_only["dam_bus_key"].to_list() == ["d9"] and dam_only["match_method"][0] == "unmatched"
    assert result.filter(pl.col("match_method") != "unmatched")["dam_bus_key"].is_unique().all()
    assert list(result.columns) == list(match.BUS_COLUMNS)


def test_match_contingencies_by_name_then_by_translated_branch_set():
    crr_out = pl.DataFrame({"contingency_id": ["C1", "C1", "C2", "C3"], "branch_id": ["L1", "L2", "L3", "L9"]})
    dam_out = pl.DataFrame({"contingency_id": ["c1", "c1", "c1", "X2", "D4"], "element_kind": ["branch", "branch", "load", "branch", "branch"],
                            "operation": ["outage", "outage", "outage", "outage", "split_bus"], "branch_id": ["DL1", "DL9", None, "DL3", "DL7"]})
    branches = pl.DataFrame({"crr_branch_id": ["L1", "L2", "L3"], "dam_branch_id": ["DL1", "DL2", "DL3"], "match_method": ["exact", "prefix", "exact"]})
    result = match.match_contingencies(crr_out, dam_out, branches)
    by = {r["crr_contingency_id"]: r for r in result.to_dicts() if r["crr_contingency_id"]}
    c1 = by["C1"]
    assert (c1["dam_contingency_id"], c1["match_method"], c1["n_crr_branches"], c1["n_dam_branches"], c1["n_shared_branches"], c1["n_dam_other_rows"]) == ("c1", "name", 2, 2, 1, 1)
    assert (by["C2"]["dam_contingency_id"], by["C2"]["match_method"]) == ("X2", "members")
    assert by["C3"]["match_method"] == "unmatched" and by["C3"]["n_crr_branches"] == 1
    d4 = result.filter(pl.col("dam_contingency_id") == "D4").to_dicts()[0]
    assert d4["crr_contingency_id"] is None and d4["has_split_bus"] and d4["match_method"] == "unmatched"
    assert list(result.columns) == list(match.CONTINGENCY_COLUMNS)


def test_several_settlement_points_on_one_node_agree_or_block():
    # c1 hosts SP_A and SP_B, both on d1 in DAM: one match with two votes.
    # c2 hosts SP_C (on d2) and SP_D (on d3): the points disagree, so c2 stays unmatched by this method.
    crr_nodes = _nodes(["c1", "c2"], ["S:SP_A|S:SP_B", "S:SP_C|S:SP_D"])
    dam_nodes = _nodes(["d1", "d2", "d3"], ["S:SP_A|S:SP_B", "S:SP_C", "S:SP_D"])
    empty = pl.DataFrame(schema={"branch_id": pl.String, "from_bus_key": pl.String, "to_bus_key": pl.String})
    matches = pl.DataFrame({"crr_branch_id": [], "dam_branch_id": [], "match_method": []}, schema={"crr_branch_id": pl.String, "dam_branch_id": pl.String, "match_method": pl.String})
    result = match.match_buses(crr_nodes, dam_nodes, empty, empty, matches)
    by = {r["crr_bus_key"]: r for r in result.to_dicts() if r["crr_bus_key"]}
    assert (by["c1"]["dam_bus_key"], by["c1"]["n_votes"]) == ("d1", 2)
    assert by["c2"]["match_method"] == "unmatched"
