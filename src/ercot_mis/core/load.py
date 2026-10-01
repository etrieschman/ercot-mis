"""``core.load``: every load of a model, where it sits and whether it is in service.

Loads are where the two models most often disagree without saying so: a load out for
a long outage, a temporary one added on a split bus, a distribution factor moved to
another load. Core records each model's loads as it gives them; comparing the two is
``core.diff_load``'s job, and nothing is changed here.

One row per load per snapshot:

- ``load_id``: the DAM's load name; for CRR, whose RAW names only the bus, the bus
  name and the PSS/E load id (``<bus name>:<id>``, with ``#<bus number>`` when two
  buses share a name);
- ``psse_bus``, ``psse_load_id``, ``bus_key``: where it is attached;
- ``is_in_service`` and ``mw`` from the RAW record (status and constant-power MW);
- DAM only, from the ``Ld`` file: ``load_zone``, ``weather_zone``, ``is_ercot_load``,
  ``is_conforming``, ``mw_ldf`` (the distribution factor the zone price is weighted
  by), ``is_rollover_capable`` and ``n_rollover_targets`` (the loads this one's share
  moves to when it is out).

The CRR model has no per-load zone or factor; its zone weights are per bus, in
``core.settlement_point_bus``.
"""

from __future__ import annotations

import polars as pl

VERSION = 2

COLUMNS = ("load_id", "node_number", "psse_load_id", "bus_key", "is_in_service", "mw", "load_zone", "weather_zone",
           "is_ercot_load", "is_conforming", "mw_ldf", "is_rollover_capable", "n_rollover_targets")
_DAM_ONLY = {"load_zone": pl.String, "weather_zone": pl.String, "is_ercot_load": pl.Boolean, "is_conforming": pl.Boolean,
             "mw_ldf": pl.Float64, "is_rollover_capable": pl.Boolean, "n_rollover_targets": pl.Int64}


def _yes(expr: pl.Expr) -> pl.Expr:
    return expr.cast(pl.String).str.to_uppercase().str.starts_with("Y")


def crr_loads(nodes: pl.DataFrame, psse_load: pl.DataFrame) -> pl.DataFrame:
    """From one month's ``psse_load``; the RAW comment is ``<bus number> <bus name> <load id> <owner>``."""
    to_node = nodes.select("node_number", "bus_key")
    tokens = pl.col("comment").str.strip_chars().str.split(" ")
    bus_name = tokens.list.slice(1, tokens.list.len() - 3).list.join(" ")
    return (psse_load.select((pl.coalesce(bus_name, pl.col("i").cast(pl.String)) + ":" + pl.col("id").cast(pl.String).str.strip_chars()).alias("load_id"),
                             pl.col("i").cast(pl.Int64).alias("node_number"), pl.col("id").cast(pl.String).str.strip_chars().alias("psse_load_id"),
                             (pl.col("status") == 1).alias("is_in_service"), pl.col("pl").cast(pl.Float64).alias("mw"))
            .join(to_node, on="node_number", how="left")
            # Two buses can share a name; those loads get the bus number as well.
            .with_columns(pl.when(pl.len().over("load_id") > 1).then(pl.col("load_id") + "#" + pl.col("node_number").cast(pl.String))
                          .otherwise(pl.col("load_id")).alias("load_id"))
            .with_columns([pl.lit(None, dtype).alias(name) for name, dtype in _DAM_ONLY.items()])
            .select(COLUMNS).sort("load_id", "node_number"))


def dam_loads(nodes: pl.DataFrame, psse_load: pl.DataFrame, loads: pl.DataFrame) -> pl.DataFrame:
    """From one hour's ``psse_load`` and ``dam_loads`` (joined on bus and load id)."""
    to_node = nodes.select("node_number", "bus_key")
    raw = psse_load.select(pl.col("i").cast(pl.Int64).alias("node_number"), pl.col("id").cast(pl.String).str.strip_chars().alias("psse_load_id"),
                           (pl.col("status") == 1).alias("_raw_in_service"), pl.col("pl").cast(pl.Float64).alias("mw"))
    return (loads.select(pl.col("load_name").cast(pl.String).alias("load_id"), pl.col("psse_bus_number").cast(pl.Int64).alias("node_number"),
                         pl.col("psse_load_id").cast(pl.String).str.strip_chars().alias("psse_load_id"),
                         pl.col("load_status").str.to_uppercase().str.starts_with("IN").alias("_csv_in_service"),
                         pl.col("load_zone_name").cast(pl.String).alias("load_zone"), pl.col("weather_zone_name").cast(pl.String).alias("weather_zone"),
                         _yes(pl.col("ercot_load")).alias("is_ercot_load"),
                         pl.col("conforming_or_non_conforming").cast(pl.String).str.to_uppercase().str.starts_with("C").alias("is_conforming"),
                         pl.col("raw_mw_ldf").cast(pl.Float64).alias("mw_ldf"), _yes(pl.col("load_rollover_capable")).alias("is_rollover_capable"),
                         pl.col("number_of_target_loads").cast(pl.Int64, strict=False).alias("n_rollover_targets"))
            .join(raw, on=["node_number", "psse_load_id"], how="left").join(to_node, on="node_number", how="left")
            .with_columns(pl.coalesce(pl.col("_raw_in_service"), pl.col("_csv_in_service")).alias("is_in_service"))
            .select(COLUMNS).sort("load_id", "node_number"))
