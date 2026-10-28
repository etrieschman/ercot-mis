"""Daily pull: archive every EWS document before it rolls off, the public reports, build the layers, check prices.

EWS keeps nothing older than each product's display window (31 days for DAM network
models, 365 for CRR models), so run this every day. It fetches pulled products, lists
tracked EWS ones so their availability is recorded, then parses every new package into
the raw layer and rebuilds core for it (packages already built are skipped by their
cache key), then runs the price check on the newest days. It exits non-zero
if anything failed; failed documents and packages are retried on the next run.

The run does not keep the Mac awake. A missed or stalled pull is flagged instead, and
the flag turns urgent only when EWS data could actually be lost:

- missed: no run finished for a day and a half, the machine slept during the run
  (wall time minus awake time), or something failed. A quiet notification.
- urgent: some EWS product is within ``URGENT_DAYS`` of losing data. Either a document
  ERCOT offers is not archived and its window is closing, or the product has not been
  fetched cleanly for so long that documents posted since then are about to roll off.
  The last clean fetch per product is kept in ``data/logs/ews_captured.json``. The
  notification has a sound and names the fix: ``uv run python scripts/urgent_pull.py``.
  The check runs before the fetch too, so a run that then stalls still warns.

The flags, the level and the days until the first possible loss go to
``data/logs/last_run.json``. The Public API keeps years, so a gap there heals itself:
every run looks back a month.

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
from ercot_mis.products import Product
from ercot_mis.raw.build import parsed_products

# DAM network models are ~29 MB a day (~10 GB a year). None captures every day;
# a set of dates captures only those.
DAM_OPERATING_DATES: set[date] | None = None

# Every pulled Public API product, over this many days back; older days are backfilled
# by hand with ``fetch(product, since=...)``. A month covers any gap short of losing EWS data.
PUBLIC_API_LOOKBACK_DAYS = 31

# The price check runs for this many of the newest days with prices and a model.
PRICE_CHECK_DAYS = 2

# Per-product ceiling for one run; a first run over a full window stays well under it.
MAX_GB = 5

# No finished run for this long is a missed pull.
MISSED_AFTER_HOURS = 36

# Sleeping longer than this during a run is flagged: the run stalled with the network down.
SLEPT_FLAG_MINUTES = 10

# EWS data this close to rolling off makes the flag urgent.
URGENT_DAYS = 7

URGENT_COMMAND = "uv run python scripts/urgent_pull.py"


def notify(title: str, text: str, sound: str | None = None) -> None:
    """A macOS notification, so a failed run does not hide in a log file."""
    if sys.platform != "darwin":
        return
    script = f'display notification "{text}" with title "{title}"' + (f' sound name "{sound}"' if sound else "")
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def windowed_products() -> list[Product]:
    """Pulled EWS products with a display window: the only data a missed pull can lose."""
    return [spec for spec in em.PRODUCTS.values() if spec.source == "ews" and spec.take == "pull" and spec.display_days]


def _captured_path(mis: em.Session) -> Path:
    return mis.data_dir / "logs" / "ews_captured.json"


def load_captured(mis: em.Session) -> dict[str, datetime]:
    """When each windowed EWS product was last fetched without a failure.

    A product missing from the file (the first run with it) starts from its newest
    listing in the catalog, which is as good: a listing shows everything then offered.
    """
    path = _captured_path(mis)
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


def save_captured(mis: em.Session, captured: dict[str, datetime]) -> None:
    path = _captured_path(mis)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({emil_id: t.isoformat() for emil_id, t in sorted(captured.items())}))


def fetch_products(mis: em.Session, specs, captured: dict[str, datetime]) -> int:
    """Fetch pulled products and list tracked ones, one log line each; returns the failure count.

    A windowed EWS product fetched without a failure gets its start time in ``captured``.
    """
    failed = 0
    for spec in specs:
        if spec.source == "public_api" and spec.take == "track":
            continue  # tracked Public API products are not listed daily; the archive keeps them for years
        # The Public API keeps years of history; a daily run looks back a month.
        since = None if spec.source == "ews" else date.today() - timedelta(days=PUBLIC_API_LOOKBACK_DAYS)
        began, began_at = time.monotonic(), datetime.now(timezone.utc)
        try:
            if spec.take == "track":
                listed = mis.list(spec.emil_id)
                print(f"  {spec.emil_id}: tracked, {listed.height} listed, {time.monotonic() - began:.0f}s", flush=True)
                continue
            dates = DAM_OPERATING_DATES if spec.emil_id == "NP4-500-SG" else None
            result = mis.fetch(spec.emil_id, since, operating_dates=dates, max_gb=MAX_GB)
        except Exception as error:  # one product's failure must not stop the others
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


def at_risk(mis: em.Session, captured: dict[str, datetime], now: datetime | None = None) -> dict:
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


def alert_urgent(risk: dict) -> None:
    notify("ercot-mis: EWS data at risk", f"{risk_line(risk)}. Run: {URGENT_COMMAND}", sound="Basso")


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
    wall, awake = time.time(), time.monotonic()  # monotonic stops while the Mac sleeps
    print(f"daily pull {started:%Y-%m-%d %H:%M} UTC", flush=True)
    failed, flags, risk, previous = 0, [], {}, {}
    with em.open() as mis:
        status_path = mis.data_dir / "logs" / "last_run.json"
        previous = json.loads(status_path.read_text()) if status_path.is_file() else {}
        if previous.get("finished_utc"):
            gap = (started - datetime.fromisoformat(previous["finished_utc"])).total_seconds() / 86400
            print(f"  previous run finished {previous['finished_utc'][:16]} UTC, {gap:.1f} days ago", flush=True)
            if gap * 24 > MISSED_AFTER_HOURS:
                flags.append(f"no pull for {gap:.1f} days")
        captured = load_captured(mis)
        risk = at_risk(mis, captured)
        urgent_at_start = is_urgent(risk)
        print(f"  before the fetch: {risk_line(risk)}", flush=True)
        if urgent_at_start:
            alert_urgent(risk)  # now, in case this run stalls too
        try:
            failed += fetch_products(mis, em.PRODUCTS.values(), captured)
            save_captured(mis, captured)
            try:
                risk = at_risk(mis, captured)
                print(f"  at risk: {risk_line(risk)}", flush=True)
            except Exception as error:
                failed += 1
                print(f"  at risk: FAILED {type(error).__name__}: {error}", flush=True)
            failed += build_layers(mis)
            failed += check_prices_recent(mis)
        finally:  # the status file and the notification happen whatever broke above
            slept = ((time.time() - wall) - (time.monotonic() - awake)) / 60
            if slept > SLEPT_FLAG_MINUTES:
                flags.append(f"asleep {slept:.0f} min during the run")
            if failed:
                flags.append(f"{failed} failure(s)")
            level = "urgent" if is_urgent(risk) else "missed" if flags else "ok"
            print(f"  level {level}" + (f": {'; '.join(flags)}" if flags else ""), flush=True)
            status_path.parent.mkdir(mode=0o700, exist_ok=True)
            status_path.write_text(json.dumps({"started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "failed": failed,
                                               "previous_finished_utc": previous.get("finished_utc"), "slept_minutes": round(slept),
                                               "level": level, "flags": flags, **risk}))
            if level == "urgent":
                alert_urgent(risk)
            elif level == "missed":
                notify("ercot-mis: pull incomplete", f"{'; '.join(flags)}. Nothing urgent: {risk_line(risk)}.")
            elif urgent_at_start:
                notify("ercot-mis: caught up", risk_line(risk))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
