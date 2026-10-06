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
names and the contingency definitions are right, it is zero at every point.

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

import ercot_mis as em
from ercot_mis.out.network import Network
from ercot_mis.shift_factors import DcSystem

BASE_CASE = "BASECASE"


def key(expr: pl.Expr) -> pl.Expr:
    return expr.str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def hour_rows(frame: pl.LazyFrame, day: date, hour: int) -> pl.DataFrame:
    """One delivery hour of a raw price table; if a day was posted twice, one posting only."""
    rows = frame.filter((pl.col("delivery_date") == f"{day:%m/%d/%Y}")
                        & (pl.col("hour_ending").str.split(":").list.first().cast(pl.Int64, strict=False) == hour)).collect()
    if rows.is_empty():
        return rows
    return rows.filter(pl.col("doc_id") == str(rows["doc_id"].cast(pl.Int64).max()))


def borrowed_gtcs(session, snapshot_id: str, net: Network) -> dict[str, tuple[list[tuple[int, float]], int]]:
    """GTCs as signed DAM branch sets, borrowed from the CRR model of the same month.

    The DAM package carries no GTC members. The CRR package does, under the codes the
    shadow price file uses; each member is translated through ``core.match_branch`` and
    its factor re-signed when the DAM branch runs the other way (its ends
    translated through ``core.match_bus``). Returns ``{gtc code: ([(branch index, factor)], members lost)}``.
    """
    day = snapshot_id.split(":")[1]
    crr_ids = session.core("snapshot").filter(pl.col("snapshot_id").str.starts_with(f"crr:monthly:{day[:7]}:")).collect()["snapshot_id"].sort()
    if crr_ids.is_empty():
        return {}
    crr = session.network(crr_ids[-1])
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
    _, day_text, he, _ = snapshot_id.split(":")
    day, hour = date.fromisoformat(day_text), int(he[2:])
    shadow = hour_rows(session.raw("dam_shadow_prices"), day, hour)
    prices = hour_rows(session.raw("dam_settlement_point_prices"), day, hour)
    if shadow.is_empty() or prices.is_empty():
        return None
    # The system price (NP4-523-CD) is published; before it was pulled it was fitted as the median difference.
    try:
        lam = hour_rows(session.raw("dam_system_lambda"), day, hour)
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
    dropped_ctg = set(net.dropped_contingencies.select(key(pl.col("contingency_id")))["contingency_id"])

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

    congestion = np.zeros(net.n_nodes)
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
            congestion += mu * sum((factor * system.shift_factors([j])[0] for j, factor in members), np.zeros(net.n_nodes))
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
                    systems[ctg] = system.outaged(ctg_of[ctg], splits.get(ctg, ()))
                    islanded_nodes = max(islanded_nodes, systems[ctg].n_islanded)
                solver = systems[ctg]
                if not (solver.connected[f] and solver.connected[t]):
                    why, solver = "constraint_islanded_by_its_contingency", None
                else:
                    why = "used_contingency_islanding" if solver.n_islanded else "used_contingency_split_bus" if splits.get(ctg) else "used_contingency"
        outcome[why] = outcome.get(why, 0) + 1
        mu_by_outcome[why] = round(mu_by_outcome.get(why, 0.0) + mu, 2)
        if solver is not None:
            congestion += sign * mu * solver.shift_factors([j])[0]
            if why == "used_contingency_islanding":
                cut_off_mu += mu * ~solver.connected[:net.n_nodes]
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
    def residual_for(sign: float) -> np.ndarray:
        base = observed - sign * c
        return base - (system_price if system_price is not None else np.median(base))

    def quantiles(values: np.ndarray) -> dict:
        a = np.abs(values)
        return {"p50_abs": round(float(np.quantile(a, 0.5)), 3), "p90_abs": round(float(np.quantile(a, 0.9)), 3),
                "p99_abs": round(float(np.quantile(a, 0.99)), 3), "max_abs": round(float(a.max()), 2),
                "within_0.01": int((a <= 0.01).sum()), "within_1.00": int((a <= 1.0).sum())}

    for sign, label in ((-1.0, "minus"), (1.0, "plus")):
        result[f"residual_{label}"] = quantiles(residual_for(sign))
    best = min((-1.0, 1.0), key=lambda s: np.abs(residual_for(s)).sum())
    centred = residual_for(best)
    resid = np.abs(centred)
    result["sign"] = "minus" if best < 0 else "plus"
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
        detail.update(points=points.with_columns(pl.Series("residual", centred)), net=net, system=system, gtc_rows=gtc_rows, weights=weights,
                      gtc_binding={n: mu for n, _, mu, *_ in rows if n in gtc_rows})
    # Points on a node that some binding row's contingency cuts off, against the rest.
    cut = (weights.with_columns(pl.Series("cut", cut_off_mu[weights["node_index"].to_numpy()] > 0)).group_by("settlement_point_id").agg(pl.col("cut").any()))
    is_cut = points.join(cut, on="settlement_point_id", how="left")["cut"].fill_null(False).to_numpy()
    for label, mask in (("points_cut_off_by_a_binding_contingency", is_cut), ("other_points", ~is_cut)):
        result[label] = {"n": int(mask.sum()), "p50_abs": round(float(np.median(resid[mask])), 3) if mask.any() else None,
                         "max_abs": round(float(resid[mask].max()), 2) if mask.any() else None, "beyond_5": int((resid[mask] > 5).sum())}
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
    best = [min(r["residual_minus"], r["residual_plus"], key=lambda x: x["p99_abs"]) for r in results]
    sources = {r["system_price"]["source"] for r in results}
    gtc_hours = sum(1 for r in results if any(k.startswith("used_gtc") for k in r["rows_by_outcome"]))
    return (f"price check {day}: {len(results)} hours, p50 {max(b['p50_abs'] for b in best):.2f}, p99 {max(b['p99_abs'] for b in best):.2f}, "
            f"max {max(b['max_abs'] for b in best):.2f} $/MWh (worst hour), {gtc_hours} hours with a binding GTC, system price {'/'.join(sorted(sources))}")


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
