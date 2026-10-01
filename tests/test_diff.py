import polars as pl

from ercot_mis.core import diff


def _branch(ids, kinds, ends, x, in_service, flag_name, flags):
    return pl.DataFrame({"branch_id": ids, "kind": kinds, "from_bus_key": [e[0] for e in ends], "to_bus_key": [e[1] for e in ends],
                         "x_pu": x, "is_in_service": in_service, flag_name: flags})


def test_diff_branches_compares_kind_reactance_kv_and_ratings_side_by_side():
    matches = pl.DataFrame({"crr_branch_id": ["C1", "C2", "C3", None], "dam_branch_id": ["D1", "D2", "D3", "D9"],
                            "match_method": ["exact", "prefix", "ops+ckt", "unmatched"]})
    crr_b = _branch(["C1", "C2", "C3"], ["line", "line", "transformer"], [("a", "b"), ("b", "c"), ("c", "d")],
                    [0.0001, 0.02, 0.05], [True, True, False], "is_monitored", [True, False, True])
    dam_b = _branch(["D1", "D2", "D3", "D9"], ["line", "line", "line", "line"], [("p", "q"), ("q", "r"), ("r", "s"), ("s", "s")],
                    [0.0005, 0.03, 0.05, 0.01], [True, True, True, True], "is_secured", [True, True, True, False])
    crr_n = pl.DataFrame({"bus_key": ["a", "b", "c", "d"], "kv": [138.0, 138.0, 345.0, 138.0]})
    dam_n = pl.DataFrame({"bus_key": ["p", "q", "r", "s"], "kv": [138.1, 138.0, 345.2, 69.0]})
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


def test_diff_settlement_points_translates_crr_nodes_and_compares_sets():
    crr_p = pl.DataFrame({"settlement_point_id": ["RN_A", "HB_H", "RN_ONLY_CRR"], "kind": ["resource_node", "hub", "resource_node"]})
    crr_n = pl.DataFrame({"settlement_point_id": ["RN_A", "HB_H", "HB_H", "HB_H"], "bus_key": ["c1", "c1", "c2", "c3"],
                          "weight": [1.0, 0.5, 0.25, 0.25], "is_resolved": [True] * 4})
    dam_p = pl.DataFrame({"settlement_point_id": ["RN_A", "HB_H"], "kind": ["resource_node", "hub"]})
    dam_n = pl.DataFrame({"settlement_point_id": ["RN_A", "HB_H", "HB_H"], "bus_key": ["d1", "d1", "d2"], "weight": [1.0, 0.6, 0.4], "is_resolved": [True] * 3})
    matches = pl.DataFrame({"crr_bus_key": ["c1", "c2", "c3"], "dam_bus_key": ["d1", "d2", None], "match_method": ["settlement_point", "branch_endpoints", "unmatched"]})
    result = diff.diff_settlement_points(crr_p, crr_n, dam_p, dam_n, matches)
    assert list(result.columns) == list(diff.SP_COLUMNS) and result["settlement_point_id"].to_list() == ["HB_H", "RN_A"]
    by = {r["settlement_point_id"]: r for r in result.to_dicts()}
    assert by["RN_A"]["same_buses"] and by["RN_A"]["n_shared_buses"] == 1
    hub = by["HB_H"]
    assert (hub["n_crr_buses"], hub["n_dam_buses"], hub["n_crr_buses_unmatched"], hub["n_shared_buses"], hub["same_buses"]) == (3, 2, 1, 2, False)
    assert (round(hub["shared_weight_crr"], 3), round(hub["shared_weight_dam"], 3)) == (0.75, 1.0)


def test_diff_loads_compares_per_matched_node_and_keeps_unmatched_sides():
    crr = pl.DataFrame({"bus_key": ["c1", "c1", "c2", "c9"], "is_in_service": [True, False, False, True], "mw": [10.0, 4.0, 6.0, 1.0]})
    dam = pl.DataFrame({"bus_key": ["d1", "d2", "d7"], "is_in_service": [True, True, False], "mw": [9.0, 5.0, 0.0],
                        "mw_ldf": [0.5, 0.3, 0.2], "is_rollover_capable": [False, True, False]})
    matches = pl.DataFrame({"crr_bus_key": ["c1", "c2", "c9", None], "dam_bus_key": ["d1", "d2", None, "d7"],
                            "match_method": ["settlement_point", "branch_endpoints", "unmatched", "unmatched"]})
    result = diff.diff_loads(crr, dam, matches)
    assert list(result.columns) == list(diff.LOAD_COLUMNS) and result.height == 4
    by = {(r["crr_bus_key"], r["dam_bus_key"]): r for r in result.to_dicts()}
    one = by[("c1", "d1")]
    assert (one["n_crr_loads"], one["n_crr_in_service"], one["crr_mw_out_of_service"], one["n_dam_loads"], one["same_n_loads"], one["same_n_in_service"]) == (2, 1, 4.0, 1, False, True)
    two = by[("c2", "d2")]  # out of service in CRR, in service in DAM
    assert (two["n_crr_in_service"], two["n_dam_in_service"], two["same_n_in_service"], two["dam_mw_ldf_in_service"], two["n_dam_rollover_capable"]) == (0, 1, False, 0.3, 1)
    assert by[("c9", None)]["match_method"] == "unmatched" and by[("c9", None)]["n_dam_loads"] == 0
    assert by[(None, "d7")]["n_crr_loads"] == 0 and by[(None, "d7")]["dam_mw_ldf_out_of_service"] == 0.2
