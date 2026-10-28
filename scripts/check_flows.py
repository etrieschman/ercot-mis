"""The flow check: do the DAM's own awards, pushed through our network, reproduce the flows ERCOT reports?

For one DAM hour this script takes the net injection per settlement point from the 60-Day
DAM Disclosure (``session.injections``, INJ-01), spreads it onto nodes with the network's
settlement point weights, and solves DC flows on ``session.network`` (base case, and each
binding contingency with its branches removed, as the price check does). Then it compares:

- **binding rows**: every row of the DAM's binding constraints (NP4-191-CD) carries the
  flow ERCOT computed at the constraint (``constraint_value``) next to its limit. Our flow on
  the same branch (or GTC, with the CRR model's members, PRC-04), under the same contingency
  and in the row's direction, should equal it. This tests injections, settlement point
  weights, topology and contingency definitions at once, on the constraints that matter;
- **limits**: on every branch the network enforces, base-case flow against the base limit.
  The DAM enforced these, so nothing should sit above its limit in the base case;
- **placement**: the injection the network cannot place (a settlement point without a node)
  and the MW the slack absorbs. Both should be zero; when they are not, the flows above are
  off by that much.

It is a report with no threshold. It needs no prices beyond the binding rows and no
re-clearing. The slack absorbs whatever does not balance, so a placement gap shows up as
slack MW and as a flow error near the slack.

Prints counts and quantiles only; writes ``data/reports/flows/<snapshot>.json``.

    uv run python scripts/check_flows.py                          # hour 12 of the newest day with awards and a model
    uv run python scripts/check_flows.py --dam dam:2026-08-20:he12:r1
    uv run python scripts/check_flows.py --day 2026-08-20          # every hour of a day
    uv run python scripts/check_flows.py --all                     # every day with awards and a model
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import numpy as np
import polars as pl

import ercot_mis as em
from ercot_mis.out.network import Network
from ercot_mis.shift_factors import DcSystem

from check_prices import BASE_CASE, borrowed_gtcs, hour_ids, hour_rows, key, row_direction

LOADING_BINS = [0.0, 0.5, 0.9, 1.0, 1.05, 1.2, np.inf]


def days_with_flows(session) -> list[date]:
    """Operating days with both a DAM model and disclosure awards, oldest first."""
    try:
        awarded = {datetime.strptime(d, "%m/%d/%Y").date() for d in session.core("hourly_award").select("delivery_date").unique().collect()["delivery_date"]}
    except FileNotFoundError:
        return []
    modelled = set(session.core("snapshot").filter(pl.col("model_kind") == "dam").select("operating_date").unique().collect()["operating_date"])
    return sorted(awarded & modelled)


def node_injections(net: Network, q: pl.DataFrame) -> tuple[np.ndarray, dict]:
    """``q`` per settlement point onto the network's nodes, and what could not be placed.

    A point is placed through ``settlement_point_nodes`` (weights sum to one per point). A point
    the network lists with no node, or does not list at all, is unplaced; its MW is counted by
    the point's kind and by award kind, and left out of the node vector.
    """
    kinds = [c for c in q.columns if c not in ("interval_start_utc", "delivery_date", "hour_ending", "settlement_point", "net_mw")]
    rows = (q.join(net.settlement_points.select(pl.col("settlement_point_id").alias("settlement_point"), "kind", "n_nodes"), on="settlement_point", how="left")
            .with_columns(pl.col("kind").fill_null("not_in_model"), pl.col("n_nodes").fill_null(0)))
    placed = rows.filter(pl.col("n_nodes") > 0)
    unplaced = rows.filter(pl.col("n_nodes") == 0)
    spread = placed.select("settlement_point", "net_mw").join(net.settlement_point_nodes.rename({"settlement_point_id": "settlement_point"}), on="settlement_point")
    x = np.zeros(net.n_nodes)
    np.add.at(x, spread["node_index"].to_numpy(), (spread["net_mw"] * spread["weight"]).to_numpy())
    by_kind = unplaced.group_by("kind").agg(pl.len().alias("points"), pl.col("net_mw").sum().round(1).alias("net_mw"), pl.col("net_mw").abs().sum().round(1).alias("abs_mw")).sort("kind")
    info = {
        "points_with_award": rows.height, "points_placed": placed.height, "points_unplaced": unplaced.height,
        "net_mw_all_points": round(float(rows["net_mw"].sum()), 1),
        "unplaced_net_mw": round(float(unplaced["net_mw"].sum()), 1), "unplaced_abs_mw": round(float(unplaced["net_mw"].abs().sum()), 1),
        "placed_abs_mw": round(float(placed["net_mw"].abs().sum()), 1),
        "unplaced_by_point_kind": {r["kind"]: {k: v for k, v in r.items() if k != "kind"} for r in by_kind.to_dicts()},
        "unplaced_by_award_kind": {k: round(float(unplaced[k].fill_null(0).sum()), 1) for k in kinds},
        "slack_mw": round(float(-x.sum()), 1),
    }
    return x, info


def quantiles(values: np.ndarray) -> dict | None:
    if values.size == 0:
        return None
    a = np.abs(values)
    return {"n": int(a.size), "p50_abs": round(float(np.quantile(a, 0.5)), 1), "p90_abs": round(float(np.quantile(a, 0.9)), 1),
            "max_abs": round(float(a.max()), 1), "median_signed": round(float(np.median(values)), 1)}


def check_hour(session, snapshot_id: str, q_day: pl.DataFrame | None = None) -> dict | None:
    snap = session.core("snapshot").filter(pl.col("snapshot_id") == snapshot_id).collect()
    if snap.is_empty():
        raise KeyError(f"{snapshot_id!r} is not in core.snapshot")
    start_utc, day = snap["interval_start_utc"][0], snap["operating_date"][0]
    q_day = session.injections(day) if q_day is None else q_day
    q = q_day.filter(pl.col("interval_start_utc") == start_utc)
    if q.is_empty():
        return None
    net: Network = session.network(snapshot_id)
    system = DcSystem(net)
    x, placement = node_injections(net, q)
    base = system.flows(x)
    result = {"snapshot_id": snapshot_id, "injections": placement, "nodes": net.n_nodes, "branches": net.n_branches}

    # Limits: base-case loading of every branch the network enforces.
    b = net.branches
    limited = b.filter(pl.col("is_limited") & pl.col("base_limit_mw").is_not_null() & (pl.col("base_limit_mw") > 0))
    idx, limit = limited["index"].to_numpy(), limited["base_limit_mw"].to_numpy()
    loading = np.abs(base[idx]) / limit
    hist, _ = np.histogram(loading, bins=LOADING_BINS)
    result["base_case_limits"] = {
        "branches_limited": int(idx.size),
        "loading_histogram": {f"<{e:g}": int(h) for h, e in zip(hist, LOADING_BINS[1:])},
        "above_limit": int((loading > 1.0).sum()), "max_loading": round(float(loading.max()), 3) if idx.size else None,
        "mw_above_limit_total": round(float(np.clip(np.abs(base[idx]) - limit, 0, None).sum()), 1),
    }

    # Binding rows: our flow at each constraint, in the row's direction and under its contingency, against ERCOT's.
    shadow = hour_rows(session.raw("dam_shadow_prices"), start_utc, session)
    if shadow.is_empty():
        result["binding_rows"] = None
        return result
    substation = dict(net.nodes.select("index", key(pl.col("substation"))).rows())
    kv = dict(net.nodes.select("index", "kv").rows())
    branch_of = {k: (j, f, t) for k, j, f, t in b.select(key(pl.col("branch_id")), "index", "from_index", "to_index").rows()}
    ctg_of = {k: idx for k, idx in net.contingencies.select(key(pl.col("contingency_id")), "branch_indexes").rows()}
    ctg_missing = {k: int(u) + int(d) for k, u, d in net.contingencies.select(key(pl.col("contingency_id")), "n_unresolved", "n_dropped").rows()}
    dropped_ctg = {k: int(d) + int(u) for k, d, u in net.dropped_contingencies.select(key(pl.col("contingency_id")), "n_dropped", "n_unresolved").rows()}
    splits = {c: list(zip(i, e)) for c, i, e in net.contingencies.select(key(pl.col("contingency_id")), "split_branch_indexes", "split_ends").rows() if i}
    gtc_rows = borrowed_gtcs(session, snapshot_id, net)
    flows_under: dict[str, tuple[DcSystem, np.ndarray]] = {}

    outcome: dict[str, int] = {}
    compared = []  # (ours, ercot value, limit, shadow price, outcome)
    rows = shadow.select(key(pl.col("constraint_name")), key(pl.col("contingency_name")), "shadow_price", "constraint_limit", "constraint_value",
                         key(pl.col("from_station")), key(pl.col("to_station")), "from_station_kv", "to_station_kv").rows()
    for name, ctg, mu, lim, value, from_station, to_station, from_kv, to_kv in rows:
        ours = None
        if name in gtc_rows:
            members, n_missing = gtc_rows[name]
            why = "used_gtc_crr_members" if not n_missing else "used_gtc_crr_members_incomplete"
            ours = float(sum(factor * base[j] for j, factor in members))
        elif name not in branch_of:
            why = "constraint_not_a_branch"
        else:
            j, f, t = branch_of[name]
            sign = row_direction(substation, kv, f, t, from_station, to_station, from_kv, to_kv)
            if sign is None:
                why = "direction_not_recognized"
            elif ctg == BASE_CASE:
                why, ours = "used_base_case", sign * base[j]
            elif ctg in dropped_ctg:
                why, ours = (("binding_under_contingency_absent_in_model", None) if dropped_ctg[ctg]
                             else ("used_non_topological_contingency", sign * base[j]))
            elif ctg not in ctg_of:
                why = "contingency_unknown"
            elif j in ctg_of[ctg]:
                why = "constraint_is_outaged_by_its_contingency"
            else:
                if ctg not in flows_under:
                    solver = system.outaged(ctg_of[ctg], splits.get(ctg, ()))
                    flows_under[ctg] = (solver, solver.flows(x))
                solver, post = flows_under[ctg]
                if not (solver.connected[f] and solver.connected[t]):
                    why = "constraint_islanded_by_its_contingency"
                else:
                    why = ("used_contingency_partial" if ctg_missing.get(ctg) else "used_contingency_islanding" if solver.n_islanded
                           else "used_contingency_split_bus" if splits.get(ctg) else "used_contingency")
                    ours = sign * post[j]
        outcome[why] = outcome.get(why, 0) + 1
        if ours is not None and value is not None:
            compared.append((float(ours), float(value), float(lim) if lim is not None else np.nan, float(mu), why))

    diff = np.array([o - v for o, v, *_ in compared])
    ours_arr = np.array([o for o, *_ in compared])
    lim_arr = np.array([c[2] for c in compared])
    value_arr = np.array([c[1] for c in compared])
    with np.errstate(invalid="ignore", divide="ignore"):
        ours_loading = np.abs(ours_arr) / np.abs(lim_arr)
    result["binding_rows"] = {
        "rows": shadow.height, "rows_by_outcome": dict(sorted(outcome.items())), "rows_compared": len(compared),
        "contingencies_solved": len(flows_under),
        "ours_minus_ercot_mw": quantiles(diff),
        "within_1_mw": int((np.abs(diff) <= 1).sum()), "within_5_percent_of_limit": int((np.abs(diff) <= 0.05 * np.abs(lim_arr)).sum()),
        "ours_opposite_sign": int((np.sign(ours_arr) != np.sign(value_arr)).sum()),
        "ours_loading_histogram": ({f"<{e:g}": int(h) for h, e in zip(np.histogram(ours_loading[np.isfinite(ours_loading)], bins=LOADING_BINS)[0], LOADING_BINS[1:])}
                                   if compared else None),
        "ercot_value_over_limit_max": round(float(np.nanmax(np.abs(value_arr) / np.abs(lim_arr))), 3) if compared else None,
        "by_outcome": {w: quantiles(np.array([o - v for o, v, _, _, why in compared if why == w])) for w in sorted({c[4] for c in compared})},
    }
    return result


def run(session, snapshot_ids: list[str], *, quiet: bool = False) -> list[dict]:
    """Check each hour, write ``data/reports/flows/<snapshot>.json``, return the results (hours without awards skipped)."""
    out = session.data_dir / "reports" / "flows"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    results, q_by_day = [], {}
    for snapshot_id in snapshot_ids:
        day = date.fromisoformat(snapshot_id.split(":")[1])
        if day not in q_by_day:
            q_by_day = {day: session.injections(day)}
        result = check_hour(session, snapshot_id, q_by_day[day])
        if result is None:
            if not quiet:
                print(f"{snapshot_id}: no awards for this hour")
            continue
        if not quiet:
            print(json.dumps(result, indent=1))
        (out / f"{snapshot_id.replace(':', '-')}.json").write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1))
        results.append(result)
    return results


def summary_line(day: date, results: list[dict]) -> str:
    """One log line for a day: binding-row flow error, base-case overloads and unplaced MW, each at its worst hour."""
    if not results:
        return f"flow check {day}: no hours with awards and a model"
    bind = [r["binding_rows"] for r in results if r.get("binding_rows") and r["binding_rows"]["ours_minus_ercot_mw"]]
    err = (f"binding rows |ours - ERCOT| p50 {max(b['ours_minus_ercot_mw']['p50_abs'] for b in bind):.0f}, "
           f"max {max(b['ours_minus_ercot_mw']['max_abs'] for b in bind):.0f} MW" if bind else "no binding rows compared")
    over = max(r["base_case_limits"]["above_limit"] for r in results)
    unplaced = max(r["injections"]["unplaced_abs_mw"] for r in results)
    slack = max(abs(r["injections"]["slack_mw"]) for r in results)
    return (f"flow check {day}: {len(results)} hours, {err}; up to {over} enforced branches above limit in the base case; "
            f"unplaced up to {unplaced:.0f} MW, slack up to {slack:.0f} MW (worst hour)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dam", default=None, help="DAM snapshot id (default: hour 12 of the newest day with awards and a model)")
    parser.add_argument("--day", default=None, help="check all 24 hours of this operating date")
    parser.add_argument("--all", action="store_true", help="check every hour of every day with awards and a model")
    args = parser.parse_args()
    with em.open() as session:
        if args.all:
            for day in days_with_flows(session):
                print(summary_line(day, run(session, hour_ids(session, day), quiet=True)), flush=True)
            return
        if args.day:
            ids = hour_ids(session, date.fromisoformat(args.day))
        elif args.dam:
            ids = [args.dam]
        else:
            days = days_with_flows(session)
            ids = [i for i in hour_ids(session, days[-1]) if i.split(":")[2] == "he12"] if days else []
        run(session, ids)


if __name__ == "__main__":
    main()
