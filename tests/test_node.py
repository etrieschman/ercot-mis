import polars as pl

from ercot_mis.core import node as identity


def _bus(numbers, names, kv=138.0):
    return pl.DataFrame({"i": numbers, "name": names, "basekv": [kv] * len(numbers), "ide": [1] * len(numbers)})


def _dam(renumber: dict[int, int]):
    r = lambda b: renumber.get(b, b)
    bus = _bus([r(1), r(2), r(3)], ["ALPHA", "ALPHA", "BRAVO"])
    lines = pl.DataFrame({"psse_from_bus_number": [r(1), r(2)], "psse_to_bus_number": [r(3), r(3)], "branch_name": ["L1", "L2"]})
    xf = lines.clear()
    gens = pl.DataFrame({"psse_bus_number": [r(1)], "generator_name": ["G1"]})
    loads = pl.DataFrame({"psse_bus_number": [r(2)], "load_name": ["D1"]})
    sps = pl.DataFrame({"psse_bus_number": [r(1)], "settlement_point_name": ["SP_A"]})
    return identity.dam_nodes(bus, lines, xf, gens, loads, sps)


def test_dam_node_keys_survive_renumbering_and_split_station_names():
    a = _dam({})
    b = _dam({1: 7, 2: 9, 3: 8})
    assert a["node_key"].n_unique() == 3 and not a["is_ambiguous"].any()
    assert a.columns == list(identity.COLUMNS) and not a["is_tie_member"].any()
    assert sorted(a["node_key"]) == sorted(b["node_key"])
    key_of = dict(zip(a["psse_bus_number"], a["node_key"]))
    key_of_b = dict(zip(b["psse_bus_number"], b["node_key"]))
    assert key_of[1] == key_of_b[7] and key_of[2] == key_of_b[9]
    assert a.filter(pl.col("psse_bus_number") == 1)["attachments"][0] == "B:L1|G:G1|S:SP_A"


def test_isolated_buses_at_one_station_get_distinct_keys():
    bus = _bus([1, 2], ["ALPHA", "ALPHA"])
    empty = pl.DataFrame({"psse_from_bus_number": [], "psse_to_bus_number": [], "branch_name": []}, schema={"psse_from_bus_number": pl.Int64, "psse_to_bus_number": pl.Int64, "branch_name": pl.String})
    nodes = identity.dam_nodes(bus, empty, empty, pl.DataFrame({"psse_bus_number": [], "generator_name": []}, schema={"psse_bus_number": pl.Int64, "generator_name": pl.String}),
                               pl.DataFrame({"psse_bus_number": [], "load_name": []}, schema={"psse_bus_number": pl.Int64, "load_name": pl.String}),
                               pl.DataFrame({"psse_bus_number": [], "settlement_point_name": []}, schema={"psse_bus_number": pl.Int64, "settlement_point_name": pl.String}))
    assert nodes["is_ambiguous"].all() and nodes["node_key"].n_unique() == 2 and nodes["node_key"][0].endswith("#1")


def test_crr_nodes_contract_bus_ties_and_name_transformers_from_autos():
    bus = _bus([1, 2, 3, 4], ["A_BUS1", "A_BUS2", "B_BUS", "C_BUS"])
    branch = pl.DataFrame({
        "i": [1, 2, 3], "j": [2, 3, 4], "ckt": ["1", "1", "1"], "x": [0.00001, 0.05, 0.02], "st": [1, 1, 1],
        "comment": ["1 A_BUS1 2 A_BUS2 1", "2 A_BUS2 3 B_BUS 1", "3 B_BUS 4 C_BUS 1"],
    })
    transformer = pl.DataFrame({"i": [1], "j": [4], "ckt": ["T1"]})
    autos = pl.DataFrame({"from_number": ["1"], "to_number": ["4"], "id": ["T1"], "crr_name": ["AUTO_A"]})
    sources = pl.DataFrame({"name": ["SP_A"], "bus_name": ["2 A_BUS2"]})
    nodes = identity.crr_nodes(bus, branch, transformer, autos, sources)
    by = {r["psse_bus_number"]: r for r in nodes.to_dicts()}
    assert by[1]["node_group"] == 1 and by[2]["node_group"] == 1 and by[1]["node_key"] == by[2]["node_key"]
    assert by[1]["is_tie_member"] and by[2]["is_tie_member"] and not by[3]["is_tie_member"]
    assert by[3]["node_group"] == 3 and nodes["node_key"].n_unique() == 3
    assert nodes.columns == list(identity.COLUMNS)
    assert by[1]["attachments"] == "B:2 A_BUS2 3 B_BUS 1|B:AUTO_A|S:SP_A"  # the tie itself is not an attachment
    assert by[4]["attachments"] == "B:3 B_BUS 4 C_BUS 1|B:AUTO_A"

    open_tie = branch.with_columns(pl.when(pl.col("i") == 1).then(0).otherwise(pl.col("st")).alias("st"))
    assert identity.crr_nodes(bus, open_tie, transformer, autos, sources)["node_key"].n_unique() == 4
