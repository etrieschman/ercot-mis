"""``out.injection``: the day-ahead market's net injection per settlement point and hour, from ``core.hourly_award``."""

from __future__ import annotations

from datetime import date

import polars as pl

from ..core import award


def awards(session, day: date) -> pl.DataFrame:
    """Every award of one operating day, from ERCOT's latest posting of the day (TOP-11)."""
    return session.latest_posting(session.core("hourly_award").filter(pl.col("delivery_date") == f"{day:%m/%d/%Y}").collect())


def injections(session, day: date) -> pl.DataFrame:
    """``q`` for one operating day: one row per (hour, settlement point) with ``net_mw`` and the award kinds behind it."""
    return award.net_injections(awards(session, day))
