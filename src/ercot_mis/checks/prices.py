"""The price identity: do our shift factors rebuild ERCOT's DAM prices?

The DAM has no loss term, so at every node (PRC-01)

    price = system price - sum over binding rows of (shadow price x shift factor)

ERCOT publishes the binding rows with their shadow prices (NP4-191-CD), the settlement
point prices (NP4-190-CD) and the system price (NP4-523-CD). For one DAM hour this check
rebuilds every settlement point's price from those and our network, and reports what is
left: the residual. It is zero everywhere when the network, the names and the contingency
definitions are right. A row the network cannot apply lowers ``share_of_shadow_price_used``;
the residual tests the model only when that share is one. Nothing about limits is tested
here; that is the flow check.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import polars as pl
from scipy import sparse

from ..core.match import name_key
from ..out.shift_factors import DcSystem
from . import write_report
from .binding import BASE_CASE, hour_rows, resolve

RESIDUAL_BINS = [-1e9, -20, -10, -5, -2, -1, -0.1, 0.1, 1, 2, 5, 10, 20, 1e9]


def check_hour(session, snapshot_id: str) -> dict | None:
    """The price identity for one DAM hour; None when ERCOT's prices for the hour are not archived."""
    snap = session.snapshot(snapshot_id)
    start_utc = snap["interval_start_utc"]
    shadow = hour_rows(session, "dam_shadow_prices", start_utc)
    prices = hour_rows(session, "dam_settlement_point_prices", start_utc)
    if shadow.is_empty() or prices.is_empty():
        return None
    lam = hour_rows(session, "dam_system_lambda", start_utc)
    system_price = float(lam["system_lambda"][0]) if lam.height else None
    net = session.network(snapshot_id)
    system = DcSystem(net)
    resolved = resolve(session, snapshot_id, net, system, shadow)

    # Settlement point weights as a (points x nodes) matrix.
    weights = net.settlement_point_nodes.select("settlement_point_id", "node_index", "weight")
    points_frame = weights.select("settlement_point_id").unique().sort("settlement_point_id").with_row_index("point_index")
    w = weights.join(points_frame, on="settlement_point_id")
    W = sparse.csr_matrix((w["weight"].to_numpy(), (w["point_index"].to_numpy(), w["node_index"].to_numpy())), shape=(points_frame.height, net.n_nodes))
    point_ids = points_frame["settlement_point_id"].to_list()

    # ERCOT's own list of the settlement points each contingency cuts off (SpCtg), to compare with ours (NAM-07).
    spctg = session.raw_rows("dam_settlement_point_contingencies", snapshot_id).select(name_key(pl.col("contingency_name")).alias("ctg"),
                                                                                       "settlement_point_name")
    ercot_cut = {ctg: set(g["settlement_point_name"]) for (ctg,), g in spctg.group_by("ctg")}
    cut_lists = {"binding_contingencies_compared": 0, "binding_contingencies_ercot_lists_none": 0, "agree": 0, "only_ours": 0, "only_ercot": 0}
    compared: set[str] = set()

    congestion = np.zeros(points_frame.height)        # per-constraint weights (PRC-03)
    congestion_plain = np.zeros(points_frame.height)  # plain weights, cut-off buses at zero
    cut_off_mu = np.zeros(net.n_nodes)                # per node: shadow price of rows whose contingency cuts it off
    outcome: dict[str, int] = {}
    mu_by_outcome: dict[str, float] = {}
    by_flags: dict[str, int] = {}
    flags = net.branches.select("is_secured", "is_monitored", "is_limited").rows()
    used_mu = total_mu = 0.0
    for row in resolved.bindings:
        total_mu += abs(row.shadow_price)
        outcome[row.outcome] = outcome.get(row.outcome, 0) + 1
        mu_by_outcome[row.outcome] = round(mu_by_outcome.get(row.outcome, 0.0) + row.shadow_price, 2)
        if row.branch is not None:  # RAT-09: which flags the binding branches carry
            label = "secured={} monitored={} limited={}".format(*flags[row.branch])
            by_flags[label] = by_flags.get(label, 0) + 1
        if row.system is None:
            continue
        used_mu += abs(row.shadow_price)
        if row.gtc_members:
            js, factors = zip(*row.gtc_members)
            sf = np.asarray(factors) @ system.shift_factors(list(js))
            congestion += row.shadow_price * (W @ sf)
            congestion_plain += row.shadow_price * (W @ sf)
            continue
        islanding = row.outcome == "used_contingency_islanding"
        connected = row.system.connected[:net.n_nodes] if islanding else None
        sf = row.system.shift_factors([row.branch])[0]
        congestion += row.sign * row.shadow_price * (_renormalized(W, connected) @ sf)
        congestion_plain += row.sign * row.shadow_price * (W @ sf)
        if connected is not None:
            cut_off_mu += row.shadow_price * ~connected
        if row.contingency != BASE_CASE and row.outcome.startswith("used_contingency") and row.contingency not in compared:
            compared.add(row.contingency)
            mask = connected if connected is not None else np.ones(net.n_nodes, bool)
            ours = {point_ids[i] for i in np.flatnonzero(np.asarray((W @ sparse.diags(mask.astype(float))).sum(axis=1)).ravel() <= 0)}
            theirs = ercot_cut.get(row.contingency)
            cut_lists["binding_contingencies_ercot_lists_none" if theirs is None else "binding_contingencies_compared"] += 1
            theirs = theirs or set()
            cut_lists["agree"] += len(ours & theirs)
            cut_lists["only_ours"] += len(ours - theirs)
            cut_lists["only_ercot"] += len(theirs - ours)

    points = (prices.select(pl.col("settlement_point").alias("settlement_point_id"), pl.col("settlement_point_price").alias("price"))
              .join(points_frame.with_columns(pl.Series("c", congestion), pl.Series("c_plain", congestion_plain)), on="settlement_point_id")
              .join(net.settlement_points.select("settlement_point_id", "kind", "weight_dropped"), on="settlement_point_id"))
    observed = points["price"].to_numpy()
    # PRC-01: price = system price - congestion. Before the system price was pulled, it was the median.
    offset = lambda c: system_price if system_price is not None else np.median(observed + c)  # noqa: E731
    residual = observed + points["c"].to_numpy() - offset(points["c"].to_numpy())
    resid = np.abs(residual)
    plain = np.abs(observed + points["c_plain"].to_numpy() - offset(points["c_plain"].to_numpy()))

    share = round(used_mu / total_mu, 4) if total_mu else None
    result = {"snapshot_id": snapshot_id, "binding_rows": shadow.height, "rows_by_outcome": dict(sorted(outcome.items())),
              "shadow_price_by_outcome": dict(sorted(mu_by_outcome.items())), "share_of_shadow_price_used": share,
              "contingencies_solved": len(resolved.systems),
              "max_nodes_islanded_by_a_contingency": max((s.n_islanded for s in resolved.systems.values()), default=0),
              "priced_points_published": prices.height, "points_compared": points.height,
              "price_spread_published": round(float(observed.max() - observed.min()), 2),
              "residual": _quantiles(residual)}
    result["residual_when_every_row_applied"] = result["residual"] if share == 1.0 else None
    result["binding_rows_by_limit_flags"] = dict(sorted(by_flags.items()))
    result["system_price"] = {"source": "published" if system_price is not None else "fitted_median",
                              "fitted_median_minus_published": round(float(np.median(observed + points["c"].to_numpy()) - system_price),
                                                                     3) if system_price is not None else None}
    hist, edges = np.histogram(residual, bins=RESIDUAL_BINS)
    result["residual_histogram"] = {f"<{e:g}": int(h) for h, e in zip(hist, edges[1:])}
    by_kind = points.with_columns(pl.Series("r", resid)).group_by("kind").agg(
        pl.len().alias("n"), pl.col("r").median().round(3).alias("p50_abs"), pl.col("r").quantile(0.9).round(3).alias("p90_abs"),
        pl.col("r").max().round(2).alias("max_abs"), (pl.col("r") <= 1.0).sum().alias("within_1.00")).sort("kind")
    result["residual_by_kind"] = {r.pop("kind"): r for r in by_kind.to_dicts()}

    # SP-05: combined-cycle trains on one bus need no weights; on several, each in-service unit takes an equal share.
    trains = session.core_rows("settlement_point", snapshot_id).filter(pl.col("type_text") == "Logical Resource Node").select("settlement_point_id", "n_buses")
    on_buses = points.select("settlement_point_id").with_row_index("i").join(trains, on="settlement_point_id")
    result["logical_resource_nodes"] = {label: _summary(resid[group["i"].to_numpy()], within=0.01)
                                        for label, group in (("one_bus", on_buses.filter(pl.col("n_buses") == 1)), ("several_buses",
                                                                                                                    on_buses.filter(pl.col("n_buses") > 1)))}

    # Points with a bus some binding contingency cuts off, against the rest: under PRC-03 and under plain weights.
    cut = weights.with_columns(pl.Series("cut", cut_off_mu[weights["node_index"].to_numpy()] > 0)).group_by("settlement_point_id").agg(pl.col("cut").any())
    is_cut = points.join(cut, on="settlement_point_id", how="left")["cut"].fill_null(False).to_numpy()
    for label, mask in (("points_cut_off_by_a_binding_contingency", is_cut), ("other_points", ~is_cut)):
        result[label] = {"n": int(mask.sum()), "p50_abs": round(float(np.median(resid[mask])), 3) if mask.any() else None,
                         "max_abs": round(float(resid[mask].max()), 2) if mask.any() else None, "beyond_5": int((resid[mask] > 5).sum()),
                         "p50_abs_plain_weights": round(float(np.median(plain[mask])), 3) if mask.any() else None,
                         "max_abs_plain_weights": round(float(plain[mask].max()), 2) if mask.any() else None}
    result["cut_off_under_binding_contingencies"] = cut_lists

    # Settlement points ERCOT de-energized in the base case (NP4-200-CD) against the points our network has no node for (NAM-07, TOP-05).
    dead = set(hour_rows(session, "dam_deenergized_settlement_points", start_utc)["settlement_point"])
    ours_dead = set(net.settlement_points.filter(pl.col("n_nodes") == 0)["settlement_point_id"])
    in_points = points["settlement_point_id"].is_in(list(dead)).to_numpy()
    result["deenergized_in_base_case"] = {"published": len(dead), "among_points_compared": int(in_points.sum()), "ours_without_node": len(ours_dead),
                                          "agree": len(dead & ours_dead), "only_ours": len(ours_dead - dead), "only_ercot": len(dead - ours_dead),
                                          "residual_p50_abs": round(float(np.median(resid[in_points])), 3) if in_points.any() else None}

    # Electrically similar settlement points (NP4-158-SG) should share a published price.
    groups = hour_rows(session, "dam_electrically_similar_settlement_points", start_utc).select("settlement_point", "group_index")
    spread = (prices.join(groups, on="settlement_point").group_by("group_index")
              .agg((pl.col("settlement_point_price").max() - pl.col("settlement_point_price").min()).alias("spread")))
    result["electrically_similar_groups"] = {"groups_priced": spread.height, "groups_with_price_spread_over_1_cent": int((spread["spread"] > 0.01).sum()),
                                             "largest_spread": round(float(spread["spread"].max()), 2) if spread.height else None}
    return result


def _renormalized(W: sparse.csr_matrix, connected: np.ndarray | None) -> sparse.csr_matrix:
    """PRC-03: under a contingency that cuts buses off, a point's weights over the buses still connected, summing to one again.

    Protocols 4.6.1.2 and 3.5.2 make the distribution factors per constraint, over energized buses;
    a point with no bus left has no sensitivity to that constraint.
    """
    if connected is None or connected.all():
        return W
    Wc = W @ sparse.diags(connected.astype(float))
    kept = np.asarray(Wc.sum(axis=1)).ravel()
    return sparse.diags(np.divide(1.0, kept, out=np.zeros_like(kept), where=kept > 0)) @ Wc


def _quantiles(values: np.ndarray) -> dict:
    a = np.abs(values)
    return {"p50_abs": round(float(np.quantile(a, 0.5)), 3), "p90_abs": round(float(np.quantile(a, 0.9)), 3),
            "p99_abs": round(float(np.quantile(a, 0.99)), 3), "max_abs": round(float(a.max()), 2),
            "within_0.01": int((a <= 0.01).sum()), "within_1.00": int((a <= 1.0).sum())}


def _summary(values: np.ndarray, within: float) -> dict:
    return {"n": int(values.size), "p50_abs": round(float(np.median(values)), 3) if values.size else None,
            "max_abs": round(float(values.max()), 2) if values.size else None, f"within_{within}": int((values <= within).sum())}


def hour_ids(session, day: date) -> list[str]:
    """The latest revision of every DAM hour of one operating day."""
    snaps = session.core("snapshot").filter((pl.col("model_kind") == "dam") & (pl.col("operating_date") == day)).collect()
    return snaps.sort("hour", "revision").unique(subset=["hour"], keep="last").sort("hour")["snapshot_id"].to_list()


def days_with_prices(session) -> list[date]:
    """Operating days with both a DAM model and archived shadow prices, oldest first."""
    posted = {datetime.strptime(d, "%m/%d/%Y").date() for d in session.raw("dam_shadow_prices").select("delivery_date").unique().collect()["delivery_date"]}
    modelled = set(session.core("snapshot").filter(pl.col("model_kind") == "dam").select("operating_date").unique().collect()["operating_date"])
    return sorted(posted & modelled)


def run(session, day: date) -> list[dict]:
    """Every hour of one day; writes ``data/reports/prices/<snapshot>.json`` per hour with prices."""
    results = [r for sid in hour_ids(session, day) if (r := check_hour(session, sid)) is not None]
    for r in results:
        write_report(session, "prices", r["snapshot_id"], r)
    return results


def summary_line(day: date, results: list[dict]) -> str:
    """One log line for a day: the residual's typical, tail and worst values at the worst hour."""
    if not results:
        return f"price check {day}: no hours with prices and a model"
    best = [r["residual"] for r in results]
    gtc_hours = sum(1 for r in results if any(k.startswith("used_gtc") for k in r["rows_by_outcome"]))
    skipped = [r["share_of_shadow_price_used"] for r in results if (r["share_of_shadow_price_used"] or 1.0) < 1.0]
    sources = "/".join(sorted({r["system_price"]["source"] for r in results}))
    note = f", {len(skipped)} hours with shadow price not applied (least share {min(skipped):.2f})" if skipped else ""
    return (f"price check {day}: {len(results)} hours, p50 {max(b['p50_abs'] for b in best):.2f}, p99 {max(b['p99_abs'] for b in best):.2f}, "
            f"max {max(b['max_abs'] for b in best):.2f} $/MWh (worst hour), {gtc_hours} hours with a binding GTC, system price {sources}{note}")
