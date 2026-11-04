"""The daily pull: fetch every product, build the raw and core layers, run the price and flow checks.

EWS keeps a document only for its product's display window, so this runs every day
(launchd, ``scripts/launchd/``). A failed document or package is retried by the next run.
The run does not keep the Mac awake; it writes its level to ``data/logs/last_run.json`` and
sends a notification when the level is not ``ok``:

- ``missed``: no run finished for ``MISSED_AFTER_HOURS``, the Mac slept during the run, or a step failed;
- ``urgent``: an EWS product is within ``URGENT_DAYS`` of losing a document (``ercot_mis.pull.at_risk``);
  the notification's text is the command to run, ``scripts/urgent_pull.py``.

To capture only some DAM days, set ``DAM_OPERATING_DATES`` in ``ercot_mis/pull.py``.

    uv run python scripts/daily_pull.py
"""

import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ercot_mis as em
from ercot_mis import pull
from ercot_mis.checks import flows, prices

# The price check runs for this many of the newest days with prices and a model.
PRICE_CHECK_DAYS = 2

# The flow check runs for this many of the newest days with awards and a model.
FLOW_CHECK_DAYS = 1

# No finished run for this long is a missed pull.
MISSED_AFTER_HOURS = 36

# Sleeping longer than this during a run is flagged.
SLEPT_FLAG_MINUTES = 10

# The fix an urgent notification names, runnable from any folder.
URGENT_COMMAND = f"cd {shlex.quote(str(Path(__file__).resolve().parents[1]))} && uv run python scripts/urgent_pull.py"


def notify(title: str, text: str, subtitle: str | None = None) -> None:
    """Post a macOS notification; it stays in Notification Center (Script Editor's notifications set to Alerts keep it on screen)."""
    if sys.platform != "darwin":
        return
    script = f'display notification "{text}" with title "{title}"' + (f' subtitle "{subtitle}"' if subtitle else "")
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def alert_urgent(risk: dict) -> None:
    notify("ercot-mis: EWS data at risk", URGENT_COMMAND, subtitle=pull.risk_line(risk))


def run_checks(mis: em.Session) -> int:
    """Run the price check on the newest days with prices and the flow check on the newest day with awards, one log line each; the number of failures."""
    failed = 0
    for module, available, days in ((prices, prices.days_with_prices(mis), PRICE_CHECK_DAYS), (flows, flows.days_with_flows(mis), FLOW_CHECK_DAYS)):
        for day in available[-days:]:
            began = time.monotonic()
            try:
                print(f"  {module.summary_line(day, module.run(mis, day))}, {time.monotonic() - began:.0f}s", flush=True)
            except pull.FAILURES as error:
                failed += 1
                print(f"  {module.__name__.rsplit('.', 1)[-1]} check {day}: FAILED {type(error).__name__}: {error}", flush=True)
    return failed


def main() -> int:
    started = datetime.now(timezone.utc)
    wall, awake = time.time(), time.monotonic()  # the monotonic clock stops while the Mac sleeps
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
        captured = pull.load_captured(mis)
        risk = pull.at_risk(mis, captured)
        urgent_at_start = pull.is_urgent(risk)
        print(f"  before the fetch: {pull.risk_line(risk)}", flush=True)
        if urgent_at_start:
            alert_urgent(risk)  # before the fetch, in case this run stalls
        try:
            failed += pull.fetch_products(mis, em.PRODUCTS.values(), captured)
            pull.save_captured(mis, captured)
            try:
                risk = pull.at_risk(mis, captured)
                print(f"  at risk: {pull.risk_line(risk)}", flush=True)
            except pull.FAILURES as error:
                failed += 1
                print(f"  at risk: FAILED {type(error).__name__}: {error}", flush=True)
            failed += pull.build_layers(mis)
            failed += run_checks(mis)
        finally:  # the status file and the notification are written whatever failed above
            slept = ((time.time() - wall) - (time.monotonic() - awake)) / 60
            if slept > SLEPT_FLAG_MINUTES:
                flags.append(f"asleep {slept:.0f} min during the run")
            if failed:
                flags.append(f"{failed} failure(s)")
            level = "urgent" if pull.is_urgent(risk) else "missed" if flags else "ok"
            print(f"  level {level}" + (f": {'; '.join(flags)}" if flags else ""), flush=True)
            status_path.parent.mkdir(mode=0o700, exist_ok=True)
            status_path.write_text(json.dumps({"started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "failed": failed,
                                               "previous_finished_utc": previous.get("finished_utc"), "slept_minutes": round(slept),
                                               "level": level, "flags": flags, **risk}))
            if level == "urgent":
                alert_urgent(risk)
            elif level == "missed":
                notify("ercot-mis: pull incomplete", f"{'; '.join(flags)}. Nothing urgent: {pull.risk_line(risk)}.")
            elif urgent_at_start:
                notify("ercot-mis: caught up", pull.risk_line(risk))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
