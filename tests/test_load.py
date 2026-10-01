import polars as pl

from ercot_mis.core import load


def _nodes():
    return pl.DataFrame({"node_number": [1, 2, 3], "bus_key": ["A", "B", "C"]})


def test_crr_loads_take_the_bus_name_from_the_comment_and_keep_open_loads():
    raw = pl.DataFrame({"i": [1, 1, 2, 9], "id": ["1 ", "T2", "1", "1"], "status": [1, 0, 1, 1], "pl": [10.0, 4.0, 0.0, 1.0],
                        "comment": ["1 STN_8 1 XX", "1 STN_8 T2 XX", "2 TWO WORDS 1 XX", None]})
    rows = load.crr_loads(_nodes(), raw)
    assert list(rows.columns) == list(load.COLUMNS)
    by = {r["load_id"]: r for r in rows.to_dicts()}
    assert set(by) == {"STN_8:1", "STN_8:T2", "TWO WORDS:1", "9:1"}
    assert (by["STN_8:T2"]["is_in_service"], by["STN_8:T2"]["mw"], by["STN_8:T2"]["bus_key"]) == (False, 4.0, "A")
    assert by["9:1"]["bus_key"] is None and by["STN_8:1"]["mw_ldf"] is None
    twins = pl.DataFrame({"i": [1, 2], "id": ["1", "1"], "status": [1, 1], "pl": [1.0, 1.0], "comment": ["1 SAME 1 XX", "2 SAME 1 XX"]})
    assert load.crr_loads(_nodes(), twins)["load_id"].to_list() == ["SAME:1#1", "SAME:1#2"]


def test_dam_loads_join_the_load_file_to_the_raw_record():
    raw = pl.DataFrame({"i": [1, 2], "id": ["L1", "L2"], "status": [1, 0], "pl": [7.0, 3.0]})
    ld = pl.DataFrame({"load_name": ["STN_L1", "STN_L2"], "psse_bus_number": [1, 2], "psse_load_id": ["L1", "L2"],
                       "load_status": ["In-Service", "Out-Of-Service"], "load_zone_name": ["LZ_Q", "LZ_Q"], "weather_zone_name": ["W", "W"],
                       "ercot_load": ["YES", "NO"], "conforming_or_non_conforming": ["Conforming", "Non-Conforming"],
                       "raw_mw_ldf": [0.6, 0.4], "load_rollover_capable": ["NO", "YES"], "number_of_target_loads": ["0", "2"]})
    rows = {r["load_id"]: r for r in load.dam_loads(_nodes(), raw, ld).to_dicts()}
    assert rows["STN_L1"] == {"load_id": "STN_L1", "node_number": 1, "psse_load_id": "L1", "bus_key": "A", "is_in_service": True, "mw": 7.0,
                              "load_zone": "LZ_Q", "weather_zone": "W", "is_ercot_load": True, "is_conforming": True, "mw_ldf": 0.6,
                              "is_rollover_capable": False, "n_rollover_targets": 0}
    assert (rows["STN_L2"]["is_in_service"], rows["STN_L2"]["is_rollover_capable"], rows["STN_L2"]["n_rollover_targets"]) == (False, True, 2)
