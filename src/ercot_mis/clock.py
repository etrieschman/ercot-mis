"""ERCOT's three ways of writing an hour, each turned into the hour's UTC start (NAM-08).

The DAM model counts a day's hours from local midnight, 1 to 24 (23 on the short day, 25 on
the long one). The price files give a delivery date, an ``hour_ending`` clock time and a
``dst_flag`` for the repeated hour. The GTL workbook gives naive local timestamps, so the
repeated hour appears twice. Hourly tables join on ``interval_start_utc``, never on position.
"""

from __future__ import annotations

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
