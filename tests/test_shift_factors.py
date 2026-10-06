"""Shift factors on a three-node triangle with a radial fourth node."""

import numpy as np
import polars as pl

from ercot_mis.out.network import CoreTables, build_network
from ercot_mis.shift_factors import DcSystem

# Triangle 1-2-3 (equal reactances) with node 4 hanging off node 3; bus 1 is the slack.
# Branch order after sorting by id: A (1-2), B (2-3), C (1-3), D (3-4).


def _system() -> tuple[DcSystem, dict[str, int]]:
    node = pl.DataFrame({"node_number": [1, 2, 3, 4], "raw_name": ["S1", "S2", "S3", "S4"], "substation": ["S1", "S2", "S3", "S4"], "kv": [138.0] * 4, "node_type": [3, 1, 1, 1],
                         "bus_group": [1, 2, 3, 4], "is_tie_member": [False] * 4, "bus_key": ["N1", "N2", "N3", "N4"]})
    branch = pl.DataFrame({"branch_id": ["A", "B", "C", "D"], "kind": ["line"] * 4, "from_node": [1, 2, 1, 3], "to_node": [2, 3, 3, 4],
                           "is_in_service": [True] * 4, "is_tie": [False] * 4, "x_pu": [0.1] * 4, "tap_ratio": [None] * 4,
                           "is_monitored": [True] * 4, "is_secured": [True] * 4}, schema_overrides={"tap_ratio": pl.Float64})
    rating = pl.DataFrame({"branch_id": ["A"], "rating_source": ["psse_raw"], "time_of_use": [None], "base_mw": [100.0], "emergency_mw": [110.0]},
                          schema_overrides={"time_of_use": pl.String})
    net = build_network("dam:2030-01-01:he01:r1", CoreTables(node, branch, rating))
    return DcSystem(net), dict(net.branches.select("branch_id", "index").rows())


def test_shift_factors_match_the_triangle_by_hand_and_flows_balance():
    system, j = _system()
    rows = system.shift_factors([j["A"], j["C"], j["D"]])
    # Inject at node 2 (index 1), withdraw at the slack: 2/3 comes back over A (against its 1->2 direction), 1/3 round the long way.
    assert np.allclose(rows[0], [0, -2 / 3, -1 / 3, -1 / 3]) and np.allclose(rows[1], [0, -1 / 3, -2 / 3, -2 / 3])
    assert np.allclose(rows[2], [0, 0, 0, -1])  # the radial branch carries all of node 4's injection
    q = np.array([-3.0, 1.0, 1.0, 1.0])
    assert np.allclose(system.A @ system.flows(q), q)
    assert np.allclose(system.flows(q)[[j["A"], j["C"], j["D"]]], rows @ q)


def test_an_outage_reroutes_flow_and_islanding_leaves_the_cut_off_node_at_zero():
    system, j = _system()
    without_c = system.outaged([j["C"]])
    assert without_c.n_islanded == 0 and np.allclose(without_c.shift_factors([j["A"]])[0], [0, -1, -1, -1])
    without_d = system.outaged([j["D"]])
    assert without_d.n_islanded == 1 and not without_d.connected[3]
    assert np.allclose(without_d.shift_factors([j["A"]])[0], [0, -2 / 3, -1 / 3, 0])


def test_a_split_bus_moves_a_branch_end_to_a_new_section():
    system, j = _system()
    # C's end at node 3 moves to a new bus section: C dangles, so node 3 is reached only through A and B.
    split = system.outaged([], [(j["C"], "to")])
    assert split.n_all == 5 and np.allclose(split.shift_factors([j["A"]])[0], [0, -1, -1, -1])
    # Moving B's and D's ends off node 3 together keeps them joined: node 4 hangs off node 2 through the new section.
    both = system.outaged([], [(j["B"], "to"), (j["D"], "from")])
    assert np.allclose(both.shift_factors([j["B"]])[0], [0, 0, 0, -1])


def test_a_transformer_tap_scales_its_susceptance():
    """RAT-11: susceptance is 1/(x * tap). Two parallel branches between the same nodes, one with tap 2, split 2:1."""
    node = pl.DataFrame({"node_number": [1, 2], "raw_name": ["S1", "S2"], "substation": ["S1", "S2"], "kv": [138.0, 138.0], "node_type": [3, 1],
                         "bus_group": [1, 2], "is_tie_member": [False, False], "bus_key": ["N1", "N2"]})
    branch = pl.DataFrame({"branch_id": ["L", "T"], "kind": ["line", "transformer"], "from_node": [1, 1], "to_node": [2, 2], "is_in_service": [True, True],
                           "is_tie": [False, False], "x_pu": [0.1, 0.1], "tap_ratio": [None, 2.0], "is_monitored": [True, True], "is_secured": [True, True]},
                          schema_overrides={"tap_ratio": pl.Float64})
    rating = pl.DataFrame({"branch_id": ["L"], "rating_source": ["psse_raw"], "time_of_use": [None], "base_mw": [100.0], "emergency_mw": [110.0]},
                          schema_overrides={"time_of_use": pl.String})
    net = build_network("dam:2030-01-01:he01:r1", CoreTables(node, branch, rating))
    system = DcSystem(net)
    j = dict(net.branches.select("branch_id", "index").rows())
    rows = system.shift_factors([j["L"], j["T"]])
    assert np.allclose(rows[0], [0, -2 / 3]) and np.allclose(rows[1], [0, -1 / 3])
