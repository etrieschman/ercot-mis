"""The DC solve on assembled networks, and what contracting CRR bus ties costs (TOP-01).

For a CRR model and a DAM hour: build ``session.network`` as ERCOT solves it, factorize it,
solve a random balanced injection, and report the residual, the flows on bus ties and how
many limited branches are ties. For the CRR model, also build the contracted variant and
compare shift factors on a sample of limited branches, so the cost of contraction is a number.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from ..core.node import TIE_REACTANCE
from ..out.network import Network, Options
from ..out.shift_factors import DcSystem
from . import write_report


def solve(net: Network) -> dict:
    """Counts and a solve residual for one assembled network."""
    system = DcSystem(net)
    q = np.random.default_rng(0).normal(size=net.n_nodes)
    q -= q.mean()
    flows = system.flows(q)
    b = net.branches
    is_tie = (b["x_pu"].abs() <= TIE_REACTANCE).to_numpy()
    limited = b["is_limited"].to_numpy()
    placeholder = (b["base_limit_mw"] >= 9999).to_numpy()
    return {"nodes": net.n_nodes, "branches": net.n_branches, "factorize_seconds": round(system.seconds, 3),
            "residual": f"{float(np.abs(system.A @ flows - q).max()):.1e}", "ties": int(is_tie.sum()), "limited": int(limited.sum()),
            "limited_ties": int((limited & is_tie).sum()), "limited_ties_with_placeholder_rating": int((limited & is_tie & placeholder).sum()),
            "max_abs_flow_ties": round(float(np.abs(flows[is_tie]).max()), 3) if is_tie.any() else None,
            "max_abs_flow_others": round(float(np.abs(flows[~is_tie]).max()), 3), "negative_reactance": int((b["x_pu"] < 0).sum()),
            "dropped": net.summary()["dropped_branches"], "dropped_contingencies": net.summary()["dropped_contingencies"]}


def contraction(session, snapshot_id: str, sample_size: int = 200) -> dict:
    """Shift factors of limited branches, ERCOT's topology against the contracted one."""
    loose, tight = session.network(snapshot_id), session.network(snapshot_id, Options(contract_ties=True))
    common = (tight.branches.filter(pl.col("is_limited")).select("branch_id", pl.col("index").alias("ti"))
              .join(loose.branches.select("branch_id", pl.col("index").alias("li")), on="branch_id"))
    sample = common.sample(n=min(sample_size, common.height), seed=1)
    t_rows, l_rows = DcSystem(tight).shift_factors(sample["ti"].to_list()), DcSystem(loose).shift_factors(sample["li"].to_list())
    # Every node maps to the bus it belongs to; compare after removing each row's reference offset.
    mapping = (loose.nodes.with_columns(pl.col("node_id").str.split("@").list.first().alias("cid"))
               .join(tight.nodes.select(pl.col("node_id").alias("cid"), pl.col("index").alias("cidx")), on="cid", how="left"))
    ok = mapping["cidx"].is_not_null().to_numpy()
    l_part, t_part = l_rows[:, ok], t_rows[:, mapping["cidx"].to_numpy()[ok].astype(int)]
    diff = np.abs((l_part - l_part[:, :1]) - (t_part - t_part[:, :1]))
    groups = mapping.filter(pl.col("node_id").str.contains("@")).group_by("cid").agg(pl.col("index"))
    spreads = [float(np.abs(l_rows[:, idx] - l_rows[:, idx].mean(axis=1, keepdims=True)).max()) for idx in groups["index"].to_list()]
    return {"sampled_limited_branches": sample.height, "nodes_compared": int(ok.sum()),
            "max_abs_ptdf_diff": f"{diff.max():.1e}", "p99_abs_ptdf_diff": f"{np.quantile(diff, 0.99):.1e}",
            "tie_groups": len(spreads), "max_ptdf_spread_within_group": f"{max(spreads):.1e}" if spreads else None,
            "limits_only_in_ercot_topology": int(loose.branches["is_limited"].sum()) - int(tight.branches["is_limited"].sum())}


def run(session, crr_id: str, dam_id: str) -> dict:
    result = {"crr": crr_id, "dam": dam_id, "sections": {
        "CRR, ERCOT topology": solve(session.network(crr_id)),
        "CRR, contracted": solve(session.network(crr_id, Options(contract_ties=True))),
        "DAM": solve(session.network(dam_id)),
        "contracted vs ERCOT topology": contraction(session, crr_id)}}
    write_report(session, "network", f"{crr_id}__{dam_id}", result)
    return result
