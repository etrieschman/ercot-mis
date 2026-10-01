"""The price identity: do our shift factors reproduce ERCOT's DAM prices?

ERCOT's DAM has no loss component, so at every node

    price(node) = system price - sum over binding constraints of shadow price x shift factor(node)

The DAM publishes the binding constraints with their shadow prices (NP4-191-CD) and
the settlement point prices (NP4-190-CD). For one DAM hour this script builds
``session.network``, computes each binding constraint's shift factors on it (base
case, or with the contingency's branches removed), prices every settlement point
through its node weights, and compares with what ERCOT published. The system price
is not published, so it is the one free number: the median difference. What is left
is the residual; if the network, the names and the contingency definitions are
right, it is zero at every point.

It needs no awards and no re-clearing: the shadow prices already carry everything
the DAM's commitment and ancillary services did. It tests shift factors only on the
constraints that bound, and nothing about limits (that is the flow check).

Prints counts and quantiles only; writes ``data/reports/prices/<snapshot>.json``.

    uv run python scripts/check_prices.py
    uv run python scripts/check_prices.py --dam dam:2026-09-30:he12:r1
    uv run python scripts/check_prices.py --day 2026-09-30        # every hour of a day
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import numpy as np
import polars as pl
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components

import ercot_mis as em
from check_network import DcSystem
from ercot_mis.out.network import Network

BASE_CASE = "BASECASE"


def key(expr: pl.Expr) -> pl.Expr:
    return expr.str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


class Outaged(DcSystem):
    """The same network with some branches removed.

    Removing them can cut nodes off (a radial line, a station fed one way). ERCOT's
    engine solves what stays connected to the slack, so this does too: nodes cut off
    get a shift factor of zero and are counted in ``n_islanded``.
    """

    def __init__(self, base: DcSystem, branch_idx):
        self.__dict__.update(base.__dict__)
        self.y = base.y.copy()
        self.y[list(branch_idx)] = 0.0
        live = self.y != 0
        graph = sp.csr_matrix((np.ones(int(live.sum())), (self.f[live], self.t[live])), shape=(self.n, self.n))
        _, component = connected_components(graph, directed=False)
        slack = next(iter(set(range(self.n)) - set(base.keep.tolist())))
        connected = component == component[slack]
        self.n_islanded = int((~connected).sum())
        self.connected = connected
        self.keep = np.array([i for i in base.keep if connected[i]])
        B = (self.A @ sp.diags(self.y) @ self.A.T).tocsc()
        self.lu = spla.splu(B[self.keep][:, self.keep])


def hour_rows(frame: pl.LazyFrame, day: date, hour: int) -> pl.DataFrame:
    """One delivery hour of a raw price table; if a day was posted twice, one posting only."""
    rows = frame.filter((pl.col("delivery_date") == f"{day:%m/%d/%Y}") & (pl.col("hour_ending") == f"{hour:02d}:00")).collect()
    if rows.is_empty():
        return rows
    return rows.filter(pl.col("doc_id") == str(rows["doc_id"].cast(pl.Int64).max()))


def borrowed_gtcs(session, snapshot_id: str, net: Network) -> dict[str, tuple[list[tuple[int, float]], int]]:
    """GTCs as signed DAM branch sets, borrowed from the CRR model of the same month.

    The DAM package carries no GTC members. The CRR package does, under the codes the
    shadow price file uses; each member is translated through ``core.match_branch`` and
    its factor re-signed when the DAM branch runs the other way (its ends
    translated through ``core.match_node``). Returns ``{gtc code: ([(branch index, factor)], members lost)}``.
    """
    day = snapshot_id.split(":")[1]
    crr_ids = session.core("snapshot").filter(pl.col("snapshot_id").str.starts_with(f"crr:monthly:{day[:7]}:")).collect()["snapshot_id"].sort()
    if crr_ids.is_empty():
        return {}
    crr = session.network(crr_ids[-1])
    pairs = session.match_branches(crr_ids[-1], snapshot_id).drop_nulls(["crr_branch_id", "dam_branch_id"]).select("crr_branch_id", "dam_branch_id")
    # Orientation: the CRR branch's ends, translated through core.match_node, against the DAM branch's ends.
    node_of = dict(session.match_nodes(crr_ids[-1], snapshot_id).drop_nulls(["crr_node_key", "dam_node_key"]).select("crr_node_key", "dam_node_key").rows())
    group = lambda e: e.str.split("@").list.first().replace_strict(node_of, default=None, return_dtype=pl.String)
    rows = (crr.gtc_members.join(crr.branches.select("branch_id", group(pl.col("from_node_id")).alias("cf"), group(pl.col("to_node_id")).alias("ct")), on="branch_id", how="left")
            .join(pairs, left_on="branch_id", right_on="crr_branch_id", how="left")
            .join(net.branches.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("index").alias("j"), pl.col("from_node_id").alias("df"), pl.col("to_node_id").alias("dt")),
                  on="dam_branch_id", how="left"))
    declared = dict(crr.gtcs.select(key(pl.col("gtc_id")), pl.col("n_members") + pl.col("n_unresolved")).rows())
    out: dict[str, list[tuple[int, float]]] = {}
    for gtc, factor, cf, ct, j, df, dt in rows.select(key(pl.col("gtc_id")), "factor", "cf", "ct", "j", "df", "dt").rows():
        members = out.setdefault(gtc, [])
        if j is None:
            continue
        same, crossed = (cf == df) + (ct == dt), (cf == dt) + (ct == df)
        if same != crossed:
            members.append((j, factor if same > crossed else -factor))
    return {gtc: (members, declared.get(gtc, len(members)) - len(members)) for gtc, members in out.items()}


def check_hour(session, snapshot_id: str) -> dict | None:
    _, day_text, he, _ = snapshot_id.split(":")
    day, hour = date.fromisoformat(day_text), int(he[2:])
    shadow = hour_rows(session.raw("dam_shadow_prices"), day, hour)
    prices = hour_rows(session.raw("dam_settlement_point_prices"), day, hour)
    if shadow.is_empty() or prices.is_empty():
        return None
    net: Network = session.network(snapshot_id)
    system = DcSystem(net)

    # A binding row names the branch and the direction of the flow that bound, as stations
    # (and voltages, for a transformer inside one station). Shift factors follow the branch's
    # own from-to, so a row whose direction runs the other way gets the opposite sign.
    station = dict(net.nodes.select("index", key(pl.col("station"))).rows())
    kv = dict(net.nodes.select("index", "kv").rows())
    branch_of = {k: (j, f, t) for k, j, f, t in net.branches.select(key(pl.col("branch_id")), "index", "from_index", "to_index").rows()}
    ctg_of = {k: idx for k, idx in net.contingencies.select(key(pl.col("contingency_id")), "branch_indexes").rows()}
    dropped_ctg = set(net.dropped_contingencies.select(key(pl.col("contingency_id")))["contingency_id"])

    def direction(f: int, t: int, from_station: str, to_station: str, from_kv: float, to_kv: float) -> float | None:
        if station[f] != station[t]:
            return 1.0 if (station[f], station[t]) == (from_station, to_station) else -1.0 if (station[t], station[f]) == (from_station, to_station) else None
        if from_kv == to_kv or {round(kv[f]), round(kv[t])} != {round(from_kv), round(to_kv)}:
            return None
        return 1.0 if round(kv[f]) == round(from_kv) else -1.0

    gtc_rows = borrowed_gtcs(session, snapshot_id, net)

    congestion = np.zeros(net.n_nodes)
    outcome: dict[str, int] = {}
    mu_by_outcome: dict[str, float] = {}
    used_mu = total_mu = 0.0
    islanded_nodes = 0
    systems: dict[str, DcSystem] = {}
    rows = shadow.select(key(pl.col("constraint_name")), key(pl.col("contingency_name")), "shadow_price", key(pl.col("from_station")),
                         key(pl.col("to_station")), "from_station_kv", "to_station_kv").rows()
    for name, ctg, mu, from_station, to_station, from_kv, to_kv in rows:
        total_mu += abs(mu)
        solver = None
        if name in gtc_rows:
            members, n_missing = gtc_rows[name]
            why = "used_gtc_crr_members" if not n_missing else "used_gtc_crr_members_incomplete"
            congestion += mu * sum((factor * system.ptdf_rows([j])[0] for j, factor in members), np.zeros(net.n_nodes))
            used_mu += abs(mu)
        elif name not in branch_of:
            why = "constraint_not_a_branch"
        else:
            j, f, t = branch_of[name]
            sign = direction(f, t, from_station, to_station, from_kv, to_kv)
            if sign is None:
                why = "direction_not_recognized"
            elif ctg == BASE_CASE:
                why, solver = "used_base_case", system
            elif ctg in dropped_ctg:
                why, solver = "used_contingency_empty_in_model", system
            elif ctg not in ctg_of:
                why = "contingency_unknown"
            elif j in ctg_of[ctg]:
                why = "constraint_is_outaged_by_its_contingency"
            else:
                if ctg not in systems:
                    systems[ctg] = Outaged(system, ctg_of[ctg])
                    islanded_nodes = max(islanded_nodes, systems[ctg].n_islanded)
                solver = systems[ctg]
                if not (solver.connected[f] and solver.connected[t]):
                    why, solver = "constraint_islanded_by_its_contingency", None
                else:
                    why = "used_contingency_islanding" if solver.n_islanded else "used_contingency"
        outcome[why] = outcome.get(why, 0) + 1
        mu_by_outcome[why] = round(mu_by_outcome.get(why, 0.0) + mu, 2)
        if solver is not None:
            congestion += sign * mu * solver.ptdf_rows([j])[0]
            used_mu += abs(mu)

    weights = net.settlement_point_nodes.select("settlement_point_id", "node_index", "weight")
    predicted = (weights.with_columns((pl.col("weight") * pl.Series(congestion[weights["node_index"].to_numpy()])).alias("c"))
                 .group_by("settlement_point_id").agg(pl.col("c").sum()))
    points = (prices.select(pl.col("settlement_point").alias("settlement_point_id"), pl.col("settlement_point_price").alias("price"))
              .join(predicted, on="settlement_point_id").join(net.settlement_points.select("settlement_point_id", "kind", "weight_dropped"), on="settlement_point_id"))
    observed, c = points["price"].to_numpy(), points["c"].to_numpy()
    result = {"snapshot_id": snapshot_id, "binding_rows": shadow.height, "rows_by_outcome": dict(sorted(outcome.items())),
              "shadow_price_by_outcome": dict(sorted(mu_by_outcome.items())),
              "share_of_shadow_price_used": round(used_mu / total_mu, 4) if total_mu else None,
              "contingencies_solved": len(systems), "max_nodes_islanded_by_a_contingency": islanded_nodes,
              "priced_points_published": prices.height, "points_compared": points.height,
              "price_spread_published": round(float(observed.max() - observed.min()), 2)}
    # The sign convention of the published shadow prices is settled by the data, not assumed.
    for sign, label in ((-1.0, "minus"), (1.0, "plus")):
        resid = observed - sign * c
        resid = resid - np.median(resid)
        result[f"residual_{label}"] = {"p50_abs": round(float(np.quantile(np.abs(resid), 0.5)), 3), "p90_abs": round(float(np.quantile(np.abs(resid), 0.9)), 3),
                                       "p99_abs": round(float(np.quantile(np.abs(resid), 0.99)), 3), "max_abs": round(float(np.abs(resid).max()), 2),
                                       "within_0.01": int((np.abs(resid) <= 0.01).sum()), "within_1.00": int((np.abs(resid) <= 1.0).sum())}
    best = min((-1.0, 1.0), key=lambda s: np.abs(observed - s * c - np.median(observed - s * c)).sum())
    resid = observed - best * c
    resid = np.abs(resid - np.median(resid))
    result["sign"] = "minus" if best < 0 else "plus"
    centred = observed - best * c
    centred = centred - np.median(centred)
    hist, edges = np.histogram(centred, bins=[-1e9, -20, -10, -5, -2, -1, -0.1, 0.1, 1, 2, 5, 10, 20, 1e9])
    result["residual_histogram"] = {f"<{e:g}": int(h) for h, e in zip(hist, edges[1:])}
    result["within_0.01_by_kind"] = {k: f"{ok} of {n}" for k, ok, n in points.with_columns(pl.Series("ok", resid <= 0.01)).group_by("kind").agg(pl.col("ok").sum(), pl.len()).sort("kind").rows()}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dam", default=None, help="DAM snapshot id (default: hour 12 of the latest day with prices)")
    parser.add_argument("--day", default=None, help="check all 24 hours of this operating date")
    args = parser.parse_args()
    with em.open() as session:
        snaps = session.core("snapshot").filter(pl.col("model_kind") == "dam").collect()
        if args.day:
            ids = snaps.filter(pl.col("operating_date") == date.fromisoformat(args.day)).sort("hour", "revision").unique(subset=["hour"], keep="last").sort("hour")["snapshot_id"].to_list()
        elif args.dam:
            ids = [args.dam]
        else:
            days = {datetime.strptime(d, "%m/%d/%Y").date() for d in session.raw("dam_shadow_prices").select("delivery_date").unique().collect()["delivery_date"]}
            ids = snaps.filter(pl.col("operating_date").is_in(list(days)) & (pl.col("hour") == 12)).sort("operating_date", "revision")["snapshot_id"].to_list()[-1:]
        out = session.data_dir / "reports" / "prices"
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        for snapshot_id in ids:
            result = check_hour(session, snapshot_id)
            if result is None:
                print(f"{snapshot_id}: no prices archived for this hour")
                continue
            print(json.dumps(result, indent=1))
            path = out / f"{snapshot_id.replace(':', '-')}.json"
            path.write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1))
            path.chmod(0o600)


if __name__ == "__main__":
    main()
