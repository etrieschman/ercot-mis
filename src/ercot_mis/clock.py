"""ERCOT's hourly facts as instants in UTC.

ERCOT writes an hour three ways. The DAM model numbers a day's hours from local
midnight, 1 to 24 (23 on the short day, 25 on the long one, where the repeated hour is
the third). The price files give a delivery date, a clock ``hour_ending`` (``HH:00``)
and a ``dst_flag`` that marks the repeated hour. The GTL workbook gives naive local
timestamps, one per hour, so the repeated hour appears twice. Each becomes
``interval_start_utc`` here, and hourly tables join on that instant, so neither
transition day needs a special case anywhere else (NAM-08).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl

ZONE = "America/Chicago"
TZ = ZoneInfo(ZONE)


def study_hour_start(day: date, hour: int) -> datetime:
    """The DAM's hour ``hour`` (1-based, counted from local midnight) as a UTC instant."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=TZ)  # midnight is never ambiguous in Texas
    return midnight.astimezone(timezone.utc) + timedelta(hours=hour - 1)


def study_hour_start_expr(day: pl.Expr, hour: pl.Expr) -> pl.Expr:
    """Column form of :func:`study_hour_start` (``day`` a Date, ``hour`` an integer)."""
    midnight = day.cast(pl.Datetime("us")).dt.replace_time_zone(ZONE).dt.convert_time_zone("UTC")
    return midnight + pl.duration(hours=hour - 1)


def hour_ending_start_expr(delivery_date: pl.Expr, hour_ending: pl.Expr, dst_flag: pl.Expr) -> pl.Expr:
    """ERCOT's (date, ``HH:00`` hour ending, DST flag) as the interval's UTC start.

    The flag ``Y`` marks the second of the two 1 a.m. hours in the fall; in spring the
    missing clock hour never appears. ``delivery_date`` is a Date, ``hour_ending`` text.
    """
    hour = hour_ending.str.strip_chars().str.split(":").list.first().cast(pl.Int64, strict=False)
    local = delivery_date.cast(pl.Datetime("us")) + pl.duration(hours=hour - 1)
    ambiguous = pl.when(dst_flag.cast(pl.String).str.to_uppercase().str.starts_with("Y")).then(pl.lit("latest")).otherwise(pl.lit("earliest"))
    return local.dt.replace_time_zone(ZONE, ambiguous=ambiguous).dt.convert_time_zone("UTC")


def local_timestamps_expr(naive: pl.Expr) -> pl.Expr:
    """Naive local timestamps as UTC instants; the second of two identical timestamps is the repeated hour."""
    second = pl.int_range(pl.len()).over(naive) > 0
    ambiguous = pl.when(second).then(pl.lit("latest")).otherwise(pl.lit("earliest"))
    return naive.dt.replace_time_zone(ZONE, ambiguous=ambiguous).dt.convert_time_zone("UTC")
