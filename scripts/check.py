"""Run a check from ``ercot_mis.checks`` and write its report under ``data/reports/<check>/``.

    uv run python scripts/check.py prices --day 2026-08-20      # every hour of a day
    uv run python scripts/check.py flows --day 2026-08-20
    uv run python scripts/check.py flows --all                  # every day with awards and a model
    uv run python scripts/check.py injections                   # every day with awards
    uv run python scripts/check.py ldf --day 2026-09-30 --hour 19
    uv run python scripts/check.py network                      # latest monthly CRR model, latest DAM noon
    uv run python scripts/check.py register                     # one measurement per row of docs/assumptions.md

Prints one line per day (counts and quantiles only, never names).
"""

from __future__ import annotations

import argparse
from datetime import date

import polars as pl

import ercot_mis as em
from ercot_mis.checks import flows, injections, ldf, network, prices, register


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("check", choices=("prices", "flows", "injections", "ldf", "network", "register"))
    parser.add_argument("--day", type=date.fromisoformat, help="operating date (default: the newest day the check can run)")
    parser.add_argument("--hour", type=int, default=12, help="ldf: hour ending")
    parser.add_argument("--all", action="store_true", help="prices, flows: every day the check can run")
    parser.add_argument("--crr", help="network: CRR snapshot id")
    parser.add_argument("--dam", help="network: DAM snapshot id")
    args = parser.parse_args()
    with em.open() as session:
        if args.check in ("prices", "flows"):
            module = prices if args.check == "prices" else flows
            available = prices.days_with_prices(session) if args.check == "prices" else flows.days_with_flows(session)
            for day in ([args.day] if args.day else available if args.all else available[-1:]):
                print(module.summary_line(day, module.run(session, day)), flush=True)
        elif args.check == "injections":
            for day in [args.day] if args.day else injections.days(session):
                result = injections.run(session, day)
                print(f"injections {day}: {result.get('hours', 0)} hours, net MW p50 {result.get('net_mw', {}).get('p50_abs')}", flush=True)
        elif args.check == "ldf":
            result = ldf.run(session, args.day, args.hour)
            print(f"ldf {args.day} he{args.hour:02d}: {result.get('matched_substations')} substations, share diff {result.get('share_abs_diff')}")
        elif args.check == "network":
            snaps = session.core("snapshot").collect()
            crr = args.crr or snaps.filter(pl.col("model_kind") == "crr_monthly").sort("month", "revision")["snapshot_id"][-1]
            dam = args.dam or snaps.filter((pl.col("model_kind") == "dam") & (pl.col("hour") == 12)).sort("operating_date", "revision")["snapshot_id"][-1]
            for label, values in network.run(session, crr, dam)["sections"].items():
                print(f"{label}: " + ", ".join(f"{k}={v}" for k, v in values.items()))
        else:
            for row_id, result in register.run(session).items():
                print(f"{row_id}: {result}", flush=True)


if __name__ == "__main__":
    main()
