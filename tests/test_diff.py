import polars as pl

from ercot_mis.core import diff


def _branch(ids, kinds, ends, x, in_service, flag_name, flags):
    return pl.DataFrame({"branch_id": ids, "kind": kinds, "from_node_key": [e[0] for e in ends], "to_node_key": [e[1] for e in ends],
                         "x_pu": x, "is_in_service": in_service, flag_name: flags})


def test_diff_branches_compares_kind_reactance_kv_and_ratings_side_by_side():
    matches = pl.DataFrame({"crr_branch_id": ["C1", "C2", "C3", None], "dam_branch_id": ["D1", "D2", "D3", "D9"],
                            "match_method": ["exact", "prefix", "ops+ckt", "unmatched"]})
    crr_b = _branch(["C1", "C2", "C3"], ["line", "line", "transformer"], [("a", "b"), ("b", "c"), ("c", "d")],
                    [0.0001, 0.02, 0.05], [True, True, False], "is_monitored", [True, False, True])
    dam_b = _branch(["D1", "D2", "D3", "D9"], ["line", "line", "line", "line"], [("p", "q"), ("q", "r"), ("r", "s"), ("s", "s")],
                    [0.0005, 0.03, 0.05, 0.01], [True, True, True, True], "is_secured", [True, True, True, False])
    crr_n = pl.DataFrame({"node_key": ["a", "b", "c", "d"], "kv": [138.0, 138.0, 345.0, 138.0]})
    dam_n = pl.DataFrame({"node_key": ["p", "q", "r", "s"], "kv": [138.1, 138.0, 345.2, 69.0]})
    crr_r = pl.DataFrame({"branch_id": ["C1", "C1", "C3"], "rating_source": ["crr_monitored", "crr_monitored", "psse_raw"],
                          "time_of_use": ["PeakWD", "Off-peak", None], "base_mw": [90.0, 80.0, 100.0], "emergency_mw": [99.0, 88.0, 110.0]})
    dam_r = pl.DataFrame({"branch_id": ["D1", "D2"], "rating_source": ["psse_raw", "psse_raw"], "time_of_use": [None, None],
                          "base_mw": [100.0, 200.0], "emergency_mw": [110.0, 220.0]})
    result = diff.diff_branches(matches, crr_b, crr_n, crr_r, dam_b, dam_n, dam_r)
    assert list(result.columns) == list(diff.COLUMNS) and result.height == 3
    by = {r["crr_branch_id"]: r for r in result.to_dicts()}
    c1 = by["C1"]
    assert (c1["same_kind"], c1["same_reactance"], c1["same_kv"]) == (True, True, True)  # tie at the DAM floor; kv tenths dropped
    assert (c1["crr_base_mw"], c1["dam_base_mw"], c1["crr_emergency_mw"], c1["crr_enforced"], c1["dam_enforced"]) == (90.0, 100.0, 99.0, True, True)
    c2 = by["C2"]
    assert (c2["same_reactance"], c2["same_kv"], c2["crr_enforced"], c2["crr_base_mw"]) == (False, True, False, None)
    c3 = by["C3"]
    assert (c3["same_kind"], c3["same_kv"], c3["crr_in_service"], c3["dam_in_service"]) == (False, False, False, True)
    assert c3["crr_base_mw"] is None  # only the CRR CSV block counts as a CRR rating here
