"""ERCOT's published load distribution factors (NP4-159-CD) against the DAM load file's shares (SP-02).

The DAM load file (``Ld``) gives each load's MW-scale distribution factor and zone; the
public report gives a factor per load and substation for the same hour but under its
own load identifiers, which do not match the model's load names. Substations do, so the
comparison is per substation: each substation's share of its zone's load in the model
against its share in the public report (zones come from the model's loads): a direct
comparison, substation by substation, no fitted summary. Counts and the sizes of the
differences only; writes ``data/reports/ldf/<day>_he<hour>.json``.

    uv run python scripts/check_ldf.py --day 2026-09-30 --hour 19
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import polars as pl

import ercot_mis as em


def check(session, day: date, hour: int) -> dict:
    ld = session.raw("dam_loads").filter((pl.col("operating_date") == day) & (pl.col("hour") == hour)).collect()
    ldf = session.raw("load_distribution_factors").filter((pl.col("ldf_date") == f"{day:%m/%d/%Y}") & (pl.col("ldf_hour") == f"{hour:02d}:00")).collect()
    if ld.is_empty() or ldf.is_empty():
        return {"day": str(day), "hour": hour, "compared": False, "model_rows": ld.height, "published_rows": ldf.height}
    model = (ld.filter(pl.col("load_status").str.to_uppercase().str.starts_with("IN"))
             .group_by(pl.col("station_name_psse_bus_name").str.to_uppercase().alias("substation"), pl.col("load_zone_name").alias("zone"))
             .agg(pl.col("raw_mw_ldf").sum().alias("model_mw")))
    # a substation that hosts loads of two zones is counted in its larger one
    zone_of = model.sort("model_mw", descending=True).unique(subset=["substation"], keep="first").select("substation", "zone")
    model = model.group_by("substation").agg(pl.col("model_mw").sum()).join(zone_of, on="substation")
    published = ldf.group_by(pl.col("substation").str.to_uppercase().alias("substation")).agg(pl.col("distribution_factor").sum().alias("published"))
    both = model.join(published, on="substation", how="inner")
    both = both.with_columns((pl.col("model_mw") / pl.col("model_mw").sum().over("zone")).alias("model_share"),
                             (pl.col("published") / pl.col("published").sum().over("zone")).alias("published_share"))
    diff = (both["model_share"] - both["published_share"]).abs()
    return {
        "day": str(day), "hour": hour, "compared": True,
        "model_substations": model.height, "published_substations": published.height, "matched_substations": both.height,
        "model_mw_total": round(float(model["model_mw"].sum()), 1), "model_mw_matched": round(float(both["model_mw"].sum()), 1),
        "published_total": round(float(published["published"].sum()), 1), "published_matched": round(float(both["published"].sum()), 1),
        "share_abs_diff": {"p50": round(float(diff.median()), 5), "p90": round(float(diff.quantile(0.9)), 5), "max": round(float(diff.max()), 5)},
        "substations_where_shares_differ_over_1pct": int((diff > 0.01).sum()),
        "largest_share_difference_by_zone": {z: round(float((g["model_share"] - g["published_share"]).abs().max()), 5) for (z,), g in both.group_by("zone")},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--hour", type=int, default=12)
    args = parser.parse_args()
    with em.open() as session:
        result = check(session, args.day, args.hour)
        print(json.dumps(result, indent=1))
        out = session.data_dir / "reports" / "ldf"
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        (out / f"{args.day}_he{args.hour:02d}.json").write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1))


if __name__ == "__main__":
    main()
