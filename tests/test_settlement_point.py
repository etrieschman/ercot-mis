import polars as pl
import pytest

from ercot_mis.core import settlement_point as spm


def _nodes(buses, keys):
    return pl.DataFrame({"psse_bus_number": buses, "node_key": keys})


def test_crr_points_normalize_weights_merge_contracted_buses_and_flag_unknown_buses():
    nodes = _nodes([1, 2, 3, 4], ["A", "A", "B", "C"])  # buses 1 and 2 are one contracted node
    ss = pl.DataFrame({
        "name": ["RN_X", "HB_H", "HB_H", "HB_H", "LZ_Z", "LZ_Z", "DCTIE", "RN_GONE"],
        "price_node": ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"],
        "bus_name": ["3 STN", "1 STN", "2 STN", "4 STN", "3 STN", "4 STN", "4 STN", "99 STN"],
        "participation_factor": [1.0, 100.0, 300.0, 400.0, 0.25, 0.75, 1.0, 1.0],
    })
    points, rows = spm.crr_settlement_points(nodes, ss)
    assert list(points.columns) == list(spm.SP_COLUMNS) and list(rows.columns) == list(spm.NODE_COLUMNS)
    by = {r["settlement_point_id"]: r for r in points.to_dicts()}
    assert (by["RN_X"]["kind"], by["HB_H"]["kind"], by["LZ_Z"]["kind"], by["DCTIE"]["kind"]) == ("resource_node", "hub", "load_zone", "dc_tie")
    assert (by["HB_H"]["n_nodes"], by["HB_H"]["weight_sum_raw"]) == (2, 800.0)  # buses 1 and 2 merged into node A
    hub = {r["node_key"]: r["weight"] for r in rows.filter(pl.col("settlement_point_id") == "HB_H").to_dicts()}
    assert hub == {"A": 0.5, "C": 0.5}
    assert (by["RN_GONE"]["n_nodes"], by["RN_GONE"]["n_unresolved"]) == (0, 1)
    gone = rows.filter(pl.col("settlement_point_id") == "RN_GONE").row(0, named=True)
    assert gone["node_key"] is None and gone["is_resolved"] is False and gone["psse_bus"] == 99


def test_dam_points_use_sp_hub_and_load_files_and_derive_the_average_hubs():
    nodes = _nodes([10, 11, 12, 13, 14], ["N10", "N11", "N12", "N13", "N14"])
    sp = pl.DataFrame({
        "settlement_point_name": ["RN_A", "RN_LOGICAL", "HB_ONE", "HB_TWO", "HB_BUSAVG", "HB_HUBAVG", "LZ_Q", "DC_T"],
        "settlement_point_type": ["Resource Node", "Logical Resource Node", "Hub", "Hub", "Hub", "Hub", "Load Zone", "DC Tie Load Zone"],
        "psse_bus_number": [10, None, None, None, None, None, None, None],
        "combined_cycle_settlement_point": [None, "RN_A", None, None, None, None, None, None],
    })
    hb = pl.DataFrame({"hub_name": ["HB_ONE", "HB_ONE", "HB_ONE", "HB_TWO"], "psse_bus_number": [11, 12, 13, 14]})
    ld = pl.DataFrame({"load_zone_name": ["LZ_Q", "LZ_Q", "LZ_Q", "DC_T"], "psse_bus_number": [12, 13, 14, 10],
                       "load_status": ["In-Service", "In-Service", "Out-Of-Service", "In-Service"], "raw_mw_ldf": [30.0, 10.0, 60.0, 5.0]})
    points, rows = spm.dam_settlement_points(nodes, sp, hb, ld)
    by = {r["settlement_point_id"]: r for r in points.to_dicts()}
    weights = lambda name: {r["node_key"]: round(r["weight"], 4) for r in rows.filter(pl.col("settlement_point_id") == name).to_dicts()}
    assert weights("RN_A") == {"N10": 1.0} and weights("RN_LOGICAL") == {"N10": 1.0}
    assert weights("HB_ONE") == {"N11": round(1 / 3, 4), "N12": round(1 / 3, 4), "N13": round(1 / 3, 4)} and weights("HB_TWO") == {"N14": 1.0}
    assert weights("HB_BUSAVG") == {"N11": 0.25, "N12": 0.25, "N13": 0.25, "N14": 0.25}
    assert weights("HB_HUBAVG") == {"N11": round(1 / 6, 4), "N12": round(1 / 6, 4), "N13": round(1 / 6, 4), "N14": 0.5}
    assert weights("LZ_Q") == {"N12": 0.75, "N13": 0.25}  # the out-of-service load is left out
    assert weights("DC_T") == {"N10": 1.0}
    assert by["LZ_Q"]["kind"] == "load_zone" and by["DC_T"]["kind"] == "dc_tie" and by["HB_HUBAVG"]["kind"] == "hub"
    assert rows.filter(pl.col("source") == "dam_loads")["settlement_point_id"].unique().sort().to_list() == ["DC_T", "LZ_Q"]
    assert points["n_unresolved"].sum() == 0


def test_dam_point_without_any_row_is_unresolved():
    nodes = _nodes([10], ["N10"])
    sp = pl.DataFrame({"settlement_point_name": ["RN_A", "HB_LONE"], "settlement_point_type": ["Resource Node", "Hub"],
                       "psse_bus_number": [10, None], "combined_cycle_settlement_point": [None, None]})
    hb = pl.DataFrame({"hub_name": [], "psse_bus_number": []}, schema={"hub_name": pl.String, "psse_bus_number": pl.Int64})
    ld = pl.DataFrame(schema={"load_zone_name": pl.String, "psse_bus_number": pl.Int64, "load_status": pl.String, "raw_mw_ldf": pl.Float64})
    points, rows = spm.dam_settlement_points(nodes, sp, hb, ld)
    lone = points.filter(pl.col("settlement_point_id") == "HB_LONE").row(0, named=True)
    assert (lone["n_nodes"], lone["n_unresolved"]) == (0, 1)
    assert rows.filter(pl.col("settlement_point_id") == "HB_LONE")["is_resolved"].to_list() == [False]
