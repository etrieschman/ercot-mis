"""ERCOT's published load distribution factors (NP4-159-CD) against the DAM load file's shares (SP-02).

The published report names loads by its own identifiers, which do not match the model's load
names, but its substations do. So the comparison is per substation: each substation's share of
its zone's load in the model, against its share in the report. Zones come from the model.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from . import write_report


def check(session, day: date, hour: int) -> dict:
    """Substation shares of zone load, model against ERCOT's report, for one hour."""
    ld = session.raw("dam_loads").filter((pl.col("operating_date") == day) & (pl.col("hour") == hour)).collect()
    ldf = session.raw("load_distribution_factors").filter((pl.col("ldf_date") == f"{day:%m/%d/%Y}") & (pl.col("ldf_hour") == f"{hour:02d}:00")).collect()
    if ld.is_empty() or ldf.is_empty():
        return {"day": str(day), "hour": hour, "compared": False, "model_rows": ld.height, "published_rows": ldf.height}
    model = (ld.filter(pl.col("load_status").str.to_uppercase().str.starts_with("IN"))
             .group_by(pl.col("station_name_psse_bus_name").str.to_uppercase().alias("substation"), pl.col("load_zone_name").alias("zone"))
             .agg(pl.col("raw_mw_ldf").sum().alias("model_mw")))
    zone_of = model.sort("model_mw", descending=True).unique(subset=["substation"], keep="first").select("substation",
                                                                                                         "zone")  # SP-02: a substation in two zones counts in its larger one
    model = model.group_by("substation").agg(pl.col("model_mw").sum()).join(zone_of, on="substation")
    published = ldf.group_by(pl.col("substation").str.to_uppercase().alias("substation")).agg(pl.col("distribution_factor").sum().alias("published"))
    both = (model.join(published, on="substation", how="inner")
            .with_columns((pl.col("model_mw") / pl.col("model_mw").sum().over("zone")).alias("model_share"),
                          (pl.col("published") / pl.col("published").sum().over("zone")).alias("published_share"))
            .with_columns((pl.col("model_share") - pl.col("published_share")).abs().alias("diff")))
    return {
        "day": str(day), "hour": hour, "compared": True,
        "model_substations": model.height, "published_substations": published.height, "matched_substations": both.height,
        "model_mw_total": round(float(model["model_mw"].sum()), 1), "model_mw_matched": round(float(both["model_mw"].sum()), 1),
        "published_total": round(float(published["published"].sum()), 1), "published_matched": round(float(both["published"].sum()), 1),
        "share_abs_diff": {"p50": round(float(both["diff"].median()), 5), "p90": round(float(both["diff"].quantile(0.9)), 5),
                           "max": round(float(both["diff"].max()), 5)},
        "substations_where_shares_differ_over_1pct": int((both["diff"] > 0.01).sum()),
        "largest_share_difference_by_zone": {z: round(float(g["diff"].max()), 5) for (z,), g in both.group_by("zone")},
    }


def run(session, day: date, hour: int = 12) -> dict:
    result = check(session, day, hour)
    write_report(session, "ldf", f"{day}_he{hour:02d}", result)
    return result
