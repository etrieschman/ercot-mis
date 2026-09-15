"""Daily pull: archive every EWS document before it rolls off.

EWS keeps nothing older than each product's display window (31 days for DAM network
models, 365 for CRR models), so run this every day. It fetches pulled products, lists
tracked ones so their availability is recorded, and exits non-zero if anything failed;
failed documents are retried on the next run.

To capture only some DAM days, copy this file to pulls/ (gitignored) and set
DAM_OPERATING_DATES there. Schedule it with launchd: see examples/launchd/.

    uv run python examples/daily_pull.py
"""

import sys
from datetime import date, datetime, timezone

import polars as pl

import ercot_mis as em

# DAM network models are ~29 MB a day (~10 GB a year). None captures every day;
# a set of dates captures only those.
DAM_OPERATING_DATES: set[date] | None = None

# Per-product ceiling for one run; a first run over a full window stays well under it.
MAX_GB = 5


def main() -> int:
    print(f"daily pull {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", flush=True)
    failed = 0
    with em.open() as mis:
        for spec in em.PRODUCTS.values():
            if spec.source != "ews":
                continue
            try:
                if spec.take == "track":
                    listed = mis.list(spec.emil_id)
                    print(f"  {spec.emil_id}: tracked, {listed.height} listed")
                    continue
                dates = DAM_OPERATING_DATES if spec.emil_id == "NP4-500-SG" else None
                result = mis.fetch(spec.emil_id, operating_dates=dates, max_gb=MAX_GB)
            except Exception as error:  # one product's failure must not stop the others
                failed += 1
                print(f"  {spec.emil_id}: FAILED {type(error).__name__}: {error}")
                continue
            done = result.filter(pl.col("status") == "fetched")
            errors = result.filter(pl.col("status") == "failed")
            failed += errors.height
            print(f"  {spec.emil_id}: fetched {done.height} ({done['size_bytes'].sum() / 1e6:.1f} MB), failed {errors.height}")
            for doc_id, error in errors.select("doc_id", "error").iter_rows():
                print(f"    doc {doc_id}: {error}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
