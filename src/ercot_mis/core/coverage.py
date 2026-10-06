"""What a snapshot's core tables carry and what they lack: counts, nothing else.

The viewer shows these beside each model and ``scripts/measure_identity.py`` records
them, so a sparse table is seen as a fact about the source rather than mistaken for a
bug. Every value is a count; a key says what is counted. The DAM RAW gives every load
0 MW (the DAM distributes load through its distribution factors), so ``loads_with_mw``
is a property of the model, not of the parser.
"""

from __future__ import annotations

import polars as pl

VERSION = 1


def _n(frame: pl.DataFrame, expr: pl.Expr) -> int:
    return int(frame.select(expr.fill_null(False).sum()).item()) if frame.height else 0


def snapshot_coverage(node: pl.DataFrame, branch: pl.DataFrame, rating: pl.DataFrame, load: pl.DataFrame,
                      points: pl.DataFrame, point_bus: pl.DataFrame, outages: pl.DataFrame,
                      enforced: pl.DataFrame | None = None) -> dict[str, int]:
    """Counts for one snapshot. ``enforced`` is ``out.network``'s ``branches`` frame when a network was assembled."""
    raw = rating.filter(pl.col("rating_source") == "psse_raw") if rating.height else rating
    csv = rating.filter(pl.col("rating_source") == "crr_monitored") if rating.height else rating
    out: dict[str, int] = {
        "nodes": node.height,
        "nodes_with_substation": _n(node, pl.col("substation").is_not_null()) if "substation" in node.columns else 0,
        "buses": int(node["bus_key"].n_unique()) if node.height else 0,
        "branches": branch.height,
        "branches_in_service": _n(branch, pl.col("is_in_service")),
        "branches_tie": _n(branch, pl.col("is_tie")) if "is_tie" in branch.columns else 0,
        "branches_monitored": _n(branch, pl.col("is_monitored")),
        "branches_secured": _n(branch, pl.col("is_secured")) if "is_secured" in branch.columns else 0,
        "branches_with_raw_rate_a": int(raw.filter(pl.col("base_mw") > 0)["branch_id"].n_unique()) if raw.height else 0,
        "branches_with_crr_csv_rating": int(csv["branch_id"].n_unique()) if csv.height else 0,
        # RAT-05's two fallback rules, counted so their reach is visible
        "branches_rate_a_zero_rate_b_positive": _n(raw, ((pl.col("base_mw").fill_null(0) <= 0) & (pl.col("emergency_mw") > 0))) if raw.height else 0,
        "branches_rate_b_missing_or_zero": _n(raw, (pl.col("base_mw") > 0) & (pl.col("emergency_mw").fill_null(0) <= 0)) if raw.height else 0,
        "branches_with_enforced_limit": _n(enforced, pl.col("is_limited")) if enforced is not None else 0,
        # RAT-12: source rows a de-duplication quietly resolves (a device listed twice in one rating block)
        "branch_rating_duplicate_rows": (int(rating.group_by(["branch_id", "rating_source", "time_of_use"]).len().filter(pl.col("len") > 1)["len"].sum()
                                             - rating.group_by(["branch_id", "rating_source", "time_of_use"]).len().filter(pl.col("len") > 1).height) if rating.height else 0),
        "loads": load.height,
        "loads_in_service": _n(load, pl.col("is_in_service")),
        "loads_with_mw": _n(load, pl.col("mw") > 0),
        "loads_with_zone": _n(load, pl.col("load_zone").is_not_null()) if "load_zone" in load.columns else 0,
        "settlement_points": points.height,
        "settlement_points_resolved": int(point_bus.filter(pl.col("is_resolved"))["settlement_point_id"].n_unique()) if point_bus.height else 0,
        "contingencies": int(outages["contingency_id"].n_unique()) if outages.height else 0,
        "contingencies_with_unresolved_rows": (int(outages.filter(~pl.col("is_resolved"))["contingency_id"].n_unique())
                                               if outages.height and "is_resolved" in outages.columns else 0),
    }
    return out


def pair_coverage(branches: pl.DataFrame, buses: pl.DataFrame, contingencies: pl.DataFrame) -> dict[str, int]:
    """Counts for a (CRR, DAM) pair from the three match tables: matched and unmatched on each side."""
    def side(frame: pl.DataFrame, crr: str, dam: str) -> dict[str, int]:
        both = frame.filter(pl.col(crr).is_not_null() & pl.col(dam).is_not_null())
        return {"matched": both.height, "crr_unmatched": _n(frame, pl.col(dam).is_null() & pl.col(crr).is_not_null()),
                "dam_unmatched": _n(frame, pl.col(crr).is_null() & pl.col(dam).is_not_null())}
    out = {}
    for name, frame, crr, dam in (("branches", branches, "crr_branch_id", "dam_branch_id"), ("buses", buses, "crr_bus_key", "dam_bus_key"),
                                  ("contingencies", contingencies, "crr_contingency_id", "dam_contingency_id")):
        for k, v in side(frame, crr, dam).items():
            out[f"{name}_{k}"] = v
    return out
