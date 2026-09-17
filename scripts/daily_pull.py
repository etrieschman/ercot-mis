"""Daily pull: archive every EWS document before it rolls off.

EWS keeps nothing older than each product's display window (31 days for DAM network
models, 365 for CRR models), so run this every day. It fetches pulled products, lists
tracked ones so their availability is recorded, and exits non-zero if anything failed;
failed documents are retried on the next run.

To capture only some DAM days, copy this file to pulls/ (gitignored) and set
DAM_OPERATING_DATES there. Schedule it with launchd: see scripts/launchd/.

    uv run python scripts/daily_pull.py
"""

import json
import subprocess
import sys
from datetime import date, datetime, timezone

import polars as pl

import ercot_mis as em

# DAM network models are ~29 MB a day (~10 GB a year). None captures every day;
# a set of dates captures only those.
DAM_OPERATING_DATES: set[date] | None = None

# Per-product ceiling for one run; a first run over a full window stays well under it.
MAX_GB = 5


def notify(title: str, text: str) -> None:
    """A macOS notification, so a failed run does not hide in a log file."""
    if sys.platform != "darwin":
        return
    script = f'display notification "{text}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"daily pull {started:%Y-%m-%d %H:%M} UTC", flush=True)
    failed = 0
    with em.open() as mis:
        status_path = mis.data_dir / "logs" / "last_run.json"
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
    status_path.parent.mkdir(mode=0o700, exist_ok=True)
    status_path.write_text(json.dumps({"started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "failed": failed}))
    if failed:
        notify("ercot-mis daily pull", f"{failed} failure(s); see data/logs/daily_pull.log")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
