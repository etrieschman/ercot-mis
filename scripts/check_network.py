"""Solve the DC model on assembled networks and report what a consumer will meet.

For each snapshot (the latest CRR monthly model and the latest DAM hour by default):
build ``session.network`` as ERCOT solves it, factorize the reduced susceptance matrix,
solve a random balanced injection, and report the residual, the flows on bus ties,
and how many limited branches are ties. For CRR, also build the contracted variant
and compare PTDF rows on a sample of limited branches, so the cost of contraction is
a number in the report rather than a belief. Prints counts only; writes a dated JSON
report to ``data/reports/network/``.

    uv run python scripts/check_network.py
    uv run python scripts/check_network.py --crr crr:monthly:2026-10:r1 --dam dam:2026-09-25:he12:r1
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone

import numpy as np
import polars as pl

import ercot_mis as em
from ercot_mis.core.node import TIE_REACTANCE
from ercot_mis.out.network import Network, Options
from ercot_mis.sensitivities import DcSystem

REPORT: dict = {}


def show(label: str, **values) -> None:
    REPORT[label] = values
    print(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in values.items()))


def check(label: str, net: Network) -> DcSystem:
    system = DcSystem(net)
    rng = np.random.default_rng(0)
    q = rng.normal(size=net.n_nodes)
    q -= q.mean()
    flows = system.flows(q)
    residual = float(np.abs(system.A @ flows - q).max())
    is_tie = (net.branches["x_pu"].abs() <= TIE_REACTANCE).to_numpy()
    limited = net.branches["is_limited"].to_numpy()
    huge = (net.branches["base_limit_mw"] >= 9999).to_numpy()
    show(label, nodes=net.n_nodes, branches=net.n_branches, factorize_seconds=round(system.seconds, 3), residual=f"{residual:.1e}",
         ties=int(is_tie.sum()), limited=int(limited.sum()), limited_ties=int((limited & is_tie).sum()),
         limited_ties_with_placeholder_rating=int((limited & is_tie & huge).sum()),
         max_abs_flow_ties=round(float(np.abs(flows[is_tie]).max()), 3) if is_tie.any() else None,
         max_abs_flow_others=round(float(np.abs(flows[~is_tie]).max()), 3),
         negative_reactance=int((net.branches["x_pu"] < 0).sum()),
         dropped=net.summary()["dropped_branches"], dropped_contingencies=net.summary()["dropped_contingencies"])
    return system


def compare_contraction(session, snapshot_id: str, sample_size: int = 200) -> None:
    """PTDF rows of limited branches, ERCOT's topology versus the contracted one."""
    loose, tight = session.network(snapshot_id), session.network(snapshot_id, Options(contract_ties=True))
    ls, ts = DcSystem(loose), DcSystem(tight)
    common = (tight.branches.filter(pl.col("is_limited")).select("branch_id", pl.col("index").alias("ti"))
              .join(loose.branches.select("branch_id", pl.col("index").alias("li")), on="branch_id"))
    sample = common.sample(n=min(sample_size, common.height), seed=1)
    t_rows, l_rows = ts.ptdf_rows(sample["ti"].to_list()), ls.ptdf_rows(sample["li"].to_list())
    # Every uncontracted node maps to a contracted one (its group key); compare entries after removing the reference offset.
    mapping = (loose.nodes.with_columns(pl.col("node_id").str.split("@").list.first().alias("cid"))
               .join(tight.nodes.select(pl.col("node_id").alias("cid"), pl.col("index").alias("cidx")), on="cid", how="left"))
    cidx = mapping["cidx"].to_numpy()
    ok = mapping["cidx"].is_not_null().to_numpy()
    l_part, t_part = l_rows[:, ok], t_rows[:, cidx[ok].astype(int)]
    diff = np.abs((l_part - l_part[:, :1]) - (t_part - t_part[:, :1]))
    groups = mapping.filter(pl.col("node_id").str.contains("@")).group_by("cid").agg(pl.col("index"))
    spreads = [float(np.abs(l_rows[:, idx] - l_rows[:, idx].mean(axis=1, keepdims=True)).max()) for idx in groups["index"].to_list()]
    show("contracted vs ERCOT topology", sampled_limited_branches=sample.height, nodes_compared=int(ok.sum()),
         max_abs_ptdf_diff=f"{diff.max():.1e}", p99_abs_ptdf_diff=f"{np.quantile(diff, 0.99):.1e}",
         tie_groups=len(spreads), max_ptdf_spread_within_group=f"{max(spreads):.1e}" if spreads else None,
         limits_only_in_ercot_topology=int(loose.branches["is_limited"].sum()) - int(tight.branches["is_limited"].sum()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--crr", default=None, help="CRR snapshot id (default: latest monthly)")
    parser.add_argument("--dam", default=None, help="DAM snapshot id (default: latest day, hour 12)")
    args = parser.parse_args()
    with em.open() as session:
        snaps = session.core("snapshot").collect()
        crr_id = args.crr or snaps.filter(pl.col("model_kind") == "monthly").sort("month", "revision")["snapshot_id"][-1]
        dam_id = args.dam or snaps.filter((pl.col("model_kind") == "dam") & (pl.col("hour") == 12)).sort("operating_date", "revision")["snapshot_id"][-1]
        print(f"CRR {crr_id}; DAM {dam_id}")
        check("CRR, ERCOT topology", session.network(crr_id))
        check("CRR, contracted", session.network(crr_id, Options(contract_ties=True)))
        check("DAM", session.network(dam_id))
        compare_contraction(session, crr_id)
        out = session.data_dir / "reports" / "network"
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = out / f"{crr_id}__{dam_id}.json".replace(":", "-")
        path.write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), "crr": crr_id, "dam": dam_id, "sections": REPORT}, indent=1, default=str))
        path.chmod(0o600)
        print(f"\nreport written to {path.relative_to(session.data_dir.parent)}")


if __name__ == "__main__":
    main()
