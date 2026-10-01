"""Daily pull: archive every EWS document before it rolls off, the day's public prices, then build the layers.

EWS keeps nothing older than each product's display window (31 days for DAM network
models, 365 for CRR models), so run this every day. It fetches pulled products, lists
tracked ones so their availability is recorded, then parses every new package into the
raw layer and rebuilds core for it (packages already built are skipped by their cache
key). It exits non-zero if anything failed; failed documents and packages are retried
on the next run.

To capture only some DAM days, copy this file to pulls/ (gitignored) and set
DAM_OPERATING_DATES there. Schedule it with launchd: see scripts/launchd/.

    uv run python scripts/daily_pull.py
"""

import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

import polars as pl

import ercot_mis as em
from ercot_mis.raw.build import parsed_products

# DAM network models are ~29 MB a day (~10 GB a year). None captures every day;
# a set of dates captures only those.
DAM_OPERATING_DATES: set[date] | None = None

# Public API products pulled every day (DAM shadow prices, bus LMPs, settlement point
# prices); older days are backfilled by hand with ``fetch(product, since=...)``.
PUBLIC_API_DAILY = ("NP4-191-CD", "NP4-183-CD", "NP4-190-CD")
PUBLIC_API_LOOKBACK_DAYS = 7

# Per-product ceiling for one run; a first run over a full window stays well under it.
MAX_GB = 5


def notify(title: str, text: str) -> None:
    """A macOS notification, so a failed run does not hide in a log file."""
    if sys.platform != "darwin":
        return
    script = f'display notification "{text}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def build_layers(mis: em.Session) -> int:
    """Raw tables for every archived package with a parser, then core; returns the failure count."""
    failed = 0
    for emil_id in parsed_products():
        try:
            result = mis.build_raw(emil_id)
        except Exception as error:
            failed += 1
            print(f"  build_raw {emil_id}: FAILED {type(error).__name__}: {error}")
            continue
        failed += _report(f"build_raw {emil_id}", result)
    try:
        result = mis.build_core()
    except Exception as error:
        failed += 1
        print(f"  build_core: FAILED {type(error).__name__}: {error}")
        return failed
    return failed + _report("build_core", result)


def _report(label: str, result: pl.DataFrame) -> int:
    """One line per build: packages built, skipped and failed, then each failure's message."""
    counts = {status: n for status, n in result.group_by("status").len().rows()}
    built = result.filter(pl.col("status") == "built")
    print(f"  {label}: built {counts.get('built', 0)} ({int(built['rows'].sum() or 0):,} rows, "
          f"{built['seconds'].sum():.0f}s), skipped {counts.get('skipped', 0)}, failed {counts.get('failed', 0)}")
    for doc_id, error in result.filter(pl.col("status") == "failed").select("doc_id", "error").iter_rows():
        print(f"    doc {doc_id}: {error}")
    return counts.get("failed", 0)


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"daily pull {started:%Y-%m-%d %H:%M} UTC", flush=True)
    failed = 0
    with em.open() as mis:
        status_path = mis.data_dir / "logs" / "last_run.json"
        for spec in em.PRODUCTS.values():
            if spec.source == "public_api" and spec.emil_id not in PUBLIC_API_DAILY:
                continue
            # The Public API keeps years of history; a daily run only looks at the last few days.
            since = None if spec.source == "ews" else date.today() - timedelta(days=PUBLIC_API_LOOKBACK_DAYS)
            began = time.monotonic()
            try:
                if spec.take == "track":
                    listed = mis.list(spec.emil_id)
                    print(f"  {spec.emil_id}: tracked, {listed.height} listed, {time.monotonic() - began:.0f}s", flush=True)
                    continue
                dates = DAM_OPERATING_DATES if spec.emil_id == "NP4-500-SG" else None
                result = mis.fetch(spec.emil_id, since, operating_dates=dates, max_gb=MAX_GB)
            except Exception as error:  # one product's failure must not stop the others
                failed += 1
                print(f"  {spec.emil_id}: FAILED {type(error).__name__}: {error}")
                continue
            done = result.filter(pl.col("status") == "fetched")
            errors = result.filter(pl.col("status") == "failed")
            failed += errors.height
            print(f"  {spec.emil_id}: fetched {done.height} ({done['size_bytes'].sum() / 1e6:.1f} MB), failed {errors.height}, {time.monotonic() - began:.0f}s", flush=True)
            for doc_id, error in errors.select("doc_id", "error").iter_rows():
                print(f"    doc {doc_id}: {error}")
        failed += build_layers(mis)
    status_path.parent.mkdir(mode=0o700, exist_ok=True)
    status_path.write_text(json.dumps({"started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "failed": failed}))
    if failed:
        notify("ercot-mis daily pull", f"{failed} failure(s); see data/logs/daily_pull.log")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
