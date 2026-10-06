"""Do the DAM's awards balance? Per operating day and hour, the signed awards by kind and their net (INJ-02).

The day-ahead market clears supply against demand with no loss term, so over the whole
system the generation and storage awards plus energy-only offer awards should equal the
energy bid awards, and every point-to-point obligation nets to zero (source against
sink). The net per hour, as a share of supply, is the measure; the per-point net is
``q``, the injection vector the flow check will push through the network.

Prints counts and quantiles only; writes ``data/reports/injections/<day>.json``.

    uv run python scripts/check_injections.py                 # every disclosure day built
    uv run python scripts/check_injections.py --day 2026-08-05
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import polars as pl

import ercot_mis as em
from ercot_mis.core import award


def check_day(session, day: date) -> dict:
    rows = session.core("hourly_award").filter(pl.col("delivery_date") == f"{day:%m/%d/%Y}").collect()
    if rows.is_empty():
        return {"day": str(day), "hours": 0}
    balance = award.hourly_balance(rows)
    q = award.net_injections(rows)
    kinds = [c for c in balance.columns if c not in ("interval_start_utc", "delivery_date", "hour_ending", "net_mw", "supply_mw", "net_share_of_supply")]
    share = balance["net_share_of_supply"].abs()
    return {
        "day": str(day), "hours": balance.height, "awards": rows.height, "settlement_points_with_injection": q["settlement_point"].n_unique(),
        "mw_by_kind_daily_total": {k: round(float(balance[k].sum()), 1) for k in kinds},
        "net_share_of_supply": {"p50_abs": round(float(share.median()), 5), "max_abs": round(float(share.max()), 5)},
        "net_mw": {"p50_abs": round(float(balance["net_mw"].abs().median()), 1), "max_abs": round(float(balance["net_mw"].abs().max()), 1)},
        "ptp_source_plus_sink_max_abs": round(float((balance["ptp_obligation_source"] + balance["ptp_obligation_sink"]).abs().max()), 3)
        if "ptp_obligation_source" in kinds and "ptp_obligation_sink" in kinds else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", default=None, help="operating date (default: every day with awards built)")
    args = parser.parse_args()
    with em.open() as session:
        if args.day:
            days = [date.fromisoformat(args.day)]
        else:
            days = sorted(datetime.strptime(d, "%m/%d/%Y").date() for d in session.core("hourly_award").select("delivery_date").unique().collect()["delivery_date"])
        out = session.data_dir / "reports" / "injections"
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        for day in days:
            result = check_day(session, day)
            print(json.dumps(result))
            (out / f"{day}.json").write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1))


if __name__ == "__main__":
    main()
