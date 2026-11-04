"""The daily pull's bookkeeping: what to fetch, what to build, and what a missed pull would lose.

EWS keeps nothing older than each product's display window (31 days for DAM network models,
365 for CRR models), so a pull runs every day (``scripts/daily_pull.py``) and an urgent one
when data is at risk (``scripts/urgent_pull.py``). Both are written from these functions.
``at_risk`` reads the catalog and ``ews_captured.json`` (the last clean fetch per windowed
product) and says how many EWS documents are unarchived and how soon the first could be lost.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from .products import PRODUCTS, Product
from .raw.build import PARSERS
from .session import Session

# DAM network models are ~29 MB a day (~10 GB a year). None captures every day;
# a set of dates captures only those.
DAM_OPERATING_DATES: set[date] | None = None

# Every pulled Public API product, over this many days back; older days are backfilled
# by hand with ``fetch(product, since=...)``. A month covers any gap short of losing EWS data.
PUBLIC_API_LOOKBACK_DAYS = 31

# Per-product ceiling for one run; a first run over a full window stays well under it.
MAX_GB = 5

# EWS data this close to rolling off makes the flag urgent.
URGENT_DAYS = 7

# What a step may raise and the run reports: a polars panic (a Rust panic) is not an Exception.
FAILURES = (Exception, pl.exceptions.PanicException)

# The last clean fetch per windowed EWS product, under the data folder.
CAPTURED_FILE = Path("logs") / "ews_captured.json"


def windowed_products() -> list[Product]:
    """Pulled EWS products with a display window: the only data a missed pull can lose."""
    return [spec for spec in PRODUCTS.values() if spec.source == "ews" and spec.is_fetched and spec.display_days]


def load_captured(mis: Session) -> dict[str, datetime]:
    """When each windowed EWS product was last fetched without a failure.

    A product missing from the file (the first run with it) starts from its newest
    listing in the catalog, which is as good: a listing shows everything then offered.
    """
    path = mis.data_dir / CAPTURED_FILE
    saved = json.loads(path.read_text()) if path.is_file() else {}
    captured = {}
    for spec in windowed_products():
        if spec.emil_id in saved:
            captured[spec.emil_id] = datetime.fromisoformat(saved[spec.emil_id])
        else:
            listed = mis.catalog.documents(spec.emil_id)["last_listed_at"].max()
            if listed is not None:
                captured[spec.emil_id] = listed
    return captured


def save_captured(mis: Session, captured: dict[str, datetime]) -> None:
    path = mis.data_dir / CAPTURED_FILE
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({emil_id: t.isoformat() for emil_id, t in sorted(captured.items())}))


def fetch_products(mis: Session, specs, captured: dict[str, datetime]) -> int:
    """Fetch pulled products and list tracked ones, one log line each; returns the failure count.

    A windowed EWS product fetched without a failure gets its start time in ``captured``.
    """
    failed = 0
    for spec in specs:
        if spec.source == "public_api" and not spec.is_fetched:
            continue  # tracked Public API products are not listed daily; the archive keeps them for years
        # The Public API keeps years of history; a daily run looks back a month.
        since = None if spec.source == "ews" else date.today() - timedelta(days=PUBLIC_API_LOOKBACK_DAYS)
        began, began_at = time.monotonic(), datetime.now(timezone.utc)
        try:
            if not spec.is_fetched:
                listed = mis.list(spec.emil_id)
                print(f"  {spec.emil_id}: tracked, {listed.height} listed, {time.monotonic() - began:.0f}s", flush=True)
                continue
            dates = DAM_OPERATING_DATES if spec.emil_id == "NP4-500-SG" else None
            result = mis.fetch(spec.emil_id, since, operating_dates=dates, max_gb=MAX_GB)
        except FAILURES as error:  # one product's failure must not stop the others
            failed += 1
            print(f"  {spec.emil_id}: FAILED {type(error).__name__}: {error}", flush=True)
            continue
        done = result.filter(pl.col("status") == "fetched")
        errors = result.filter(pl.col("status") == "failed")
        failed += errors.height
        if errors.is_empty() and spec.source == "ews" and spec.display_days:
            captured[spec.emil_id] = began_at
        print(f"  {spec.emil_id}: fetched {done.height} ({done['size_bytes'].sum() / 1e6:.1f} MB), failed {errors.height}, {time.monotonic() - began:.0f}s", flush=True)
        for doc_id, error in errors.select("doc_id", "error").iter_rows():
            print(f"    doc {doc_id}: {error}")
    return failed


def build_layers(mis: Session) -> int:
    """Raw tables for every archived package with a parser, then core; returns the failure count."""
    failed = 0
    for emil_id in PARSERS:
        try:
            result = mis.build_raw(emil_id)
        except FAILURES as error:
            failed += 1
            print(f"  build_raw {emil_id}: FAILED {type(error).__name__}: {error}")
            continue
        failed += report(f"build_raw {emil_id}", result)
    try:
        result = mis.build_core()
    except FAILURES as error:
        failed += 1
        print(f"  build_core: FAILED {type(error).__name__}: {error}")
        return failed
    return failed + report("build_core", result)


def report(label: str, result: pl.DataFrame) -> int:
    """One line per build: packages built, skipped and failed, then each failure's message."""
    counts = {status: n for status, n in result.group_by("status").len().rows()}
    built = result.filter(pl.col("status") == "built")
    print(f"  {label}: built {counts.get('built', 0)} ({int(built['rows'].sum() or 0):,} rows, "
          f"{built['seconds'].sum():.0f}s), skipped {counts.get('skipped', 0)}, failed {counts.get('failed', 0)}")
    for doc_id, error in result.filter(pl.col("status") == "failed").select("doc_id", "error").iter_rows():
        print(f"    doc {doc_id}: {error}")
    return counts.get("failed", 0)


def at_risk(mis: Session, captured: dict[str, datetime], now: datetime | None = None) -> dict:
    """What a missed pull could lose, from the catalog and ``captured`` alone (no EWS calls).

    Two clocks per windowed product: an unarchived document still inside its window rolls
    off a window after its posting; anything posted since the last clean fetch is unseen,
    and the oldest of it rolls off a window after that fetch. ``days_until_loss`` is the
    nearest of all of them. A document skipped by ``DAM_OPERATING_DATES`` counts.
    """
    now = now or datetime.now(timezone.utc)
    unarchived, nearest, product = 0, None, None
    for spec in windowed_products():
        window = timedelta(days=spec.display_days)
        docs = mis.catalog.documents(spec.emil_id)
        missing = docs.filter(~pl.col("is_archived") & pl.col("posted_at").is_not_null() & (pl.col("posted_at") > now - window))
        unarchived += missing.height
        clocks = [missing["posted_at"].min()] if not missing.is_empty() else []
        if spec.emil_id in captured:
            clocks.append(captured[spec.emil_id])
        for start in clocks:
            days_left = (start + window - now).total_seconds() / 86400
            if nearest is None or days_left < nearest:
                nearest, product = days_left, spec.emil_id
    return {"unarchived_ews_documents": unarchived,
            "days_until_loss": None if nearest is None else round(nearest, 1), "nearest_product": product}


def risk_line(risk: dict) -> str:
    line = f"{risk['unarchived_ews_documents']} EWS documents not archived"
    if risk["days_until_loss"] is not None:
        line += f", first possible loss in {risk['days_until_loss']} days ({risk['nearest_product']})"
    return line


def is_urgent(risk: dict) -> bool:
    return risk.get("days_until_loss") is not None and risk["days_until_loss"] <= URGENT_DAYS
