"""``out.injection``: the day-ahead market's net injection per settlement point and hour, from ``core.hourly_award``."""

from __future__ import annotations

from datetime import date

import polars as pl

from ..core import award


def injections(session, day: date) -> pl.DataFrame:
    """``q`` for one operating day: one row per (hour, settlement point) with ``net_mw`` and the kinds behind it."""
    rows = session.core("hourly_award").filter(pl.col("delivery_date") == f"{day:%m/%d/%Y}").collect()
    return award.net_injections(rows)


def balance(session, day: date) -> pl.DataFrame:
    """Per hour of one day: MW by kind, the net, and the net as a share of supply."""
    rows = session.core("hourly_award").filter(pl.col("delivery_date") == f"{day:%m/%d/%Y}").collect()
    return award.hourly_balance(rows)
