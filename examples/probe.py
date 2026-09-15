"""Probe how far back ERCOT's EWS archive goes for every EWS product.

Lists documents only; downloads nothing. Writes each product's full listing and a
summary to ``data/probes/<date>/``, and prints the summary. File names are not
printed because ERCOT embeds the participant DUNS in them.

    uv run python examples/probe.py
"""

from datetime import datetime, timezone

import polars as pl

import ercot_mis as em

mis = em.open()
out = mis.data_dir / "probes" / datetime.now(timezone.utc).strftime("%Y-%m-%d")
out.mkdir(parents=True, exist_ok=True)

summaries = []
for spec in em.PRODUCTS.values():
    if spec.source != "ews":
        continue
    print(f"probing {spec.emil_id} (report type {spec.report_type_id}) ...", flush=True)
    try:
        probe = mis.probe(spec.emil_id)
    except em.EwsError as error:
        print(f"  failed: {error}")
        continue
    probe.documents.write_parquet(out / f"{spec.emil_id}.parquet")
    summaries.append(probe.summary)
    for group, count in sorted(probe.summary["report_groups"].items()):
        print(f"  {count:>5}  {group}")

summary = pl.DataFrame(
    [{k: v for k, v in s.items() if k != "report_groups"} for s in summaries],
    infer_schema_length=None,
)
summary.write_parquet(out / "summary.parquet")

with pl.Config(tbl_cols=-1, tbl_rows=-1, tbl_width_chars=200, fmt_str_lengths=40):
    print(
        summary.select(
            "emil_id",
            "display_days",
            "n_docs",
            "n_docs_unbounded_listing",
            "n_docs_before_display_window",
            "earliest_posted",
            "latest_posted",
            (pl.col("total_bytes") / 1e6).round(1).alias("total_mb"),
            "older_window_error",
        )
    )
print(f"\nListings written to {out}")
