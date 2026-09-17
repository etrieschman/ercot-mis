import polars as pl

from ercot_mis.core import branch, node


def _bus(numbers, names):
    return pl.DataFrame({"i": numbers, "name": names, "basekv": [138.0] * len(numbers), "ide": [1] * len(numbers)})


def _raw_branch(rows):
    return pl.DataFrame(rows, schema={"i": pl.Int64, "j": pl.Int64, "ckt": pl.String, "r": pl.Float64, "x": pl.Float64, "b": pl.Float64,
                                      "ratea": pl.Float64, "rateb": pl.Float64, "ratec": pl.Float64, "st": pl.Int64, "comment": pl.String}, orient="row")


def _raw_xf(rows):
    return pl.DataFrame(rows, schema={"i": pl.Int64, "j": pl.Int64, "ckt": pl.String, "r1_2": pl.Float64, "x1_2": pl.Float64, "windv1": pl.Float64,
                                      "windv2": pl.Float64, "ang1": pl.Float64, "rata1": pl.Float64, "ratb1": pl.Float64, "ratc1": pl.Float64,
                                      "stat": pl.Int64, "comment": pl.String}, orient="row")


def test_crr_branches_name_lines_by_comment_transformers_by_autos_and_keep_both_ratings():
    bus = _bus([1, 2, 3, 4], ["A1", "A2", "B", "C"])
    raw_branch = _raw_branch([(1, 2, "1", 0.0, 0.00001, 0.0, 9999.0, 9999.0, 9999.0, 1, "1 A1 2 A2 1"),
                              (2, 3, "1", 0.01, 0.05, 0.0, 200.0, 220.0, 240.0, 1, "2 A2 3 B 1"),
                              (3, 4, "1", 0.01, 0.02, 0.0, 100.0, 110.0, 120.0, 0, "3 B 4 C 1")])
    raw_xf = _raw_xf([(1, 4, "T1", 0.0, 0.1, 1.05, 1.0, 0.0, 300.0, 330.0, 360.0, 1, None)])
    autos = pl.DataFrame({"from_number": ["4"], "to_number": ["1"], "id": ["T1"], "crr_name": ["AUTO_A"]})  # swapped vs the RAW
    sources = pl.DataFrame({"name": ["SP_A"], "bus_name": ["2 A2"]})
    monitored = pl.DataFrame({"device_name": ["2 A2 3 B 1", "2 A2 3 B 1", "AUTO_A"], "device_type": ["Line", "Line", "XFMR"],
                              "base_case_rating": [180.0, 170.0, 270.0], "emergency_rating": [220.0, 200.0, 330.0],
                              "time_of_use": ["PeakWD", "Off-peak", "PeakWD"]})
    nodes = node.crr_nodes(bus, raw_branch, raw_xf, autos, sources)
    branches, ratings = branch.crr_branches(nodes, raw_branch, raw_xf, autos, monitored)

    by = {r["branch_id"]: r for r in branches.to_dicts()}
    assert set(by) == {"1 A1 2 A2 1", "2 A2 3 B 1", "3 B 4 C 1", "AUTO_A"}
    tie = by["1 A1 2 A2 1"]
    assert tie["is_tie"] and tie["from_node_key"] == tie["to_node_key"] and not tie["is_monitored"]
    line = by["2 A2 3 B 1"]
    assert line["is_monitored"] and line["is_secured"] and line["from_node_key"] != line["to_node_key"] and line["kind"] == "line"
    assert not by["3 B 4 C 1"]["is_in_service"]
    xf = by["AUTO_A"]
    assert xf["kind"] == "transformer" and xf["tap_ratio"] == 1.05 and xf["is_monitored"]
    assert list(branches.columns) == list(branch.BRANCH_COLUMNS)

    r = ratings.filter(pl.col("branch_id") == "2 A2 3 B 1").sort("rating_source", "time_of_use")
    assert r.select("rating_source", "time_of_use", "base_mw", "emergency_mw").rows() == [
        ("crr_monitored", "Off-peak", 170.0, 200.0), ("crr_monitored", "PeakWD", 180.0, 220.0), ("psse_raw", None, 200.0, 220.0)]
    assert ratings.filter(pl.col("branch_id") == "AUTO_A").height == 2


def test_dam_branches_take_names_and_flags_from_the_csvs():
    bus = _bus([1, 2, 3], ["ALPHA", "ALPHA", "BRAVO"])
    raw_branch = _raw_branch([(1, 3, "1", 0.01, 0.05, 0.0, 200.0, 220.0, 240.0, 1, None), (2, 3, "1", 0.01, 0.05, 0.0, 200.0, 220.0, 240.0, 1, None)])
    raw_xf = _raw_xf([(1, 2, "1", 0.0, 0.1, 1.0, 1.0, 0.0, 300.0, 330.0, 360.0, 1, None)])
    lines = pl.DataFrame({"psse_from_bus_number": [1, 2], "psse_to_bus_number": [3, 3], "psse_ckt_id": ["1", "1"], "branch_name": ["L1", "L2"],
                          "monitored": ["No", "Yes"], "monitored_and_secured": ["Yes", "No"]})
    xf = pl.DataFrame({"psse_from_bus_number": [1], "psse_to_bus_number": [2], "psse_ckt_id": ["1 "], "branch_name": ["X1"],
                       "monitored": ["No"], "monitored_and_secured": ["Yes"]})
    empty = lambda cols: pl.DataFrame({c: [] for c in cols}, schema={c: (pl.Int64 if "number" in c else pl.String) for c in cols})
    nodes = node.dam_nodes(bus, lines, xf, empty(["psse_bus_number", "generator_name"]), empty(["psse_bus_number", "load_name"]),
                           empty(["psse_bus_number", "settlement_point_name"]))
    branches, ratings = branch.dam_branches(nodes, raw_branch, raw_xf, lines, xf)
    by = {r["branch_id"]: r for r in branches.to_dicts()}
    assert set(by) == {"L1", "L2", "X1"}
    assert not by["L1"]["is_monitored"] and by["L1"]["is_secured"]
    assert by["L2"]["is_monitored"] and not by["L2"]["is_secured"]
    assert by["X1"]["kind"] == "transformer" and by["X1"]["from_node_key"] != by["X1"]["to_node_key"]
    assert not branches["is_tie"].any()
    assert ratings["rating_source"].unique().to_list() == ["psse_raw"] and ratings.height == 3
