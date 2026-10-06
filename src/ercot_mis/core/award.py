"""``core.hourly_award``: every energy award the day-ahead market made, as a signed MW at a settlement point.

Built from the 60-Day DAM Disclosure (``raw.dam_60d_*``), one package per operating day.
A row is one award: the hour (``interval_start_utc`` and ERCOT's ``delivery_date`` and
``hour_ending``), the settlement point, the award's ``kind`` and its ``mw`` signed as an
injection into the grid (INJ-01):

- ``generation`` and ``storage``: a resource's awarded quantity at its resource node
  (storage charging is a negative award in the file and stays negative);
- ``energy_only_offer``: positive, at its settlement point;
- ``energy_bid``: a purchase; the file already writes bid awards as negative MW and they
  are kept as written (adding a sign of our own doubled them, measured on the first days);
- ``ptp_obligation_source`` / ``ptp_obligation_sink``: a point-to-point obligation (bid or
  linked to an option) is an injection at its source and the same withdrawal at its sink.

Summed over a settlement point and hour this is the DAM's net injection, ``q``
(``out.injection``). Summed over the system it should balance to zero within the
market's tolerance (INJ-02), which ``scripts/check_injections.py`` measures. The files
carry no DST flag, so on the long day the repeated hour is read as its first occurrence
(NAM-08).
"""

from __future__ import annotations

import polars as pl

from ..clock import hour_ending_start_expr

VERSION = 2

COLUMNS = ("interval_start_utc", "delivery_date", "hour_ending", "settlement_point", "kind", "mw", "source_id")


def _rows(frame: pl.DataFrame, point: str, mw: str, kind: str, source_id: str, sign: float = 1.0) -> pl.DataFrame:
    return frame.select(
        pl.col("delivery_date").cast(pl.String), pl.col("hour_ending").cast(pl.String),
        pl.col(point).cast(pl.String).alias("settlement_point"), pl.lit(kind).alias("kind"),
        (pl.col(mw).cast(pl.Float64) * sign).alias("mw"), pl.col(source_id).cast(pl.String).alias("source_id"),
    ).filter(pl.col("mw").is_not_null() & (pl.col("mw") != 0))


def hourly_awards(gen: pl.DataFrame, esr: pl.DataFrame, energy_only_offers: pl.DataFrame, energy_bids: pl.DataFrame,
                  ptp_bids: pl.DataFrame, ptp_options: pl.DataFrame) -> pl.DataFrame:
    """One frame of signed awards from the six award tables of one disclosure day."""
    parts = [
        _rows(gen, "settlement_point_name", "awarded_quantity", "generation", "resource_name"),
        _rows(esr, "settlement_point_name", "awarded_quantity", "storage", "resource_name"),
        _rows(energy_only_offers, "settlement_point", "energy_only_offer_award_in_mw", "energy_only_offer", "offer_id"),
        _rows(energy_bids, "settlement_point", "energy_only_bid_award_in_mw", "energy_bid", "bid_id"),  # negative in the file
        _rows(ptp_bids, "settlement_point_source", "pt_p_bid_award_mw", "ptp_obligation_source", "bid_id"),
        _rows(ptp_bids, "settlement_point_sink", "pt_p_bid_award_mw", "ptp_obligation_sink", "bid_id", -1.0),
        _rows(ptp_options, "settlement_point_source", "mw", "ptp_obligation_source", "offer_id"),
        _rows(ptp_options, "settlement_point_sink", "mw", "ptp_obligation_sink", "offer_id", -1.0),
    ]
    rows = pl.concat(parts)
    rows = rows.with_columns(hour_ending_start_expr(pl.col("delivery_date").str.to_date("%m/%d/%Y"), pl.col("hour_ending"), pl.lit(None, pl.String)).alias("interval_start_utc"))
    return rows.select(COLUMNS).sort("interval_start_utc", "kind", "settlement_point", "source_id")


def net_injections(awards: pl.DataFrame) -> pl.DataFrame:
    """``q``: net MW per (hour, settlement point), with the kinds that made it up as columns."""
    by_kind = awards.pivot(on="kind", index=["interval_start_utc", "delivery_date", "hour_ending", "settlement_point"], values="mw", aggregate_function="sum")
    kinds = [c for c in by_kind.columns if c not in ("interval_start_utc", "delivery_date", "hour_ending", "settlement_point")]
    return (by_kind.with_columns(pl.sum_horizontal([pl.col(k).fill_null(0.0) for k in kinds]).alias("net_mw"))
            .sort("interval_start_utc", "settlement_point"))


def hourly_balance(awards: pl.DataFrame) -> pl.DataFrame:
    """Per hour: MW by kind, the net, and the net as a share of supply (INJ-02)."""
    by_hour = awards.pivot(on="kind", index=["interval_start_utc", "delivery_date", "hour_ending"], values="mw", aggregate_function="sum").fill_null(0.0)
    kinds = [c for c in by_hour.columns if c not in ("interval_start_utc", "delivery_date", "hour_ending")]
    supply = pl.sum_horizontal([pl.when(pl.col(k) > 0).then(pl.col(k)).otherwise(0.0) for k in kinds])
    return (by_hour.with_columns(pl.sum_horizontal([pl.col(k) for k in kinds]).alias("net_mw"), supply.alias("supply_mw"))
            .with_columns((pl.col("net_mw") / pl.col("supply_mw")).alias("net_share_of_supply")).sort("interval_start_utc"))
