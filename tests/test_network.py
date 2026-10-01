import math

import polars as pl
import pytest

from ercot_mis.out.network import CoreTables, Options, build_network

# A CRR-like snapshot: buses 1 and 2 are one tie group (node A), 3 is node B, 4 node C,
# 5 an isolated bus, 6 and 7 a two-bus island. Branches: T (tie 1-2), L1 (1-3), L2 (2-3,
# parallel to the group so it loops? no: 2-3 joins A and B like L1), L3 (3-4), X (4-3
# transformer, out of service), P (1-2 real line parallel to the tie: a loop), I (6-7).


def _core():
    node = pl.DataFrame({
        "psse_bus_number": [1, 2, 3, 4, 5, 6, 7],
        "substation": ["S1", "S1", "S2", "S3", "S4", "S5", "S5"],
        "kv": [138.0] * 7,
        "bus_type": [1, 1, 3, 1, 4, 1, 1],
        "bus_group": [1, 1, 3, 4, 5, 6, 7],
        "is_tie_member": [True, True, False, False, False, False, False],
        "bus_key": ["A", "A", "B", "C", "D", "E", "F"],
    })
    branch = pl.DataFrame({
        "branch_id": ["T", "L1", "L2", "L3", "X", "P", "I"],
        "kind": ["line", "line", "line", "line", "transformer", "line", "line"],
        "from_bus": [1, 1, 2, 3, 4, 1, 6], "to_bus": [2, 3, 3, 4, 3, 2, 7],
        "is_in_service": [True, True, True, True, False, True, True],
        "is_tie": [True, False, False, False, False, False, False],
        "x_pu": [0.0001, 0.01, 0.02, 0.03, 0.05, 0.04, 0.01],
        "tap_ratio": [None, None, None, None, 1.05, None, None],
        "is_monitored": [True, True, False, True, True, False, True],
        "is_secured": [True, True, False, True, True, False, True],
        "is_name_reversed": [False, False, False, False, True, False, False],
    })
    rating = pl.DataFrame({
        "branch_id": ["T", "L1", "L2", "L3", "X", "L1", "L3", "L1"],
        "rating_source": ["psse_raw"] * 5 + ["crr_monitored"] * 3,
        "time_of_use": [None] * 5 + ["PeakWD", "PeakWD", "Off-peak"],
        "base_mw": [9999.0, 100.0, 200.0, 0.0, 150.0, 90.0, 120.0, 80.0],
        "emergency_mw": [9999.0, 110.0, 220.0, 0.0, 160.0, 99.0, 121.0, 88.0],
    })
    contingency = pl.DataFrame({"contingency_id": ["C1", "C2", "C3", "C4"], "has_split_bus": [False] * 4})
    outage = pl.DataFrame({
        "contingency_id": ["C1", "C1", "C2", "C3", "C4", "C4"],
        "element_kind": ["line", "line", "line", "line", "line", "load"],
        "operation": ["outage"] * 6,
        "branch_id": ["L1", "T", "ZZZ", "X", "L3", None],
        "is_resolved": [True, True, False, True, True, True],
    })
    gtc = pl.DataFrame({"gtc_id": ["G1"], "source": ["crr_csv"], "limit_mw": [500.0], "crr_gtc_id": ["G1"]})
    member = pl.DataFrame({"gtc_id": ["G1", "G1", "G1"], "branch_id": ["L1", "L3", "X"], "factor": [1.0, 0.5, 1.0],
                           "flow_direction": ["From-To", "To-From", "From-To"], "is_resolved": [True, True, True]})
    points = pl.DataFrame({"settlement_point_id": ["RN_1", "HB_X", "RN_ISLAND"], "kind": ["resource_node", "hub", "resource_node"]})
    point_nodes = pl.DataFrame({"settlement_point_id": ["RN_1", "HB_X", "HB_X", "HB_X", "RN_ISLAND"], "bus_key": ["A", "A", "C", "E", "E"],
                                "weight": [1.0, 0.5, 0.25, 0.25, 1.0], "is_resolved": [True] * 5})
    return CoreTables(node, branch, rating, contingency, outage, gtc, member, points, point_nodes)


CONTRACT = Options(contract_ties=True)


def test_contracted_network_drops_ties_loops_islands_and_out_of_service():
    net = build_network("crr:monthly:2026-10:r1", _core(), CONTRACT)
    assert net.nodes["node_id"].to_list() == ["A", "B", "C"]
    assert net.nodes.filter(pl.col("node_id") == "A")["n_members"][0] == 2
    assert net.branches["branch_id"].to_list() == ["L1", "L2", "L3"]
    dropped = dict(net.dropped_branches.select("branch_id", "reason").rows())
    assert dropped == {"T": "contracted_tie", "P": "loop", "X": "out_of_service", "I": "island"}
    assert dict(net.dropped_nodes.rows()) == {"D": "isolated", "E": "island", "F": "island"}
    assert net.slack_node_id == "B" and net.slack_source == "ercot" and net.nodes.filter(pl.col("is_slack"))["node_id"].to_list() == ["B"]
    # endpoints as dense indexes
    l1 = net.branches.filter(pl.col("branch_id") == "L1").row(0, named=True)
    assert (l1["from_index"], l1["to_index"]) == (0, 1) and l1["tap_ratio"] == 1.0


def test_crr_limits_come_from_the_monitored_csv_block_and_unmonitored_is_unlimited():
    net = build_network("crr:monthly:2026-10:r1", _core(), CONTRACT)
    lim = {r["branch_id"]: (r["base_limit_mw"], r["contingency_limit_mw"], r["is_limited"]) for r in net.branches.to_dicts()}
    assert lim["L1"] == (90.0, 99.0, True)
    assert lim["L3"] == (120.0, 121.0, True)
    assert lim["L2"] == (INF := math.inf, INF, False)  # not monitored
    off = build_network("crr:monthly:2026-10:r1", _core(), Options(contract_ties=True, time_of_use="Off-peak"))
    assert off.branches.filter(pl.col("branch_id") == "L1")["base_limit_mw"][0] == 80.0
    with pytest.raises(ValueError):
        build_network("crr:monthly:2026-10:r1", _core(), Options(time_of_use="Nope"))


def test_raw_ratings_treat_zero_as_unlimited_and_limits_option_widens():
    net = build_network("dam:2026-09-25:he12:r1", _core(), Options(limits="all"))
    lim = {r["branch_id"]: (r["base_limit_mw"], r["contingency_limit_mw"]) for r in net.branches.to_dicts()}
    assert lim["L1"] == (100.0, 110.0) and lim["L2"] == (200.0, 220.0)
    assert lim["L3"] == (math.inf, math.inf)  # rate A = 0
    base = build_network("dam:2026-09-25:he12:r1", _core(), Options(limits="all", contingency_rating="base"))
    assert base.branches.filter(pl.col("branch_id") == "L1")["contingency_limit_mw"][0] == 100.0


def test_contingencies_become_index_sets_and_empty_ones_are_dropped_with_reasons():
    net = build_network("crr:monthly:2026-10:r1", _core(), CONTRACT)
    by = {r["contingency_id"]: r for r in net.contingencies.to_dicts()}
    assert sorted(by) == ["C1", "C4"]
    assert by["C1"]["branch_ids"] == ["L1"] and by["C1"]["n_dropped"] == 1  # the tie outage is a no-op after contraction
    assert by["C1"]["branch_indexes"] == [0]
    assert by["C4"]["branch_ids"] == ["L3"] and by["C4"]["n_other_rows"] == 1
    dropped = {r["contingency_id"]: r for r in net.dropped_contingencies.to_dicts()}
    assert dropped["C2"]["reason"] == "empty" and dropped["C2"]["n_unresolved"] == 1
    assert dropped["C3"]["reason"] == "empty" and dropped["C3"]["n_dropped"] == 1  # X is out of service
    assert net.summary()["dropped_contingencies"] == {"empty": 2}
    kept = build_network("crr:monthly:2026-10:r1", _core(), Options(contract_ties=True, drop_empty_contingencies=False))
    assert kept.contingencies.height == 4 and kept.contingencies.filter(pl.col("is_empty"))["contingency_id"].to_list() == ["C2", "C3"]
    assert kept.dropped_contingencies.is_empty()


def test_slack_falls_back_to_the_busiest_node_when_no_swing_bus_survives():
    core = _core()
    node = core.node.with_columns(pl.when(pl.col("psse_bus_number") == 3).then(1).otherwise(pl.col("bus_type")).alias("bus_type"))
    net = build_network("crr:monthly:2026-10:r1", CoreTables(node, core.branch, core.branch_rating, core.contingency, core.contingency_outage, core.gtc, core.gtc_member), CONTRACT)
    assert net.slack_source == "fallback" and net.slack_node_id == "B"  # B still has the most branches


def test_gtc_members_are_signed_and_counted():
    net = build_network("crr:monthly:2026-10:r1", _core(), CONTRACT)
    assert net.gtcs.to_dicts()[0] == {"gtc_id": "G1", "source": "crr_csv", "limit_mw": 500.0, "n_members": 2, "n_unresolved": 1, "crr_gtc_id": "G1"}
    assert {r["branch_id"]: r["factor"] for r in net.gtc_members.to_dicts()} == {"L1": 1.0, "L3": -0.5}
    # X is out of service above; put it in service: its name is reversed, so "From-To" means RAW to-from.
    core = _core()
    branch = core.branch.with_columns(pl.when(pl.col("branch_id") == "X").then(True).otherwise(pl.col("is_in_service")).alias("is_in_service"))
    net = build_network("crr:monthly:2026-10:r1", CoreTables(branch=branch, **{k: v for k, v in core.__dict__.items() if k != "branch"}), CONTRACT)
    assert {r["branch_id"]: r["factor"] for r in net.gtc_members.to_dicts()} == {"L1": 1.0, "L3": -0.5, "X": -1.0}


def test_settlement_points_are_renormalized_over_kept_nodes():
    net = build_network("crr:monthly:2026-10:r1", _core(), CONTRACT)
    by = {r["settlement_point_id"]: r for r in net.settlement_points.to_dicts()}
    assert (by["RN_1"]["n_nodes"], by["RN_1"]["weight_dropped"]) == (1, 0.0)
    assert (by["HB_X"]["n_nodes"], by["HB_X"]["weight_dropped"]) == (2, 0.25)  # node E is in a dropped island
    assert (by["RN_ISLAND"]["n_nodes"], by["RN_ISLAND"]["weight_dropped"]) == (0, 1.0)
    hub = {r["node_id"]: (r["node_index"], round(r["weight"], 4)) for r in net.settlement_point_nodes.filter(pl.col("settlement_point_id") == "HB_X").to_dicts()}
    assert hub == {"A": (0, round(2 / 3, 4)), "C": (2, round(1 / 3, 4))}
    loose = build_network("crr:monthly:2026-10:r1", _core())  # the default keeps every bus
    rn = {r["node_id"]: r["weight"] for r in loose.settlement_point_nodes.filter(pl.col("settlement_point_id") == "RN_1").to_dicts()}
    assert rn == {"A@1": 0.5, "A@2": 0.5}  # spread over the group's buses


def test_by_default_ties_are_branches_between_their_own_buses_as_ercot_solves_them():
    net = build_network("crr:monthly:2026-10:r1", _core())
    assert net.nodes["node_id"].to_list() == ["A@1", "A@2", "B", "C"]
    assert "T" in net.branches["branch_id"].to_list() and "P" in net.branches["branch_id"].to_list()
    assert net.summary()["dropped_branches"] == {"island": 1, "out_of_service": 1}


def test_a_network_written_to_disk_reads_back_the_same(tmp_path):
    from ercot_mis.out import network as out

    net = build_network("crr:monthly:2026-10:r1", _core(), CONTRACT)
    assert out.read(tmp_path / "missing") is None
    out.write(net, tmp_path / "n")
    back = out.read(tmp_path / "n")
    assert (back.snapshot_id, back.options, back.slack_node_id, back.slack_source) == (net.snapshot_id, net.options, net.slack_node_id, net.slack_source)
    for name in out.FRAMES:
        assert getattr(back, name).equals(getattr(net, name)), name
    assert out.cache_key("core=1", Options()) != out.cache_key("core=1", CONTRACT) != out.cache_key("core=2", CONTRACT)
    (tmp_path / "n" / "network.json").unlink()
    assert out.read(tmp_path / "n") is None  # incomplete folders are not trusted
