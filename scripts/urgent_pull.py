"""Urgent pull: archive the EWS documents a missed pull could lose, and nothing else.

Run it when the daily pull's notification says EWS data is at risk:

    uv run python scripts/urgent_pull.py

It keeps the Mac awake while it runs (``caffeinate``), prints what is at risk, fetches
every EWS product with a display window (the one closest to losing data first), records
each clean fetch in ``data/logs/ews_captured.json`` and prints what is still at risk.
No Public API products (their archive keeps years), no builds and no price check: the
next daily pull does those. ``DAM_OPERATING_DATES`` comes from ``scripts/daily_pull.py``,
not from a copy in pulls/. It exits non-zero if anything failed or is still urgent.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_pull  # noqa: E402

import ercot_mis as em  # noqa: E402


def keep_awake() -> None:
    """Hold off idle sleep until this process exits."""
    if sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def main() -> int:
    keep_awake()
    print(f"urgent pull {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", flush=True)
    with em.open() as mis:
        captured = daily_pull.load_captured(mis)
        print(f"  before: {daily_pull.risk_line(daily_pull.at_risk(mis, captured))}", flush=True)
        never = datetime.min.replace(tzinfo=timezone.utc)
        specs = sorted(daily_pull.windowed_products(), key=lambda spec: captured.get(spec.emil_id, never) + timedelta(days=spec.display_days))
        failed = daily_pull.fetch_products(mis, specs, captured)
        daily_pull.save_captured(mis, captured)
        risk = daily_pull.at_risk(mis, captured)
        print(f"  after: {daily_pull.risk_line(risk)}", flush=True)
    urgent = daily_pull.is_urgent(risk)
    if urgent:
        print("  still urgent: look at the failures above and run this again", flush=True)
    return 1 if failed or urgent else 0


if __name__ == "__main__":
    sys.exit(main())
