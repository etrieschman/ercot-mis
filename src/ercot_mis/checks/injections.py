"""Do the DAM's awards balance? Per hour, the signed awards by kind and their net (INJ-02).

The day-ahead market clears supply against demand with no loss term, so generation and
storage awards plus energy-only offer awards should equal the energy bid awards every hour,
and every point-to-point obligation nets to zero between its source and its sink.
"""

from __future__ import annotations

from datetime import date, datetime

from ..core import award
from ..out.injection import awards
from . import write_report

TOTALS = ("interval_start_utc", "delivery_date", "hour_ending", "net_mw", "supply_mw", "net_share_of_supply")


def check_day(session, day: date) -> dict:
    """The hourly balance of one day's awards."""
    rows = awards(session, day)
    if rows.is_empty():
        return {"day": str(day), "hours": 0}
    balance = award.hourly_balance(rows)
    kinds = [c for c in balance.columns if c not in TOTALS]
    share = balance["net_share_of_supply"].abs()
    ptp = balance["ptp_obligation_source"] + balance["ptp_obligation_sink"] if {"ptp_obligation_source", "ptp_obligation_sink"} <= set(kinds) else None
    return {
        "day": str(day), "hours": balance.height, "awards": rows.height,
        "settlement_points_with_injection": award.net_injections(rows)["settlement_point"].n_unique(),
        "mw_by_kind_daily_total": {k: round(float(balance[k].sum()), 1) for k in kinds},
        "net_share_of_supply": {"p50_abs": round(float(share.median()), 5), "max_abs": round(float(share.max()), 5)},
        "net_mw": {"p50_abs": round(float(balance["net_mw"].abs().median()), 1), "max_abs": round(float(balance["net_mw"].abs().max()), 1)},
        "ptp_source_plus_sink_max_abs": round(float(ptp.abs().max()), 3) if ptp is not None else None,
    }


def days(session) -> list[date]:
    """Every operating day with awards built, oldest first."""
    return sorted(datetime.strptime(d, "%m/%d/%Y").date() for d in session.core("hourly_award").select("delivery_date").unique().collect()["delivery_date"])


def run(session, day: date) -> dict:
    result = check_day(session, day)
    write_report(session, "injections", str(day), result)
    return result
