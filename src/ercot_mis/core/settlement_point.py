"""``core.settlement_point`` and ``core.settlement_point_bus``: where each settlement
point sits in the network.

ERCOT settles prices at settlement points (resource nodes, hubs, load zones); each
resolves to one or more electrical buses with weights. The CRR model calls those
buses "price nodes" and ships the weights in its SourcesAndSinks file; the DAM model
gives one bus per resource node (``Sp``), the hub buses (``Hb``) and each load's
zone and distribution factor (``Ld``). This module puts both in one shape:

- ``settlement_point``: one row per settlement point per snapshot with its ``kind``
  (``resource_node``, ``hub``, ``load_zone``, ``dc_tie``), the model's own type text,
  how many buses it reaches and how many rows could not be resolved;
- ``settlement_point_bus``: one row per (settlement point, bus) with ``weight``
  (normalized to sum to one over the resolved rows), the ``raw_weight`` as the file
  gave it, the ``source`` file and the PSS/E bus it came through.

Weights are facts from the files, normalized only so they sum to one: CRR hub and
zone rows carry MW-scale weights, DAM zone LDFs sometimes sum to one and sometimes
to the zone's MW. A DAM hub follows Protocols 3.5.2: equal weight per Hub Bus, then
equal weight per energized power flow bus inside it (the ``Hb`` file names both
levels and the status). ERCOT's two average hubs (names ending ``AVG``) are derived
from the four regional hubs: the hub average weighs each hub equally and then as
above; the bus average weighs every Hub Bus of the four hubs equally. A DAM logical resource node has no bus of its own and
takes the bus of its combined-cycle settlement point when the file names one.
"""

from __future__ import annotations

import polars as pl

VERSION = 2

# The hubs the two average hubs are built from (Protocols 3.5.2.6 and 3.5.2.7): the Panhandle hub is not one.
AVERAGE_HUB_MEMBERS = ("HB_NORTH", "HB_SOUTH", "HB_HOUSTON", "HB_WEST")

SP_COLUMNS = ("settlement_point_id", "kind", "type_text", "n_buses", "n_unresolved", "weight_sum_raw")
BUS_COLUMNS = ("settlement_point_id", "bus_key", "weight", "raw_weight", "source", "node_number", "is_resolved")

_EMPTY_NODES = {"settlement_point_id": pl.String, "bus_key": pl.String, "weight": pl.Float64, "raw_weight": pl.Float64,
                "source": pl.String, "node_number": pl.Int64, "is_resolved": pl.Boolean}


def _kind_from_name(name: pl.Expr) -> pl.Expr:
    """ERCOT's naming: ``HB_`` hubs, ``LZ_`` load zones, ``DC`` DC ties, else a resource node."""
    upper = name.str.to_uppercase()
    return (pl.when(upper.str.starts_with("HB_")).then(pl.lit("hub"))
            .when(upper.str.starts_with("LZ_")).then(pl.lit("load_zone"))
            .when(upper.str.starts_with("DC")).then(pl.lit("dc_tie"))
            .otherwise(pl.lit("resource_node")))


def _kind_from_type(type_text: pl.Expr) -> pl.Expr:
    upper = type_text.str.to_uppercase()
    return (pl.when(upper.str.contains("DC TIE")).then(pl.lit("dc_tie"))
            .when(upper.str.contains("HUB")).then(pl.lit("hub"))
            .when(upper.str.contains("LOAD ZONE")).then(pl.lit("load_zone"))
            .otherwise(pl.lit("resource_node")))


def _finish(points: pl.DataFrame, rows: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The caller resolved each row's node to its bus; here weights are normalized and counts taken.

    ``rows`` has settlement_point_id, bus_key (null when unresolved), raw_weight, source, psse_bus.
    Rows landing on the same bus (several CRR nodes joined by closed breakers) are merged.
    """
    rows = rows.with_columns(pl.col("bus_key").is_not_null().alias("is_resolved"))
    resolved = (rows.filter(pl.col("is_resolved"))
                .group_by("settlement_point_id", "bus_key", "source")
                .agg(pl.col("raw_weight").sum(), pl.col("node_number").min()))
    totals = resolved.group_by("settlement_point_id").agg(pl.col("raw_weight").sum().alias("_total"))
    resolved = (resolved.join(totals, on="settlement_point_id")
                .with_columns(pl.when(pl.col("_total") > 0).then(pl.col("raw_weight") / pl.col("_total")).otherwise(None).alias("weight"))
                .with_columns(pl.lit(True).alias("is_resolved")))
    unresolved = rows.filter(~pl.col("is_resolved")).select("settlement_point_id", "bus_key", pl.lit(None, pl.Float64).alias("weight"),
                                                              "raw_weight", "source", "node_number", "is_resolved")
    buses = pl.concat([resolved.select(BUS_COLUMNS), unresolved.select(BUS_COLUMNS)]).sort("settlement_point_id", "bus_key")
    counts = (buses.group_by("settlement_point_id")
              .agg(pl.col("is_resolved").sum().cast(pl.UInt32).alias("n_buses"), (~pl.col("is_resolved")).sum().cast(pl.UInt32).alias("n_unresolved"),
                   pl.col("raw_weight").filter(pl.col("is_resolved")).sum().alias("weight_sum_raw")))
    points = (points.join(counts, on="settlement_point_id", how="left")
              .with_columns(pl.col("n_buses").fill_null(0), pl.col("n_unresolved").fill_null(0))
              .select(SP_COLUMNS).sort("settlement_point_id"))
    return points, buses


def crr_settlement_points(nodes: pl.DataFrame, sources_sinks: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From ``crr_sources_and_sinks`` (Name, PriceNode, BusName "number name", ParticipationFactor)."""
    to_node = nodes.select("node_number", "bus_key")
    rows = (sources_sinks.select(pl.col("name").alias("settlement_point_id"),
                                 pl.col("bus_name").str.extract(r"^\s*(\d+)").cast(pl.Int64, strict=False).alias("node_number"),
                                 pl.col("participation_factor").alias("raw_weight"), pl.lit("crr_sources_sinks").alias("source"))
            .join(to_node, on="node_number", how="left"))
    points = (sources_sinks.select(pl.col("name").alias("settlement_point_id")).unique()
              .with_columns(_kind_from_name(pl.col("settlement_point_id")).alias("kind"), pl.lit(None, pl.String).alias("type_text")))
    return _finish(points, rows)


def dam_settlement_points(nodes: pl.DataFrame, settlement_points: pl.DataFrame, hub_buses: pl.DataFrame,
                          loads: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From one hour's ``dam_settlement_points``, ``dam_hub_buses`` and ``dam_loads``."""
    to_node = nodes.select("node_number", "bus_key")
    sp = settlement_points.select(pl.col("settlement_point_name").alias("settlement_point_id"), pl.col("settlement_point_type").alias("type_text"),
                                  pl.col("psse_bus_number").cast(pl.Int64).alias("node_number"),
                                  pl.col("combined_cycle_settlement_point").cast(pl.String).alias("_cc"))
    points = sp.select("settlement_point_id", _kind_from_type(pl.col("type_text")).alias("kind"), "type_text").unique(subset=["settlement_point_id"])

    # Resource nodes: the file's bus; a logical resource node borrows its combined-cycle point's bus.
    by_name = sp.select(pl.col("settlement_point_id").alias("_cc"), pl.col("node_number").alias("_cc_bus"))
    direct = (sp.join(by_name, on="_cc", how="left")
              .with_columns(pl.coalesce(pl.col("node_number"), pl.col("_cc_bus")).alias("node_number"))
              .join(points.select("settlement_point_id", "kind"), on="settlement_point_id")
              .filter((pl.col("kind") == "resource_node") | ((pl.col("kind") == "dc_tie") & pl.col("node_number").is_not_null()))
              .select("settlement_point_id", "node_number", pl.lit(1.0).alias("raw_weight"), pl.lit("dam_sp").alias("source")))

    # Hubs (SP-01, Protocols 3.5.2.x): a hub is the plain average of its Hub Buses, and a Hub Bus the
    # plain average of its energized power flow buses, so a bus weighs 1 / (hub buses) / (buses in its
    # hub bus). De-energized rows are left out, as ERCOT leaves them out of the price. The two average
    # hubs are derived: the hub average weighs the four regional hubs equally (not Panhandle), the bus
    # average weighs every Hub Bus of those four hubs equally.
    hb = hub_buses.select(pl.col("hub_name").cast(pl.String).alias("settlement_point_id"), pl.col("hub_bus_name").cast(pl.String).alias("_hub_bus"),
                          pl.col("psse_bus_number").cast(pl.Int64).alias("node_number"),
                          pl.col("bus_status").cast(pl.String).str.to_uppercase().str.starts_with("ENERGIZED").alias("_energized"))
    hub_names = points.filter(pl.col("kind") == "hub")["settlement_point_id"]
    real = hb.filter(pl.col("settlement_point_id").is_in(hub_names.implode()) & pl.col("_energized")).drop("_energized")
    real = (real.with_columns(pl.col("node_number").count().over("settlement_point_id", "_hub_bus").alias("_in_hub_bus"))
                .with_columns(pl.col("_hub_bus").n_unique().over("settlement_point_id").alias("_hub_buses"))
                .with_columns((1.0 / pl.col("_hub_buses") / pl.col("_in_hub_bus")).alias("raw_weight")))
    derived = [name for name in hub_names if name not in set(real["settlement_point_id"]) and name.upper().endswith("AVG")]
    regional = real.filter(pl.col("settlement_point_id").str.to_uppercase().is_in(list(AVERAGE_HUB_MEMBERS)))
    parts = [real.select("settlement_point_id", "node_number", "raw_weight")]
    for name in derived:
        if "BUS" in name.upper():
            parts.append(regional.with_columns(pl.col("_hub_bus").n_unique().alias("_all_hub_buses"))
                         .select(pl.lit(name).alias("settlement_point_id"), "node_number", (1.0 / pl.col("_all_hub_buses") / pl.col("_in_hub_bus")).alias("raw_weight")))
        else:
            parts.append(regional.with_columns(pl.col("settlement_point_id").n_unique().alias("_hubs"))
                         .select(pl.lit(name).alias("settlement_point_id"), "node_number", (pl.col("raw_weight") / pl.col("_hubs")).alias("raw_weight")))
    hubs = pl.concat(parts).with_columns(pl.lit("dam_hub_buses").alias("source"))

    # Load zones: in-service loads of the zone, weighted by their MW distribution factor.
    zone_names = points.filter(pl.col("kind").is_in(["load_zone", "dc_tie"]))["settlement_point_id"]
    zones = (loads.filter(pl.col("load_zone_name").is_in(zone_names.implode()) & pl.col("load_status").str.to_uppercase().str.starts_with("IN"))
             .select(pl.col("load_zone_name").cast(pl.String).alias("settlement_point_id"), pl.col("psse_bus_number").cast(pl.Int64).alias("node_number"),
                     pl.col("raw_mw_ldf").cast(pl.Float64).alias("raw_weight"), pl.lit("dam_loads").alias("source")))
    zones = zones.filter(~pl.col("settlement_point_id").is_in(direct["settlement_point_id"].implode()))  # a DC tie with a bus of its own keeps it

    rows = pl.concat([direct, hubs, zones], how="diagonal_relaxed").join(to_node, on="node_number", how="left")
    # A settlement point with no row at all (a hub without Hb rows, a zone without loads) still appears, unresolved.
    missing = points.filter(~pl.col("settlement_point_id").is_in(rows["settlement_point_id"].implode())).select(
        "settlement_point_id", pl.lit(None, pl.Int64).alias("node_number"), pl.lit(None, pl.Float64).alias("raw_weight"),
        pl.lit("dam_sp").alias("source"), pl.lit(None, pl.String).alias("bus_key"))
    rows = pl.concat([rows.select("settlement_point_id", "node_number", "raw_weight", "source", "bus_key"), missing])
    return _finish(points, rows)


def no_settlement_points() -> tuple[pl.DataFrame, pl.DataFrame]:
    points = pl.DataFrame(schema={"settlement_point_id": pl.String, "kind": pl.String, "type_text": pl.String, "n_buses": pl.UInt32,
                                  "n_unresolved": pl.UInt32, "weight_sum_raw": pl.Float64})
    return points, pl.DataFrame(schema=_EMPTY_NODES)
