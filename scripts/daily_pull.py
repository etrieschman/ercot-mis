"""Daily pull: archive every EWS document before it rolls off, the public reports, build the layers, check prices.

EWS keeps nothing older than each product's display window (31 days for DAM network
models, 365 for CRR models), so run this every day. It fetches pulled products, lists
tracked EWS ones so their availability is recorded, then parses every new package into
the raw layer and rebuilds core for it (packages already built are skipped by their
cache key), then runs the price identity check on the newest days. It exits non-zero
if anything failed; failed documents and packages are retried on the next run.

Each run logs what is still at risk: EWS documents ERCOT currently offers that are not
archived, and how many days before the oldest rolls off its window. The same facts
go to ``data/logs/last_run.json`` with the previous run's time, so a gap between runs
(a sleeping machine) is visible. The Public API keeps years, so a gap there heals
itself: every run looks back a month.

To capture only some DAM days, copy this file to pulls/ (gitignored) and set
DAM_OPERATING_DATES there. Schedule it with launchd: see scripts/launchd/.

    uv run python scripts/daily_pull.py
"""

import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

import ercot_mis as em
from ercot_mis.raw.build import parsed_products

# DAM network models are ~29 MB a day (~10 GB a year). None captures every day;
# a set of dates captures only those.
DAM_OPERATING_DATES: set[date] | None = None

# Every pulled Public API product, over this many days back; older days are backfilled
# by hand with ``fetch(product, since=...)``. A month covers any gap short of losing EWS data.
PUBLIC_API_LOOKBACK_DAYS = 31

# The price identity check runs for this many of the newest days with prices and a model.
PRICE_CHECK_DAYS = 2

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


def at_risk(mis: em.Session, listed_since: datetime) -> dict:
    """EWS documents ERCOT offers right now that are not archived, and days before the oldest rolls off.

    Reads the catalog's listing from this run (no new EWS calls). A document that failed
    to download, or was skipped by ``DAM_OPERATING_DATES``, counts; one that already rolled
    off is gone and is not counted.
    """
    now = datetime.now(timezone.utc)
    unarchived, nearest = 0, None
    for spec in em.PRODUCTS.values():
        if spec.source != "ews" or spec.take != "pull" or not spec.display_days:
            continue
        docs = mis.catalog.documents(spec.emil_id)
        missing = docs.filter(~pl.col("is_archived") & (pl.col("last_listed_at") >= listed_since) & pl.col("posted_at").is_not_null())
        if missing.is_empty():
            continue
        unarchived += missing.height
        oldest = missing["posted_at"].min()
        days_left = spec.display_days - (now - oldest).total_seconds() / 86400
        nearest = days_left if nearest is None else min(nearest, days_left)
    return {"unarchived_ews_documents": unarchived, "days_until_oldest_rolls_off": None if nearest is None else round(nearest, 1)}


def check_prices_recent(mis: em.Session, days: int = PRICE_CHECK_DAYS) -> int:
    """The price identity on the newest days with prices and a model; one log line per day. Returns the failure count."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_prices

    failed = 0
    for day in check_prices.days_with_prices(mis)[-days:]:
        began = time.monotonic()
        try:
            results = check_prices.run(mis, check_prices.hour_ids(mis, day), quiet=True)
        except Exception as error:
            failed += 1
            print(f"  price check {day}: FAILED {type(error).__name__}: {error}", flush=True)
            continue
        print(f"  {check_prices.summary_line(day, results)}, {time.monotonic() - began:.0f}s", flush=True)
    return failed


def main() -> int:
    started = datetime.now(timezone.utc)
    print(f"daily pull {started:%Y-%m-%d %H:%M} UTC", flush=True)
    failed, risk, previous = 0, {}, {}
    with em.open() as mis:
        status_path = mis.data_dir / "logs" / "last_run.json"
        previous = json.loads(status_path.read_text()) if status_path.is_file() else {}
        if previous.get("finished_utc"):
            gap = (started - datetime.fromisoformat(previous["finished_utc"])).total_seconds() / 86400
            print(f"  previous run finished {previous['finished_utc'][:16]} UTC, {gap:.1f} days ago", flush=True)
        for spec in em.PRODUCTS.values():
            if spec.source == "public_api" and spec.take == "track":
                continue  # tracked Public API products are not listed daily; the archive keeps them for years
            # The Public API keeps years of history; a daily run looks back a month.
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
        try:
            try:
                risk = at_risk(mis, started)
                print(f"  at risk: {risk['unarchived_ews_documents']} EWS documents not archived"
                      + (f", oldest rolls off in {risk['days_until_oldest_rolls_off']} days" if risk["days_until_oldest_rolls_off"] is not None else ""), flush=True)
            except Exception as error:
                failed += 1
                print(f"  at risk: FAILED {type(error).__name__}: {error}", flush=True)
            failed += build_layers(mis)
            failed += check_prices_recent(mis)
        finally:  # the status file and the notification happen whatever broke above
            status_path.parent.mkdir(mode=0o700, exist_ok=True)
            status_path.write_text(json.dumps({"started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "failed": failed,
                                               "previous_finished_utc": previous.get("finished_utc"), **risk}))
            if failed:
                notify("ercot-mis daily pull", f"{failed} failure(s); see data/logs/daily_pull.log")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
