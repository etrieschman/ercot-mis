"""The viewer's data builder on a synthetic DAM snapshot, and the page's fixed parts."""

import json
import stat
from pathlib import Path

import polars as pl

from ercot_mis import viewer

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic" / "viewer_data.json"
SID = "dam:2030-01-15:he12:r1"


class FakeSession:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.tables = {
            "node": pl.DataFrame({"snapshot_id": [SID] * 3, "psse_bus_number": [1, 2, 3], "kv": [345.0, 138.0, 138.0], "station": ["ALPHA", "ALPHA", "BRAVO"],
                                  "bus_type": [3, 1, 1], "node_key": ["a", "b", "c"], "attachments": ["B:XF1|G:ALPHA_G1", "B:XF1|B:L1|S:RN_ALPHA", "B:L1|D:BRAVO_L1"]}),
            "branch": pl.DataFrame({"snapshot_id": [SID] * 2, "branch_id": ["XF1", "L1"], "kind": ["transformer", "line"], "from_bus": [1, 2], "to_bus": [2, 3],
                                    "ckt": ["1", "1"], "x_pu": [0.02, 0.01], "is_in_service": [True, False], "is_tie": [False, False],
                                    "is_monitored": [False, False], "is_secured": [True, True]}),
            "branch_rating": pl.DataFrame({"snapshot_id": [SID] * 2, "branch_id": ["XF1", "L1"], "rating_source": ["psse_raw"] * 2, "time_of_use": [None, None],
                                           "base_mw": [400.0, 200.0], "emergency_mw": [440.0, 220.0]}, schema_overrides={"time_of_use": pl.String}),
            "contingency_outage": pl.DataFrame({"snapshot_id": [SID] * 2, "contingency_id": ["C1", "C2"], "branch_id": ["L1", "L1"]}),
            "settlement_point": pl.DataFrame({"snapshot_id": [SID] * 2, "settlement_point_id": ["RN_ALPHA", "LZ_X"], "kind": ["resource_node", "load_zone"]}),
            "settlement_point_node": pl.DataFrame({"snapshot_id": [SID] * 2, "settlement_point_id": ["RN_ALPHA", "LZ_X"], "node_key": ["b", "c"], "is_resolved": [True, True]}),
            "load": pl.DataFrame({"snapshot_id": [SID], "load_id": ["BRAVO_L1"], "psse_bus": [3], "is_in_service": [True], "mw": [12.5], "mw_ldf": [0.001], "load_zone": ["LZ_X"]}),
        }

    def core(self, table):
        return self.tables[table].lazy()


def test_build_writes_an_owner_only_page_with_the_model_embedded(tmp_path):
    path = viewer.build(FakeSession(tmp_path), SID)
    assert path.parent == tmp_path / "reports" / "viewer" and stat.S_IMODE(path.stat().st_mode) == 0o600
    html = path.read_text()
    assert "ERCOT CEII" in html and "Do not share, upload or publish" in html and "/*DATA*/null" not in html
    data = json.loads(html.split('<script id="data" type="application/json">')[1].split("</script>")[0])
    model = data["models"][0]
    assert data["compare"] is None and model["kind"] == "dam"
    assert {b["n"]: b["st"] for b in model["buses"]} == {1: "ALPHA", 2: "ALPHA", 3: "BRAVO"}
    assert model["buses"][0]["gen"] == ["ALPHA_G1"] and model["buses"][1]["sp"] == ["RN_ALPHA"] and model["buses"][2]["agg"] == ["LZ_X"]
    assert not any(b["star"] for b in model["buses"])
    line = next(b for b in model["branches"] if b["id"] == "L1")
    assert (line["k"], line["on"], line["base"], line["ctg"]) == ("L", False, 200.0, ["C1", "C2"])
    assert model["loads"] == [{"id": "BRAVO_L1", "bus": 3, "on": True, "mw": 12.5, "ldf": 0.001, "zone": "LZ_X"}]
    # The synthetic page data used to look at the template by hand has the same shape.
    sample = json.loads(FIXTURE.read_text())["models"][1]
    assert set(sample) == set(model) and set(sample["buses"][0]) == set(model["buses"][0])
    assert set(sample["branches"][0]) == set(model["branches"][0]) and set(sample["loads"][0]) == set(model["loads"][0])


def test_the_page_loads_nothing_from_the_network():
    html = viewer.TEMPLATE.read_text()
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "") and "https://" not in html
