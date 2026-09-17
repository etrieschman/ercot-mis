import polars as pl

from ercot_mis.core import contingency, gtc


def _branches(ids, keys):
    return pl.DataFrame({"branch_id": ids, "from_bus": [1, 2, 1], "to_bus": [3, 3, 2], "ckt": ["1", "1", "T1"],
                         "from_node_key": [k[0] for k in keys], "to_node_key": [k[1] for k in keys]})


def test_crr_contingencies_resolve_devices_by_branch_id_and_keep_unresolved():
    branches = _branches(["2 A 3 B 1", "1 A 3 B 1", "AUTO_A"], [("a", "b"), ("a", "b"), ("a", "a")])
    raw = pl.DataFrame({"contingency": ["C1", "C1", "C2"], "device_name": ["2 A 3 B 1", "AUTO_A", "GHOST"],
                        "device_type": ["LINE", "XFMR", "LINE"], "action": ["OPN"] * 3})
    ctg, out = contingency.crr_contingencies(branches, raw)
    assert ctg.rows() == [("C1", 2, 0, False), ("C2", 1, 1, False)]
    ghost = out.filter(pl.col("contingency_id") == "C2").to_dicts()[0]
    assert not ghost["is_resolved"] and ghost["branch_id"] is None and ghost["element_name"] == "GHOST"
    assert out.filter(pl.col("branch_id") == "AUTO_A")["element_kind"][0] == "transformer"
    assert list(out.columns) == list(contingency.OUTAGE_COLUMNS)


def test_dam_contingencies_resolve_branches_by_key_and_equipment_by_bus():
    branches = _branches(["L1", "L2", "X1"], [("a", "b"), ("c", "b"), ("a", "c")])
    nodes = pl.DataFrame({"psse_bus_number": [1, 2, 3], "node_key": ["a", "c", "b"]})
    raw = pl.DataFrame({
        "contingency_name": ["D1", "D1", "D2", "D3", "D4"],
        "equipment_type": ["Branch", "Generator", "Load", "SettlementPoint", "Branch"],
        "contingency_operation": ["Outage", "Outage", "Outage", "Split Bus", "Outage"],
        "psse_from_bus_number": [3, None, None, None, 9], "psse_to_bus_number": [1, None, None, None, 9],
        "psse_ckt_id": ["1 ", None, None, None, "1"],
        "psse_gen_or_load_or_sp_bus_number": [None, 2, 3, 99, None], "psse_gen_or_load_id": [None, "G1", "L1", None, None],
        "station_name_psse_bus_name": [None, "ALPHA", "BRAVO", "ZULU", None],
    })
    ctg, out = contingency.dam_contingencies(branches, nodes, raw)
    by = {r["contingency_id"]: r for r in ctg.to_dicts()}
    assert by["D1"] == {"contingency_id": "D1", "n_outages": 2, "n_unresolved": 0, "has_split_bus": False}
    assert by["D3"]["has_split_bus"] and by["D3"]["n_unresolved"] == 1  # bus 99 is not in the model
    assert by["D4"]["n_unresolved"] == 1
    rows = {(r["contingency_id"], r["element_kind"]): r for r in out.to_dicts()}
    assert rows[("D1", "branch")]["branch_id"] == "L1"  # reversed orientation still resolves
    assert rows[("D1", "generator")]["node_key"] == "c" and rows[("D1", "generator")]["psse_id"] == "G1"
    assert rows[("D3", "settlement_point")]["operation"] == "split_bus"


def test_crr_gtcs_and_empty_dam_gtcs():
    branches = _branches(["L1", "L2", "X1"], [("a", "b")] * 3)
    raw = pl.DataFrame({"name": ["G1", "G1", "G2"], "limit": [1500.0, 1500.0, 800.0], "device_name": ["L1", "X1", "NOPE"],
                        "device_type": ["Line", "Transformer", "Line"], "flow_direction": ["From-To", "To-From", "From-To"], "factor": [1.0, 0.5, 1.0]})
    gtcs, members = gtc.crr_gtcs(branches, raw)
    assert gtcs.rows() == [("G1", "crr_csv", 1500.0, 2, 0, "G1"), ("G2", "crr_csv", 800.0, 1, 1, "G2")]
    assert members.filter(pl.col("gtc_id") == "G1")["factor"].to_list() == [1.0, 0.5]
    empty_gtcs, empty_members = gtc.no_gtcs()
    assert empty_gtcs.columns == gtcs.columns and empty_members.columns == members.columns
    assert pl.concat([members, empty_members], how="diagonal_relaxed").height == 3


def test_dam_gtcs_from_gtl_rows_with_crosswalk(tmp_path):
    gtl = pl.DataFrame({"hour_ending": [7, 7, 7], "gtc_name": ["North to Somewhere", "North to Somewhere", "Lonely"],
                        "market": ["rt", "dam", "dam"], "limit_mw": [900.0, 850.0, 100.0]})
    (tmp_path / "overrides").mkdir()
    (tmp_path / "overrides" / "gtc_names.csv").write_text("# comment\ngtl_name,crr_gtc_id\nNorth to Somewhere,N_TO_S\n")
    gtcs, members = gtc.dam_gtcs(gtl, gtc.name_crosswalk(tmp_path))
    assert gtcs.rows() == [("Lonely", "gtl_dam", 100.0, 0, 0, None), ("North to Somewhere", "gtl_dam", 850.0, 0, 0, "N_TO_S")]
    assert members.is_empty() and list(gtcs.columns) == list(gtc.GTC_COLUMNS)
    assert gtc.name_crosswalk(tmp_path / "nowhere").is_empty()
    assert gtc.dam_gtcs(gtl.clear(), gtc.name_crosswalk(tmp_path))[0].columns == list(gtc.GTC_COLUMNS)
