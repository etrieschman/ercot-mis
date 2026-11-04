"""ERCOT's three ways of writing an hour, each turned into the hour's UTC start (NAM-08), and the CRR time-of-use blocks.

The DAM model counts a day's hours from local midnight, 1 to 24 (23 on the short day, 25 on
the long one). The price files give a delivery date, an ``hour_ending`` clock time and a
``dst_flag`` for the repeated hour. The GTL workbook gives naive local timestamps, so the
repeated hour appears twice. Hourly tables join on ``interval_start_utc``, never on position.

The CRR auction sells each month in three time-of-use blocks (Protocols 7.3(5)): hours ending
7 to 22 on weekdays that are not NERC holidays (``PeakWD``), the same hours on Saturdays,
Sundays and NERC holidays (``PeakWE``), and every other hour (``Off-peak``). The block names
are the CRR files' spelling. ``block_hours`` counts a month's hours per block, which turns a
block's shadow price per MW into a price per MWh (CRA-01).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl

ZONE = "America/Chicago"
TZ = ZoneInfo(ZONE)


def study_hour_start(day: date, hour: int) -> datetime:
    """The UTC start of the DAM's study hour ``hour`` (1-based from local midnight) on ``day``."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)  # midnight is never ambiguous in Texas
    return midnight.astimezone(timezone.utc) + timedelta(hours=hour - 1)


def hour_ending_start_expr(delivery_date: pl.Expr, hour_ending: pl.Expr, dst_flag: pl.Expr) -> pl.Expr:
    """The UTC start of the hour a price file gives as (delivery date, ``HH:00`` hour ending, DST flag).

    The flag ``Y`` marks the second of the two repeated hours in the fall; the spring's missing hour never appears.
    """
    hour = hour_ending.str.strip_chars().str.split(":").list.first().cast(pl.Int64, strict=False)
    local = delivery_date.cast(pl.Datetime("us")) + pl.duration(hours=hour - 1)
    ambiguous = pl.when(dst_flag.cast(pl.String).str.to_uppercase().str.starts_with("Y")).then(pl.lit("latest")).otherwise(pl.lit("earliest"))
    return local.dt.replace_time_zone(ZONE, ambiguous=ambiguous).dt.convert_time_zone("UTC")


def local_timestamps_expr(naive: pl.Expr) -> pl.Expr:
    """Naive local timestamps as UTC instants; the second of two equal timestamps is the fall's repeated hour."""
    second = pl.int_range(pl.len()).over(naive) > 0
    ambiguous = pl.when(second).then(pl.lit("latest")).otherwise(pl.lit("earliest"))
    return naive.dt.replace_time_zone(ZONE, ambiguous=ambiguous).dt.convert_time_zone("UTC")


BLOCKS = ("PeakWD", "PeakWE", "Off-peak")
PEAK_HOURS = 16  # hours ending 7 to 22


def nerc_holidays(year: int) -> set[date]:
    """NERC's six holidays in ``year``: New Year's Day, Memorial Day, Independence Day, Labor Day, Thanksgiving and Christmas; one that falls on a Sunday is observed on the Monday after."""
    def on_or_after(day: date, weekday: int) -> date:
        return day + timedelta(days=(weekday - day.weekday()) % 7)

    fixed = (date(year, 1, 1), date(year, 7, 4), date(year, 12, 25))
    observed = {d + timedelta(days=1) if d.weekday() == 6 else d for d in fixed}
    memorial = on_or_after(date(year, 5, 25), 0)  # the last Monday of May
    labor = on_or_after(date(year, 9, 1), 0)  # the first Monday of September
    thanksgiving = on_or_after(date(year, 11, 22), 3)  # the fourth Thursday of November
    return observed | {memorial, labor, thanksgiving}


def block_hours(month: date) -> dict[str, int]:
    """Hours of each CRR time-of-use block in the calendar month of ``month``, counted on the clock (Protocols 7.3(5))."""
    n_days = calendar.monthrange(month.year, month.month)[1]
    days = [date(month.year, month.month, d) for d in range(1, n_days + 1)]
    holidays = nerc_holidays(month.year)
    weekdays = sum(1 for d in days if d.weekday() < 5 and d not in holidays)
    return {"PeakWD": weekdays * PEAK_HOURS, "PeakWE": (n_days - weekdays) * PEAK_HOURS, "Off-peak": n_days * (24 - PEAK_HOURS)}
