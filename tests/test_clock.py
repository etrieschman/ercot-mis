"""Hourly facts as UTC instants on ordinary days and on both daylight-saving transition days."""

from datetime import date, datetime, timezone

import polars as pl

from ercot_mis import clock

UTC = timezone.utc


def test_dam_study_hours_count_from_local_midnight():
    # an ordinary summer day: hour 1 starts at 05:00 UTC
    assert clock.study_hour_start(date(2026, 9, 30), 1) == datetime(2026, 9, 30, 5, tzinfo=UTC)
    assert clock.study_hour_start(date(2026, 9, 30), 24) == datetime(2026, 10, 1, 4, tzinfo=UTC)
    # the long day (2026-11-01): hours 2 and 3 are the two 1 a.m. hours, hour 25 ends at the next local midnight
    assert [clock.study_hour_start(date(2026, 11, 1), h) for h in (2, 3, 4)] == [datetime(2026, 11, 1, 6, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 1, 8, tzinfo=UTC)]
    assert clock.study_hour_start(date(2026, 11, 1), 25) == datetime(2026, 11, 2, 5, tzinfo=UTC)
    # the short day (2026-03-08): hour 3 is 3 a.m. daylight time
    assert clock.study_hour_start(date(2026, 3, 8), 3) == datetime(2026, 3, 8, 8, tzinfo=UTC)
    frame = pl.DataFrame({"day": [date(2026, 11, 1)] * 3, "hour": [2, 3, 25]}).select(clock.study_hour_start_expr(pl.col("day"), pl.col("hour")).alias("s"))
    assert frame["s"].to_list() == [datetime(2026, 11, 1, 6, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 2, 5, tzinfo=UTC)]


def test_price_rows_use_the_dst_flag_for_the_repeated_hour():
    frame = pl.DataFrame({
        "delivery_date": ["11/01/2026", "11/01/2026", "11/01/2026", "11/01/2026", "03/08/2026", "03/08/2026", "09/30/2026"],
        "hour_ending": ["01:00", "02:00", "02:00", "03:00", "02:00", "04:00", "24:00"],
        "dst_flag": ["N", "N", "Y", "N", "N", "N", "N"],
    }).select(clock.hour_ending_start_expr(pl.col("delivery_date").str.to_date("%m/%d/%Y"), pl.col("hour_ending"), pl.col("dst_flag")).alias("s"))
    assert frame["s"].to_list() == [
        datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 6, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 1, 8, tzinfo=UTC),
        datetime(2026, 3, 8, 7, tzinfo=UTC), datetime(2026, 3, 8, 8, tzinfo=UTC),  # hour ending 3 does not exist in spring; 4 follows 2
        datetime(2026, 10, 1, 4, tzinfo=UTC),
    ]
    # the same instants the DAM's study hours give: price hour and model hour meet on the instant
    assert frame["s"][2] == clock.study_hour_start(date(2026, 11, 1), 3) and frame["s"][5] == clock.study_hour_start(date(2026, 3, 8), 3)


def test_workbook_timestamps_take_the_second_duplicate_as_the_repeated_hour():
    naive = pl.Series("t", ["2026-11-01 00:00:00", "2026-11-01 01:00:00", "2026-11-01 01:00:00", "2026-11-01 02:00:00", "2026-03-08 01:00:00", "2026-03-08 03:00:00"]).str.to_datetime()
    out = pl.DataFrame({"t": naive}).select(clock.local_timestamps_expr(pl.col("t")).alias("u"))["u"].to_list()
    assert out == [datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 6, tzinfo=UTC), datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 1, 8, tzinfo=UTC),
                   datetime(2026, 3, 8, 7, tzinfo=UTC), datetime(2026, 3, 8, 8, tzinfo=UTC)]
