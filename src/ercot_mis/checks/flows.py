"""The flow check: do the DAM's own awards, pushed through our network, give the flows ERCOT reports?

For one DAM hour: the net injection per settlement point from the 60-Day DAM Disclosure
(``session.injections``, INJ-01) is spread onto nodes by the network's settlement point
weights and solved as DC flows, in the base case and under each binding contingency. Then:

- **binding rows** (FLW-02): our flow at each constraint of NP4-191-CD, in the row's
  direction and under its contingency, against the flow ERCOT computed there;
- **limits**: base-case flow on every branch the network enforces, against its limit;
- **placement** (FLW-01): the MW the network cannot place, and the MW the slack absorbs.
  Both should be zero; when they are not, every flow is off by up to that much.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import polars as pl

from ..out.network import Network
from ..out.shift_factors import DcSystem
from . import write_report
from .binding import hour_rows, resolve
from .prices import hour_ids

LOADING_BINS = [0.0, 0.5, 0.9, 1.0, 1.05, 1.2, np.inf]  # loading is rounded to 6 decimals first: a branch at its limit stays at it


def check_hour(session, snapshot_id: str, q_day: pl.DataFrame) -> dict | None:
    """The flow check for one DAM hour, given the day's injections; None when the hour has no awards."""
    snap = session.snapshot(snapshot_id)
    q = q_day.filter(pl.col("interval_start_utc") == snap["interval_start_utc"])
    if q.is_empty():
        return None
    net = session.network(snapshot_id)
    system = DcSystem(net)
    x, placement = node_injections(net, q)
    base = system.flows(x)
    result = {"snapshot_id": snapshot_id, "injections": placement, "nodes": net.n_nodes, "branches": net.n_branches}

    limited = net.branches.filter(pl.col("is_limited") & (pl.col("base_limit_mw") > 0))
    j, limit = limited["index"].to_numpy(), limited["base_limit_mw"].to_numpy()
    loading = np.round(np.abs(base[j]) / limit, 6)
    result["base_case_limits"] = {"branches_limited": int(j.size), "loading_histogram": _histogram(loading),
                                  "above_limit": int((loading > 1).sum()), "max_loading": round(float(loading.max()), 3) if j.size else None,
                                  "mw_above_limit_total": round(float(np.clip(np.abs(base[j]) - limit, 0, None).sum()), 1)}

    shadow = hour_rows(session, "dam_shadow_prices", snap["interval_start_utc"])
    if shadow.is_empty():
        result["binding_rows"] = None
        return result
    resolved = resolve(session, snapshot_id, net, system, shadow)
    flows = {None: base}  # per contingency solved: the post-contingency flows
    compared = []  # (ours, ERCOT's, limit, outcome)
    for row in resolved.bindings:
        if row.system is None or row.flow_mw is None:
            continue
        if row.gtc_members:
            ours = sum(factor * base[k] for k, factor in row.gtc_members)
        else:
            key = row.contingency if row.system is not system else None
            if key not in flows:
                flows[key] = row.system.flows(x)
            ours = row.sign * flows[key][row.branch]
        compared.append((float(ours), float(row.flow_mw), float(row.limit_mw) if row.limit_mw is not None else np.nan, row.outcome))

    outcome: dict[str, int] = {}
    for row in resolved.bindings:
        outcome[row.outcome] = outcome.get(row.outcome, 0) + 1
    ours, theirs, lim = (np.array([c[i] for c in compared]) for i in range(3))
    diff = ours - theirs
    with np.errstate(invalid="ignore", divide="ignore"):
        ours_loading = np.round(np.abs(ours) / np.abs(lim), 6)
    result["binding_rows"] = {
        "rows": shadow.height, "rows_by_outcome": dict(sorted(outcome.items())), "rows_compared": len(compared),
        "contingencies_solved": len(resolved.systems), "ours_minus_ercot_mw": _quantiles(diff),
        "within_1_mw": int((np.abs(diff) <= 1).sum()), "within_5_percent_of_limit": int((np.abs(diff) <= 0.05 * np.abs(lim)).sum()),
        "ours_opposite_sign": int((np.sign(ours) != np.sign(theirs)).sum()),
        "ours_loading_histogram": _histogram(ours_loading[np.isfinite(ours_loading)]) if compared else None,
        "ercot_value_over_limit_max": round(float(np.nanmax(np.abs(theirs) / np.abs(lim))), 3) if compared else None,
        "by_outcome": {w: _quantiles(np.array([o - t for o, t, _, why in compared if why == w])) for w in sorted({c[3] for c in compared})},
    }
    return result


def node_injections(net: Network, q: pl.DataFrame) -> tuple[np.ndarray, dict]:
    """``q`` per settlement point spread onto the network's nodes, and a summary of what could not be placed (FLW-01)."""
    kinds = [c for c in q.columns if c not in ("interval_start_utc", "delivery_date", "hour_ending", "settlement_point", "net_mw")]
    rows = (q.join(net.settlement_points.select(pl.col("settlement_point_id").alias("settlement_point"), "kind", "n_nodes"), on="settlement_point", how="left")
            .with_columns(pl.col("kind").fill_null("not_in_model"), pl.col("n_nodes").fill_null(0)))
    placed, unplaced = rows.filter(pl.col("n_nodes") > 0), rows.filter(pl.col("n_nodes") == 0)
    spread = placed.select("settlement_point",
                           "net_mw").join(net.settlement_point_nodes.rename({"settlement_point_id": "settlement_point"}), on="settlement_point")
    x = np.zeros(net.n_nodes)
    np.add.at(x, spread["node_index"].to_numpy(), (spread["net_mw"] * spread["weight"]).to_numpy())
    by_kind = unplaced.group_by("kind").agg(pl.len().alias("points"), pl.col("net_mw").sum().round(1).alias("net_mw"),
                                            pl.col("net_mw").abs().sum().round(1).alias("abs_mw")).sort("kind")
    return x, {
        "points_with_award": rows.height, "points_placed": placed.height, "points_unplaced": unplaced.height,
        "net_mw_all_points": round(float(rows["net_mw"].sum()), 1),
        "unplaced_net_mw": round(float(unplaced["net_mw"].sum()), 1), "unplaced_abs_mw": round(float(unplaced["net_mw"].abs().sum()), 1),
        "placed_abs_mw": round(float(placed["net_mw"].abs().sum()), 1),
        "unplaced_by_point_kind": {r.pop("kind"): r for r in by_kind.to_dicts()},
        "unplaced_by_award_kind": {k: round(float(unplaced[k].fill_null(0).sum()), 1) for k in kinds},
        "slack_mw": round(float(-x.sum()), 1),
    }


def _histogram(loading: np.ndarray) -> dict:
    counts, _ = np.histogram(loading, bins=LOADING_BINS)
    return {f"<{e:g}": int(n) for n, e in zip(counts, LOADING_BINS[1:])}


def _quantiles(values: np.ndarray) -> dict | None:
    if values.size == 0:
        return None
    a = np.abs(values)
    return {"n": int(a.size), "p50_abs": round(float(np.quantile(a, 0.5)), 1), "p90_abs": round(float(np.quantile(a, 0.9)), 1),
            "max_abs": round(float(a.max()), 1), "median_signed": round(float(np.median(values)), 1)}


def days_with_flows(session) -> list[date]:
    """Operating days with both a DAM model and disclosure awards, oldest first."""
    awarded = {datetime.strptime(d, "%m/%d/%Y").date() for d in session.core("hourly_award").select("delivery_date").unique().collect()["delivery_date"]}
    modelled = set(session.core("snapshot").filter(pl.col("model_kind") == "dam").select("operating_date").unique().collect()["operating_date"])
    return sorted(awarded & modelled)


def run(session, day: date) -> list[dict]:
    """Every hour of one day; writes ``data/reports/flows/<snapshot>.json`` per hour with awards."""
    q_day = session.injections(day)
    results = [r for sid in hour_ids(session, day) if (r := check_hour(session, sid, q_day)) is not None]
    for r in results:
        write_report(session, "flows", r["snapshot_id"], r)
    return results


def summary_line(day: date, results: list[dict]) -> str:
    """One log line for a day: binding-row flow error, base-case overloads and unplaced MW, each at its worst hour."""
    if not results:
        return f"flow check {day}: no hours with awards and a model"
    bind = [r["binding_rows"]["ours_minus_ercot_mw"] for r in results if r["binding_rows"] and r["binding_rows"]["ours_minus_ercot_mw"]]
    err = f"binding rows |ours - ERCOT| p50 {max(b['p50_abs'] for b in bind):.0f}, max {max(b['max_abs'] for b in bind):.0f} MW" if bind else "no binding rows compared"
    over = max(r["base_case_limits"]["above_limit"] for r in results)
    unplaced = max(r["injections"]["unplaced_abs_mw"] for r in results)
    slack = max(abs(r["injections"]["slack_mw"]) for r in results)
    return (f"flow check {day}: {len(results)} hours, {err}; up to {over} enforced branches above limit in the base case; "
            f"unplaced up to {unplaced:.0f} MW, slack up to {slack:.0f} MW (worst hour)")
