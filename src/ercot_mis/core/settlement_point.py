"""``core.settlement_point`` and ``core.settlement_point_bus``: each settlement point of a snapshot and the buses it is priced on, with weights summing to one.

Sources: the CRR sources and sinks file (participation factors per node); the DAM ``Sp`` file (one
bus per resource node), ``Hb`` file (hub buses: equal weight per Hub Bus, then per energized bus
inside it, Protocols 3.5.2; SP-01), ``Ld`` file (load zones by distribution factor, with rollover;
SP-02) and ``Gn`` file (a combined-cycle train's units, an equal share per in-service unit where
Protocols 4.6.1.4 uses unpublished limits; SP-05). ``raw_weight`` is the file's number, ``weight``
the normalized one, ``node_number`` or ``bus_number`` the PSS/E number the row came through.
Details: docs/datasets/settlement-point-mappings.md and docs/datasets/dam-network-model.md.
"""

from __future__ import annotations

import polars as pl
from ..raw.table import flag

VERSION = 7

# The hubs the two average hubs are built from (Protocols 3.5.2.6 and 3.5.2.7): the Panhandle hub is not one.
AVERAGE_HUB_MEMBERS = ("HB_NORTH", "HB_SOUTH", "HB_HOUSTON", "HB_WEST")

SP_COLUMNS = ("settlement_point_id", "kind", "type_text", "n_buses", "n_unresolved", "weight_sum_raw")
BUS_COLUMNS = ("settlement_point_id", "bus_key", "weight", "raw_weight", "source", "node_number", "bus_number", "is_resolved")


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


def _normalized(points: pl.DataFrame, rows: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Weights normalized to sum to one per settlement point over the resolved rows, and the per-point counts; rows on the same bus (CRR nodes one bus joins) are merged."""
    rows = rows.with_columns(pl.col("bus_key").is_not_null().alias("is_resolved"))
    resolved = (rows.filter(pl.col("is_resolved"))
                .group_by("settlement_point_id", "bus_key", "source")
                .agg(pl.col("raw_weight").sum(), pl.col("node_number").min(), pl.col("bus_number").min()))
    totals = resolved.group_by("settlement_point_id").agg(pl.col("raw_weight").sum().alias("_total"))
    resolved = (resolved.join(totals, on="settlement_point_id")
                .with_columns(pl.when(pl.col("_total") > 0).then(pl.col("raw_weight") / pl.col("_total")).otherwise(None).alias("weight"))
                .with_columns(pl.lit(True).alias("is_resolved")))
    unresolved = rows.filter(~pl.col("is_resolved")).select("settlement_point_id", "bus_key", pl.lit(None, pl.Float64).alias("weight"),
                                                              "raw_weight", "source", "node_number", "bus_number", "is_resolved")
    buses = pl.concat([resolved.select(BUS_COLUMNS), unresolved.select(BUS_COLUMNS)]).sort("settlement_point_id", "bus_key", "source")
    counts = (buses.group_by("settlement_point_id")
              .agg(pl.col("bus_key").filter(pl.col("is_resolved")).n_unique().cast(pl.UInt32).alias("n_buses"), (~pl.col("is_resolved")).sum().cast(pl.UInt32).alias("n_unresolved"),
                   pl.col("raw_weight").filter(pl.col("is_resolved")).sum().alias("weight_sum_raw")))
    points = (points.join(counts, on="settlement_point_id", how="left")
              .with_columns(pl.col("n_buses").fill_null(0), pl.col("n_unresolved").fill_null(0))
              .select(SP_COLUMNS).sort("settlement_point_id"))
    return points, buses


def crr_settlement_points(nodes: pl.DataFrame, sources_sinks: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From ``crr_sources_and_sinks`` (Name, PriceNode, BusName "number name", ParticipationFactor)."""
    to_bus = nodes.select("node_number", "bus_key")
    rows = (sources_sinks.select(pl.col("name").alias("settlement_point_id"),
                                 pl.col("bus_name").str.extract(r"^\s*(\d+)").cast(pl.Int64, strict=False).alias("node_number"),
                                 pl.col("participation_factor").alias("raw_weight"), pl.lit("crr_sources_sinks").alias("source"),
                                 pl.lit(None, pl.Int64).alias("bus_number"))
            .join(to_bus, on="node_number", how="left"))
    points = (sources_sinks.select(pl.col("name").alias("settlement_point_id")).unique()
              .with_columns(_kind_from_name(pl.col("settlement_point_id")).alias("kind"), pl.lit(None, pl.String).alias("type_text")))
    return _normalized(points, rows)


def dam_settlement_points(buses: pl.DataFrame, settlement_points: pl.DataFrame, hub_buses: pl.DataFrame,
                          loads: pl.DataFrame, generators: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """One DAM hour's settlement points and their bus weights, from its ``Sp``, ``Hb``, ``Ld`` and ``Gn`` files; ``buses`` is the hour's ``core.bus``."""
    sp = settlement_points.select(pl.col("settlement_point_name").alias("settlement_point_id"), pl.col("settlement_point_type").alias("type_text"),
                                  pl.col("psse_bus_number").cast(pl.Int64).alias("bus_number"),
                                  pl.col("combined_cycle_settlement_point").cast(pl.String).alias("_cc"))
    points = sp.select("settlement_point_id", _kind_from_type(pl.col("type_text")).alias("kind"), "type_text").unique(subset=["settlement_point_id"])
    direct = _dam_resource_nodes(sp, points, generators)
    zones = _dam_zones(loads, points).filter(~pl.col("settlement_point_id").is_in(direct["settlement_point_id"].implode()))  # a DC tie with a bus of its own keeps it
    rows = pl.concat([direct, _dam_hubs(hub_buses, points), zones], how="diagonal_relaxed").join(buses.select("bus_number", "bus_key"),
                                                                                                 on="bus_number", how="left")
    # A settlement point with no row at all (a hub without Hb rows, a zone without loads) still appears, unresolved.
    missing = points.filter(~pl.col("settlement_point_id").is_in(rows["settlement_point_id"].implode())).select(
        "settlement_point_id", pl.lit(None, pl.Int64).alias("bus_number"), pl.lit(None, pl.Float64).alias("raw_weight"),
        pl.lit("dam_sp").alias("source"), pl.lit(None, pl.String).alias("bus_key"))
    return _normalized(points, pl.concat([rows.select("settlement_point_id", "bus_number", "raw_weight", "source", "bus_key"), missing])
                   .with_columns(pl.lit(None, pl.Int64).alias("node_number")))


def _dam_resource_nodes(sp: pl.DataFrame, points: pl.DataFrame, generators: pl.DataFrame) -> pl.DataFrame:
    """Resource nodes and DC ties on a bus of their own, from ``Sp``; a combined-cycle train takes its combined-cycle point's bus, else its in-service units' resource node buses from ``Gn`` in equal shares (SP-05)."""
    by_name = sp.select(pl.col("settlement_point_id").alias("_cc"), pl.col("bus_number").alias("_cc_bus"))
    direct = (sp.join(by_name, on="_cc", how="left")
              .with_columns(pl.coalesce(pl.col("bus_number"), pl.col("_cc_bus")).alias("bus_number"))
              .join(points.select("settlement_point_id", "kind"), on="settlement_point_id")
              .filter((pl.col("kind") == "resource_node") | ((pl.col("kind") == "dc_tie") & pl.col("bus_number").is_not_null()))
              .select("settlement_point_id", "bus_number", pl.lit(1.0).alias("raw_weight"), pl.lit("dam_sp").alias("source")))
    column = "combined_cycle_logical_resource_node_settlement_point_name"
    without_bus = direct.filter(pl.col("bus_number").is_null())["settlement_point_id"]
    if without_bus.is_empty():
        return direct
    units = (generators.filter(pl.col(column).is_in(without_bus.implode()))
             .select(pl.col(column).cast(pl.String).alias("settlement_point_id"),
                     pl.col("resource_node_psse_bus_number").cast(pl.Int64).alias("bus_number"),
                     flag(pl.col("generator_status"), "IN").fill_null(False).alias("in_service")))
    units = (units.filter(pl.col("in_service") | ~pl.col("in_service").any().over("settlement_point_id"))
             .select("settlement_point_id", "bus_number", pl.lit(1.0).alias("raw_weight"), pl.lit("dam_gn").alias("source")))
    placed = pl.col("bus_number").is_null() & pl.col("settlement_point_id").is_in(units["settlement_point_id"].implode())
    return pl.concat([direct.filter(~placed), units], how="diagonal_relaxed")


def _dam_hubs(hub_buses: pl.DataFrame, points: pl.DataFrame) -> pl.DataFrame:
    """Hub weights per Protocols 3.5.2 (SP-01): equal per Hub Bus, then equal per energized bus inside it; the two average hubs are built from the four regional hubs (3.5.2.6 and 3.5.2.7)."""
    hb = hub_buses.select(pl.col("hub_name").cast(pl.String).alias("settlement_point_id"), pl.col("hub_bus_name").cast(pl.String).alias("_hub_bus"),
                          pl.col("psse_bus_number").cast(pl.Int64).alias("bus_number"),
                          flag(pl.col("bus_status"), "ENERGIZED").alias("_energized"))
    hub_names = points.filter(pl.col("kind") == "hub")["settlement_point_id"]
    real = (hb.filter(pl.col("settlement_point_id").is_in(hub_names.implode()) & pl.col("_energized")).drop("_energized")
            .with_columns(pl.col("bus_number").count().over("settlement_point_id", "_hub_bus").alias("_in_hub_bus"))
            .with_columns(pl.col("_hub_bus").n_unique().over("settlement_point_id").alias("_hub_buses"))
            .with_columns((1.0 / pl.col("_hub_buses") / pl.col("_in_hub_bus")).alias("raw_weight")))
    regional = real.filter(pl.col("settlement_point_id").str.to_uppercase().is_in(list(AVERAGE_HUB_MEMBERS)))
    parts = [real.select("settlement_point_id", "bus_number", "raw_weight")]
    for name in (n for n in hub_names if n not in set(real["settlement_point_id"]) and n.upper().endswith("AVG")):
        if "BUS" in name.upper():
            parts.append(regional.with_columns(pl.col("_hub_bus").n_unique().alias("_all_hub_buses"))
                         .select(pl.lit(name).alias("settlement_point_id"), "bus_number",
                                 (1.0 / pl.col("_all_hub_buses") / pl.col("_in_hub_bus")).alias("raw_weight")))
        else:
            parts.append(regional.with_columns(pl.col("settlement_point_id").n_unique().alias("_hubs"))
                         .select(pl.lit(name).alias("settlement_point_id"), "bus_number", (pl.col("raw_weight") / pl.col("_hubs")).alias("raw_weight")))
    return pl.concat(parts).with_columns(pl.lit("dam_hub_buses").alias("source"))


def _dam_zones(loads: pl.DataFrame, points: pl.DataFrame) -> pl.DataFrame:
    """Load zone and DC-tie zone weights (SP-02): the zone's in-service loads by MW distribution factor, with an out-of-service rollover-capable load's MW handed to its in-service targets by the file's fractions."""
    zone_names = points.filter(pl.col("kind").is_in(["load_zone", "dc_tie"]))["settlement_point_id"]
    in_service = flag(pl.col("load_status"), "IN")
    zone_loads = loads.filter(pl.col("load_zone_name").is_in(zone_names.implode()))
    zones = (zone_loads.filter(in_service)
             .select(pl.col("load_zone_name").cast(pl.String).alias("settlement_point_id"), pl.col("psse_bus_number").cast(pl.Int64).alias("bus_number"),
                     pl.col("raw_mw_ldf").cast(pl.Float64).alias("raw_weight"), pl.lit("dam_loads").alias("source")))
    targets = [(f"{n}_target_load_name", f"fraction_of_this_load_to_{n}_target_load") for n in ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th")]
    rolling = zone_loads.filter(~in_service & flag(pl.col("load_rollover_capable"), "Y"))
    receivers = loads.filter(in_service).select(pl.col("load_name").alias("_target"), pl.col("psse_bus_number").cast(pl.Int64).alias("bus_number"))
    rolled = pl.concat([
        rolling.select(pl.col("load_zone_name").cast(pl.String).alias("settlement_point_id"), pl.col(name).cast(pl.String).alias("_target"),
                       (pl.col("raw_mw_ldf").cast(pl.Float64) * pl.col(fraction).cast(pl.Float64)).alias("raw_weight"))
        for name, fraction in targets]).filter(pl.col("_target").is_not_null() & (pl.col("raw_weight") > 0))
    rolled = rolled.join(receivers, on="_target", how="inner").select("settlement_point_id", "bus_number", "raw_weight",
                                                                      pl.lit("dam_loads_rollover").alias("source"))
    return pl.concat([zones, rolled])

