"""The price identity: do our shift factors reproduce ERCOT's DAM prices?

ERCOT's DAM has no loss component, so at every node

    price(node) = system price - sum over binding constraints of shadow price x shift factor(node)

The DAM publishes the binding constraints with their shadow prices (NP4-191-CD) and
the settlement point prices (NP4-190-CD). For one DAM hour this script builds
``session.network``, computes each binding constraint's shift factors on it (base
case, or with the contingency's branches removed), prices every settlement point
through its node weights, and compares with what ERCOT published. The system price
is the published DAM System Lambda (NP4-523-CD); for hours before it was pulled it is
fitted as the median difference. What is left is the residual; if the network, the
names and the contingency definitions are right, it is zero at every point. A binding
row the model cannot apply (a name it lacks, a contingency whose branches it lacks) is
counted in ``rows_by_outcome`` and lowers ``share_of_shadow_price_used``; the headline
is a test of the model only when that share is one.

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
from scipy import sparse

import ercot_mis as em
from ercot_mis.clock import hour_ending_start_expr
from ercot_mis.out.network import Network
from ercot_mis.shift_factors import DcSystem

BASE_CASE = "BASECASE"


def key(expr: pl.Expr) -> pl.Expr:
    return expr.str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def hour_rows(frame: pl.LazyFrame, start_utc: datetime, session=None) -> pl.DataFrame:
    """The rows of a raw price table for the hour starting at ``start_utc``; if a day was posted twice, the latest posting only.

    ERCOT's (delivery date, hour ending, DST flag) become the instant through
    ``clock.hour_ending_start_expr``, so the repeated and the missing hour of the two
    transition days need no special case (NAM-08).
    """
    rows = (frame.with_columns(hour_ending_start_expr(pl.col("delivery_date").str.to_date("%m/%d/%Y"), pl.col("hour_ending"), pl.col("dst_flag")).alias("_start"))
            .filter(pl.col("_start") == start_utc).drop("_start").collect())
    if rows.is_empty():
        return rows
    ids = rows["doc_id"].unique().to_list()
    if len(ids) == 1:
        return rows
    latest = max(ids, key=int)  # document ids do not follow posting time (NAM-04); the catalog's posting time decides when it can
    if session is not None:
        docs = session.catalog.documents(rows["emil_id"][0], ids).filter(pl.col("posted_at").is_not_null()).sort("posted_at")
        if docs.height:
            latest = docs["doc_id"][-1]
    return rows.filter(pl.col("doc_id") == latest)


def borrowed_gtcs(session, snapshot_id: str, net: Network) -> dict[str, tuple[list[tuple[int, float]], int]]:
    """GTCs as signed DAM branch sets, borrowed from the CRR model of the same month.

    The DAM package carries no GTC members. The CRR package does, under the codes the
    shadow price file uses; each member is translated through ``core.match_branch`` and
    its factor re-signed when the DAM branch runs the other way (its ends
    translated through ``core.match_bus``). Returns ``{gtc code: ([(branch index, factor)], members lost)}``.
    """
    day = snapshot_id.split(":")[1]
    crr_ids = session.core("snapshot").filter(pl.col("snapshot_id").str.starts_with(f"crr:monthly:{day[:7]}:")).sort("revision").collect()["snapshot_id"]
    if crr_ids.is_empty():
        return {}
    crr = session.network(crr_ids[-1])  # the highest revision, by number (TOP-11)
    pairs = session.match_branches(crr_ids[-1], snapshot_id).drop_nulls(["crr_branch_id", "dam_branch_id"]).select("crr_branch_id", "dam_branch_id")
    # Orientation: the CRR branch's ends, translated through core.match_bus, against the DAM branch's ends.
    node_of = dict(session.match_buses(crr_ids[-1], snapshot_id).drop_nulls(["crr_bus_key", "dam_bus_key"]).select("crr_bus_key", "dam_bus_key").rows())
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


def check_hour(session, snapshot_id: str, detail: dict | None = None) -> dict | None:
    snap = session.core("snapshot").filter(pl.col("snapshot_id") == snapshot_id).collect()
    if snap.is_empty():
        raise KeyError(f"{snapshot_id!r} is not in core.snapshot")
    start_utc = snap["interval_start_utc"][0]
    shadow = hour_rows(session.raw("dam_shadow_prices"), start_utc, session)
    prices = hour_rows(session.raw("dam_settlement_point_prices"), start_utc, session)
    if shadow.is_empty() or prices.is_empty():
        return None
    # The system price (NP4-523-CD) is published; before it was pulled it was fitted as the median difference.
    try:
        lam = hour_rows(session.raw("dam_system_lambda"), start_utc, session)
        system_price = float(lam["system_lambda"][0]) if lam.height else None
    except Exception:  # no artifact of the table yet
        system_price = None
    net: Network = session.network(snapshot_id)
    system = DcSystem(net)

    # A binding row names the branch and the direction of the flow that bound, as substations
    # (and voltages, for a transformer inside one substation). Shift factors follow the branch's
    # own from-to, so a row whose direction runs the other way gets the opposite sign.
    substation = dict(net.nodes.select("index", key(pl.col("substation"))).rows())
    kv = dict(net.nodes.select("index", "kv").rows())
    branch_of = {k: (j, f, t) for k, j, f, t in net.branches.select(key(pl.col("branch_id")), "index", "from_index", "to_index").rows()}
    ctg_of = {k: idx for k, idx in net.contingencies.select(key(pl.col("contingency_id")), "branch_indexes").rows()}
    # Of a contingency the network applies: how many of its branch rows it could not (unresolved names, branches it dropped).
    ctg_missing = {k: int(u) + int(d) for k, u, d in net.contingencies.select(key(pl.col("contingency_id")), "n_unresolved", "n_dropped").rows()}
    # Of a contingency the network dropped as empty: why it was empty.
    dropped_info = {k: (int(d), int(u), int(o)) for k, d, u, o in
                    net.dropped_contingencies.select(key(pl.col("contingency_id")), "n_dropped", "n_unresolved", "n_other_rows").rows()}
    limit_flags = {k: f"secured={s} monitored={m} limited={l}" for k, s, m, l in net.branches.select(key(pl.col("branch_id")), "is_secured", "is_monitored", "is_limited").rows()}
    rows_by_flags: dict[str, int] = {}

    def direction(f: int, t: int, from_station: str, to_station: str, from_kv: float, to_kv: float) -> float | None:
        if substation[f] != substation[t]:
            return 1.0 if (substation[f], substation[t]) == (from_station, to_station) else -1.0 if (substation[t], substation[f]) == (from_station, to_station) else None
        # Inside one substation the voltages tell the ends apart, to the tenth: ERCOT uses the
        # tenths digit of the base kV to tell bus sections of one level apart.
        ends, published = (round(kv[f], 1), round(kv[t], 1)), (round(from_kv, 1), round(to_kv, 1))
        if published[0] == published[1] or set(ends) != set(published):
            return None
        return 1.0 if ends == published else -1.0

    gtc_rows = borrowed_gtcs(session, snapshot_id, net)
    splits = {ctg: list(zip(idx, ends)) for ctg, idx, ends in net.contingencies.select(key(pl.col("contingency_id")), "split_branch_indexes", "split_ends").rows() if idx}

    # Settlement point weights as a (points x nodes) matrix. Under a contingency that cuts buses off, a
    # hub's or zone's weights are renormalized over the buses still energized for that constraint
    # (Protocols 4.6.1.2 and 3.5.2: the distribution factors are per constraint); a point with no bus
    # left has no sensitivity to that constraint (no path to it). The same-substation average of
    # 4.5.1(8)(b) is for buses de-energized in the base case, which the network drops before pricing;
    # tried here for islanded points on 2026-09-30, it made the worst of them several dollars worse. PRC-03.
    weights = net.settlement_point_nodes.select("settlement_point_id", "node_index", "weight")
    points_frame = weights.select("settlement_point_id").unique().sort("settlement_point_id").with_row_index("point_index")
    w = weights.join(points_frame, on="settlement_point_id")
    W = sparse.csr_matrix((w["weight"].to_numpy(), (w["point_index"].to_numpy(), w["node_index"].to_numpy())), shape=(points_frame.height, net.n_nodes))

    def aggregated(sf: np.ndarray, connected: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
        """Per point: the constraint's shift factor with per-constraint weights, and with the plain weights."""
        plain = W @ sf
        if connected is None or connected.all():
            return plain, plain
        Wc = W @ sparse.diags(connected.astype(float))
        kept = np.asarray(Wc.sum(axis=1)).ravel()
        has_bus = kept > 0
        Wc = sparse.diags(np.where(has_bus, 1.0 / np.where(has_bus, kept, 1.0), 0.0)) @ Wc
        return Wc @ sf, plain

    node_congestion = np.zeros(net.n_nodes)                 # plain node-level sum, for digging (detail only)
    # ERCOT's own statement of which settlement points a contingency cuts off (the DAM package's SpCtg file, per
    # hour), against the points our solve leaves without a connected bus: a test of contingency definitions,
    # split-bus moves and connectivity that needs no prices (NAM-07).
    try:
        spctg = (session.raw("dam_settlement_point_contingencies").filter((pl.col("operating_date") == snap["operating_date"][0]) & (pl.col("hour") == snap["hour"][0]))
                 .select(key(pl.col("contingency_name")).alias("ctg"), pl.col("settlement_point_name").alias("sp")).collect())
        ercot_cut = {ctg: set(g["sp"]) for (ctg,), g in spctg.group_by("ctg")} if spctg.height else {}
    except FileNotFoundError:
        ercot_cut = None
    point_ids = points_frame["settlement_point_id"].to_list()
    cut_lists = {"binding_contingencies_compared": 0, "binding_contingencies_ercot_lists_none": 0, "agree": 0, "only_ours": 0, "only_ercot": 0}
    compared_ctgs: set[str] = set()
    point_congestion = np.zeros(points_frame.height)        # per-constraint weights and the heuristic (PRC-03)
    point_congestion_plain = np.zeros(points_frame.height)  # plain weights, cut-off buses at zero (what the check did before)
    cut_off_mu = np.zeros(net.n_nodes)  # per node: shadow price of binding rows whose contingency cuts the node off
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
            sf = sum((factor * system.shift_factors([j])[0] for j, factor in members), np.zeros(net.n_nodes))
            agg, plain = aggregated(sf, None)
            node_congestion += mu * sf
            point_congestion += mu * agg
            point_congestion_plain += mu * plain
            used_mu += abs(mu)
        elif name not in branch_of:
            why = "constraint_not_a_branch"
        else:
            j, f, t = branch_of[name]
            rows_by_flags[limit_flags[name]] = rows_by_flags.get(limit_flags[name], 0) + 1  # RAT-09: which flags the binding branches carry
            sign = direction(f, t, from_station, to_station, from_kv, to_kv)
            if sign is None:
                why = "direction_not_recognized"
            elif ctg == BASE_CASE:
                why, solver = "used_base_case", system
            elif ctg in dropped_info:
                n_dropped, n_unresolved, n_other = dropped_info[ctg]
                if n_dropped or n_unresolved:
                    # ERCOT bound a row under a contingency whose branches our model lacks or has out of service:
                    # a topology discrepancy, recorded and not priced (RAT-10).
                    why = "binding_under_contingency_absent_in_model"
                else:
                    why, solver = "used_non_topological_contingency", system  # only generator, load or settlement point rows: the DC topology is the base case
            elif ctg not in ctg_of:
                why = "contingency_unknown"
            elif j in ctg_of[ctg]:
                why = "constraint_is_outaged_by_its_contingency"
            else:
                if ctg not in systems:
                    systems[ctg] = system.outaged(ctg_of[ctg], splits.get(ctg, ()))
                    islanded_nodes = max(islanded_nodes, systems[ctg].n_islanded)
                solver = systems[ctg]
                if not (solver.connected[f] and solver.connected[t]):
                    why, solver = "constraint_islanded_by_its_contingency", None
                else:
                    why = "used_contingency_islanding" if solver.n_islanded else "used_contingency_split_bus" if splits.get(ctg) else "used_contingency"
                    if ctg_missing.get(ctg):
                        why = "used_contingency_partial"  # applied with fewer branches than ERCOT's definition names
        outcome[why] = outcome.get(why, 0) + 1
        mu_by_outcome[why] = round(mu_by_outcome.get(why, 0.0) + mu, 2)
        if solver is not None:
            connected = solver.connected[:net.n_nodes] if why == "used_contingency_islanding" else None
            sf = solver.shift_factors([j])[0]
            agg, plain = aggregated(sf, connected)
            if ercot_cut is not None and ctg != BASE_CASE and ctg not in compared_ctgs and why.startswith("used_contingency"):
                compared_ctgs.add(ctg)
                mask = (connected if connected is not None else np.ones(net.n_nodes, bool)).astype(float)
                ours = {point_ids[i] for i in np.flatnonzero(np.asarray((W @ sparse.diags(mask)).sum(axis=1)).ravel() <= 0)}
                theirs = ercot_cut.get(ctg)
                if theirs is None:
                    cut_lists["binding_contingencies_ercot_lists_none"] += 1
                    theirs = set()
                else:
                    cut_lists["binding_contingencies_compared"] += 1
                cut_lists["agree"] += len(ours & theirs); cut_lists["only_ours"] += len(ours - theirs); cut_lists["only_ercot"] += len(theirs - ours)
            node_congestion += sign * mu * sf
            point_congestion += sign * mu * agg
            point_congestion_plain += sign * mu * plain
            if connected is not None:
                cut_off_mu += mu * ~connected
            used_mu += abs(mu)

    predicted = points_frame.with_columns(pl.Series("c", point_congestion), pl.Series("c_plain", point_congestion_plain))
    points = (prices.select(pl.col("settlement_point").alias("settlement_point_id"), pl.col("settlement_point_price").alias("price"))
              .join(predicted, on="settlement_point_id").join(net.settlement_points.select("settlement_point_id", "kind", "weight_dropped"), on="settlement_point_id"))
    observed, c = points["price"].to_numpy(), points["c"].to_numpy()
    result = {"snapshot_id": snapshot_id, "binding_rows": shadow.height, "rows_by_outcome": dict(sorted(outcome.items())),
              "shadow_price_by_outcome": dict(sorted(mu_by_outcome.items())),
              "share_of_shadow_price_used": round(used_mu / total_mu, 4) if total_mu else None,
              "contingencies_solved": len(systems), "max_nodes_islanded_by_a_contingency": islanded_nodes,
              "priced_points_published": prices.height, "points_compared": points.height,
              "price_spread_published": round(float(observed.max() - observed.min()), 2)}
    # PRC-01: price = system price - sum over binding rows of shadow price x shift factor, with the shift factor in the
    # branch's own from-to orientation and the row's direction sign (PRC-02). The convention is fixed; the residual
    # under the opposite sign is kept as a diagnostic (it was worse in every hour measured).
    SIGN = -1.0

    def residual_for(sign: float) -> np.ndarray:
        base = observed - sign * c
        return base - (system_price if system_price is not None else np.median(base))

    def quantiles(values: np.ndarray) -> dict:
        a = np.abs(values)
        return {"p50_abs": round(float(np.quantile(a, 0.5)), 3), "p90_abs": round(float(np.quantile(a, 0.9)), 3),
                "p99_abs": round(float(np.quantile(a, 0.99)), 3), "max_abs": round(float(a.max()), 2),
                "within_0.01": int((a <= 0.01).sum()), "within_1.00": int((a <= 1.0).sum())}

    best = SIGN
    centred = residual_for(SIGN)
    resid = np.abs(centred)
    result["residual"] = quantiles(centred)
    result["residual_if_sign_flipped"] = quantiles(residual_for(-SIGN))
    # The headline is only a test of the model when every binding row was applied; otherwise the misses mix
    # modeling error with rows that were skipped, and the share says how much shadow price was skipped.
    result["residual_when_every_row_applied"] = result["residual"] if result["share_of_shadow_price_used"] == 1.0 else None
    result["binding_rows_by_limit_flags"] = dict(sorted(rows_by_flags.items()))
    result["system_price"] = {"source": "published" if system_price is not None else "fitted_median",
                              "fitted_median_minus_published": (round(float(np.median(observed - best * c) - system_price), 3)
                                                                if system_price is not None else None)}
    hist, edges = np.histogram(centred, bins=[-1e9, -20, -10, -5, -2, -1, -0.1, 0.1, 1, 2, 5, 10, 20, 1e9])
    result["residual_histogram"] = {f"<{e:g}": int(h) for h, e in zip(hist, edges[1:])}
    by_kind = points.with_columns(pl.Series("abs_residual", resid)).group_by("kind").agg(
        pl.len().alias("n"), pl.col("abs_residual").median().round(3).alias("p50_abs"),
        pl.col("abs_residual").quantile(0.9).round(3).alias("p90_abs"), pl.col("abs_residual").max().round(2).alias("max_abs"),
        (pl.col("abs_residual") <= 1.0).sum().alias("within_1.00")).sort("kind")
    result["residual_by_kind"] = {row["kind"]: {k: v for k, v in row.items() if k != "kind"} for row in by_kind.to_dicts()}
    if detail is not None:  # for digging into a residual: the points, the network and the solver
        detail.update(points=points.with_columns(pl.Series("residual", centred)), net=net, system=system, gtc_rows=gtc_rows, weights=weights, W=W,
                      node_congestion=node_congestion, system_price=system_price, best_sign=best,
                      gtc_binding={n: mu for n, _, mu, *_ in rows if n in gtc_rows})
    # Points with a bus that some binding row's contingency cuts off, against the rest; for them, the
    # residual under the protocol's rule (PRC-03) and under the plain reading (cut-off buses at zero).
    cut = (weights.with_columns(pl.Series("cut", cut_off_mu[weights["node_index"].to_numpy()] > 0)).group_by("settlement_point_id").agg(pl.col("cut").any()))
    is_cut = points.join(cut, on="settlement_point_id", how="left")["cut"].fill_null(False).to_numpy()
    plain_resid = np.abs(observed - best * points["c_plain"].to_numpy() - (system_price if system_price is not None else np.median(observed - best * points["c_plain"].to_numpy())))
    for label, mask in (("points_cut_off_by_a_binding_contingency", is_cut), ("other_points", ~is_cut)):
        result[label] = {"n": int(mask.sum()), "p50_abs": round(float(np.median(resid[mask])), 3) if mask.any() else None,
                         "max_abs": round(float(resid[mask].max()), 2) if mask.any() else None, "beyond_5": int((resid[mask] > 5).sum()),
                         "p50_abs_plain_weights": round(float(np.median(plain_resid[mask])), 3) if mask.any() else None,
                         "max_abs_plain_weights": round(float(plain_resid[mask].max()), 2) if mask.any() else None}
    # Settlement points ERCOT de-energized in the base case (NP4-200-CD) against the points our network leaves
    # without a node (every bus dropped as isolated or islanded): the base-case half of the same test (NAM-07, TOP-05).
    result["cut_off_under_binding_contingencies"] = cut_lists if ercot_cut is not None else None
    try:
        dead = set(hour_rows(session.raw("dam_deenergized_settlement_points"), start_utc, session)["settlement_point"])
        compared = set(points["settlement_point_id"])
        ours_dead = set(net.settlement_points.filter(pl.col("n_nodes") == 0)["settlement_point_id"])
        result["deenergized_in_base_case"] = {"published": len(dead), "among_points_compared": len(dead & compared),
                                              "ours_without_node": len(ours_dead), "agree": len(dead & ours_dead),
                                              "only_ours": len(ours_dead - dead), "only_ercot": len(dead - ours_dead),
                                              "residual_p50_abs": round(float(np.median(resid[points["settlement_point_id"].is_in(list(dead)).to_numpy()])), 3) if dead & compared else None}
    except FileNotFoundError:
        result["deenergized_in_base_case"] = None
    # Electrically similar settlement points (NP4-158-SG) should share a published price; count the groups that do not.
    try:
        groups = hour_rows(session.raw("dam_electrically_similar_settlement_points"), start_utc, session).select("settlement_point", "group_index")
        spread = (prices.select(pl.col("settlement_point"), pl.col("settlement_point_price")).join(groups, on="settlement_point")
                  .group_by("group_index").agg((pl.col("settlement_point_price").max() - pl.col("settlement_point_price").min()).alias("spread"), pl.len()))
        result["electrically_similar_groups"] = {"groups_priced": spread.height, "groups_with_price_spread_over_1_cent": int((spread["spread"] > 0.01).sum()),
                                                 "largest_spread": round(float(spread["spread"].max()), 2) if spread.height else None}
    except FileNotFoundError:
        result["electrically_similar_groups"] = None
    return result


def hour_ids(session, day: date) -> list[str]:
    """Every hour's latest-revision DAM snapshot for one operating day."""
    snaps = session.core("snapshot").filter(pl.col("model_kind") == "dam").collect()
    return snaps.filter(pl.col("operating_date") == day).sort("hour", "revision").unique(subset=["hour"], keep="last").sort("hour")["snapshot_id"].to_list()


def days_with_prices(session) -> list[date]:
    """Operating days that have both a DAM model and archived shadow prices, oldest first."""
    try:
        posted = {datetime.strptime(d, "%m/%d/%Y").date() for d in session.raw("dam_shadow_prices").select("delivery_date").unique().collect()["delivery_date"]}
    except FileNotFoundError:
        return []
    modelled = set(session.core("snapshot").filter(pl.col("model_kind") == "dam").select("operating_date").unique().collect()["operating_date"])
    return sorted(posted & modelled)


def run(session, snapshot_ids: list[str], *, quiet: bool = False) -> list[dict]:
    """Check each hour, write ``data/reports/prices/<snapshot>.json``, return the results (hours without prices skipped)."""
    out = session.data_dir / "reports" / "prices"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    results = []
    for snapshot_id in snapshot_ids:
        result = check_hour(session, snapshot_id)
        if result is None:
            if not quiet:
                print(f"{snapshot_id}: no prices archived for this hour")
            continue
        if not quiet:
            print(json.dumps(result, indent=1))
        path = out / f"{snapshot_id.replace(':', '-')}.json"
        path.write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1))
        results.append(result)
    return results


def summary_line(day: date, results: list[dict]) -> str:
    """One log line for a day: hours checked, the residual's typical, tail and worst values, and the system price source."""
    if not results:
        return f"price check {day}: no hours with prices and a model"
    best = [r["residual"] for r in results]
    sources = {r["system_price"]["source"] for r in results}
    gtc_hours = sum(1 for r in results if any(k.startswith("used_gtc") for k in r["rows_by_outcome"]))
    skipped = [r for r in results if (r["share_of_shadow_price_used"] or 1.0) < 1.0]
    note = f", {len(skipped)} hours with shadow price not applied (least share {min(r['share_of_shadow_price_used'] for r in skipped):.2f})" if skipped else ""
    return (f"price check {day}: {len(results)} hours, p50 {max(b['p50_abs'] for b in best):.2f}, p99 {max(b['p99_abs'] for b in best):.2f}, "
            f"max {max(b['max_abs'] for b in best):.2f} $/MWh (worst hour), {gtc_hours} hours with a binding GTC, system price {'/'.join(sorted(sources))}{note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dam", default=None, help="DAM snapshot id (default: hour 12 of the latest day with prices)")
    parser.add_argument("--day", default=None, help="check all 24 hours of this operating date")
    args = parser.parse_args()
    with em.open() as session:
        if args.day:
            ids = hour_ids(session, date.fromisoformat(args.day))
        elif args.dam:
            ids = [args.dam]
        else:
            days = days_with_prices(session)
            ids = [i for i in hour_ids(session, days[-1]) if i.split(":")[2] == "he12"] if days else []
        run(session, ids)


if __name__ == "__main__":
    main()
