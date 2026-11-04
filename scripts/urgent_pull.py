"""The urgent pull: fetch the EWS documents a missed pull could lose, and nothing else.

Run it when the daily pull's notification says EWS data is at risk. It keeps the Mac awake,
fetches every EWS product with a display window (the one closest to a loss first), records
each clean fetch and prints what is still at risk. The next daily pull does the builds and
the checks. It exits non-zero when a fetch failed or data is still at risk.

    uv run python scripts/urgent_pull.py
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import ercot_mis as em
from ercot_mis import daily


def keep_awake() -> None:
    """Keep the Mac from idle sleep until this process exits."""
    if sys.platform == "darwin":
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])


def main() -> int:
    keep_awake()
    print(f"urgent pull {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", flush=True)
    with em.open() as mis:
        captured = daily.load_captured(mis)
        print(f"  before: {daily.risk_line(daily.at_risk(mis, captured))}", flush=True)
        never = datetime.min.replace(tzinfo=timezone.utc)
        specs = sorted(daily.windowed_products(), key=lambda spec: captured.get(spec.emil_id, never) + timedelta(days=spec.display_days))
        failed = daily.fetch_products(mis, specs, captured)
        daily.save_captured(mis, captured)
        risk = daily.at_risk(mis, captured)
        print(f"  after: {daily.risk_line(risk)}", flush=True)
    urgent = daily.is_urgent(risk)
    if urgent:
        print("  still urgent: look at the failures above and run this again", flush=True)
    return 1 if failed or urgent else 0


if __name__ == "__main__":
    sys.exit(main())
