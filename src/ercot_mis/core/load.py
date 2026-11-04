"""``core.load``: every load of a snapshot, where it sits and whether it is in service.

Columns: ``load_id`` (the DAM load name; for CRR ``<node name>:<load id>``, with ``#<node number>``
when two nodes share a name), ``node_number`` (CRR) or ``bus_number`` (DAM), ``psse_load_id``,
``bus_key``, ``is_in_service`` and ``mw`` from the RAW record, and from the DAM ``Ld`` file
``load_zone``, ``weather_zone``, ``is_ercot_load``, ``is_conforming``, ``mw_ldf``,
``is_rollover_capable`` and ``n_rollover_targets``. Each model's loads are kept as given;
``core.diff_load`` compares them. The CRR model's zone weights are per bus, in
``core.settlement_point_bus``.
"""

from __future__ import annotations

import polars as pl

from ..raw.table import flag

VERSION = 3

COLUMNS = ("load_id", "node_number", "bus_number", "psse_load_id", "bus_key", "is_in_service", "mw", "load_zone", "weather_zone",
           "is_ercot_load", "is_conforming", "mw_ldf", "is_rollover_capable", "n_rollover_targets")
_DAM_ONLY = {"load_zone": pl.String, "weather_zone": pl.String, "is_ercot_load": pl.Boolean, "is_conforming": pl.Boolean,
             "mw_ldf": pl.Float64, "is_rollover_capable": pl.Boolean, "n_rollover_targets": pl.Int64}


def crr_loads(nodes: pl.DataFrame, psse_load: pl.DataFrame) -> pl.DataFrame:
    """``core.load`` for one CRR month from ``psse_load``; the RAW comment is ``<node number> <node name> <load id> <owner>``."""
    to_node = nodes.select("node_number", "bus_key")
    tokens = pl.col("comment").str.strip_chars().str.split(" ")
    bus_name = tokens.list.slice(1, tokens.list.len() - 3).list.join(" ")
    return (psse_load.select((pl.coalesce(bus_name, pl.col("i").cast(pl.String)) + ":" + pl.col("id").cast(pl.String).str.strip_chars()).alias("load_id"),
                             pl.col("i").cast(pl.Int64).alias("node_number"), pl.col("id").cast(pl.String).str.strip_chars().alias("psse_load_id"),
                             (pl.col("status") == 1).alias("is_in_service"), pl.col("pl").cast(pl.Float64).alias("mw"))
            .join(to_node, on="node_number", how="left")
            # Two nodes can share a name; those loads get the node number as well.
            .with_columns(pl.when(pl.len().over("load_id") > 1).then(pl.col("load_id") + "#" + pl.col("node_number").cast(pl.String))
                          .otherwise(pl.col("load_id")).alias("load_id"))
            .with_columns([pl.lit(None, dtype).alias(name) for name, dtype in _DAM_ONLY.items()] + [pl.lit(None, pl.Int64).alias("bus_number")])
            .select(COLUMNS).sort("load_id", "node_number"))


def dam_loads(buses: pl.DataFrame, psse_load: pl.DataFrame, loads: pl.DataFrame) -> pl.DataFrame:
    """From one hour's ``psse_load`` and ``dam_loads`` (joined on bus and load id); ``buses`` is the hour's ``core.bus``."""
    to_bus = buses.select("bus_number", "bus_key")
    raw = psse_load.select(pl.col("i").cast(pl.Int64).alias("bus_number"), pl.col("id").cast(pl.String).str.strip_chars().alias("psse_load_id"),
                           (pl.col("status") == 1).alias("_raw_in_service"), pl.col("pl").cast(pl.Float64).alias("mw"))
    return (loads.select(pl.col("load_name").cast(pl.String).alias("load_id"), pl.col("psse_bus_number").cast(pl.Int64).alias("bus_number"),
                         pl.col("psse_load_id").cast(pl.String).str.strip_chars().alias("psse_load_id"),
                         flag(pl.col("load_status"), "IN").alias("_csv_in_service"),
                         pl.col("load_zone_name").cast(pl.String).alias("load_zone"), pl.col("weather_zone_name").cast(pl.String).alias("weather_zone"),
                         flag(pl.col("ercot_load"), "Y").alias("is_ercot_load"),
                         flag(pl.col("conforming_or_non_conforming"), "C").alias("is_conforming"),
                         pl.col("raw_mw_ldf").cast(pl.Float64).alias("mw_ldf"), flag(pl.col("load_rollover_capable"), "Y").alias("is_rollover_capable"),
                         pl.col("number_of_target_loads").cast(pl.Int64, strict=False).alias("n_rollover_targets"))
            .join(raw, on=["bus_number", "psse_load_id"], how="left").join(to_bus, on="bus_number", how="left")
            .with_columns(pl.coalesce(pl.col("_raw_in_service"), pl.col("_csv_in_service")).alias("is_in_service"), pl.lit(None, pl.Int64).alias("node_number"))
            .select(COLUMNS).sort("load_id", "bus_number"))
