"""``core.settlement_point`` and ``core.settlement_point_node``: where each settlement
point sits in the network.

ERCOT settles prices at settlement points (resource nodes, hubs, load zones); each
resolves to one or more electrical buses with weights. The CRR model calls those
buses price nodes and ships the weights in its SourcesAndSinks file; the DAM model
gives one bus per resource node (``Sp``), the hub buses (``Hb``) and each load's
zone and distribution factor (``Ld``). This module puts both in one shape:

- ``settlement_point``: one row per settlement point per snapshot with its ``kind``
  (``resource_node``, ``hub``, ``load_zone``, ``dc_tie``), the model's own type text,
  how many nodes it reaches and how many rows could not be resolved;
- ``settlement_point_node``: one row per (settlement point, node) with ``weight``
  (normalized to sum to one over the resolved rows), the ``raw_weight`` as the file
  gave it, the ``source`` file and the PSS/E bus it came through.

Weights are facts from the files, normalized only so they sum to one: CRR hub and
zone rows carry MW-scale weights, DAM zone LDFs sometimes sum to one and sometimes
to the zone's MW. ERCOT's two average hubs (names ending ``AVG``) are derived: the
bus average puts equal weight on every hub bus, the hub average puts equal weight on
each hub and then on its buses. A DAM logical resource node has no bus of its own and
takes the bus of its combined-cycle settlement point when the file names one.
"""

from __future__ import annotations

import polars as pl

VERSION = 1

SP_COLUMNS = ("settlement_point_id", "kind", "type_text", "n_nodes", "n_unresolved", "weight_sum_raw")
NODE_COLUMNS = ("settlement_point_id", "node_key", "weight", "raw_weight", "source", "psse_bus", "is_resolved")

_EMPTY_NODES = {"settlement_point_id": pl.String, "node_key": pl.String, "weight": pl.Float64, "raw_weight": pl.Float64,
                "source": pl.String, "psse_bus": pl.Int64, "is_resolved": pl.Boolean}


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
    """Resolve buses to nodes was done by the caller; here weights are normalized and counts taken.

    ``rows`` has settlement_point_id, node_key (null when unresolved), raw_weight, source, psse_bus.
    Rows landing on the same node (CRR buses of one contracted node) are merged.
    """
    rows = rows.with_columns(pl.col("node_key").is_not_null().alias("is_resolved"))
    resolved = (rows.filter(pl.col("is_resolved"))
                .group_by("settlement_point_id", "node_key", "source")
                .agg(pl.col("raw_weight").sum(), pl.col("psse_bus").min()))
    totals = resolved.group_by("settlement_point_id").agg(pl.col("raw_weight").sum().alias("_total"))
    resolved = (resolved.join(totals, on="settlement_point_id")
                .with_columns(pl.when(pl.col("_total") > 0).then(pl.col("raw_weight") / pl.col("_total")).otherwise(None).alias("weight"))
                .with_columns(pl.lit(True).alias("is_resolved")))
    unresolved = rows.filter(~pl.col("is_resolved")).select("settlement_point_id", "node_key", pl.lit(None, pl.Float64).alias("weight"),
                                                              "raw_weight", "source", "psse_bus", "is_resolved")
    nodes = pl.concat([resolved.select(NODE_COLUMNS), unresolved.select(NODE_COLUMNS)]).sort("settlement_point_id", "node_key")
    counts = (nodes.group_by("settlement_point_id")
              .agg(pl.col("is_resolved").sum().cast(pl.UInt32).alias("n_nodes"), (~pl.col("is_resolved")).sum().cast(pl.UInt32).alias("n_unresolved"),
                   pl.col("raw_weight").filter(pl.col("is_resolved")).sum().alias("weight_sum_raw")))
    points = (points.join(counts, on="settlement_point_id", how="left")
              .with_columns(pl.col("n_nodes").fill_null(0), pl.col("n_unresolved").fill_null(0))
              .select(SP_COLUMNS).sort("settlement_point_id"))
    return points, nodes


def crr_settlement_points(nodes: pl.DataFrame, sources_sinks: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From ``crr_sources_and_sinks`` (Name, PriceNode, BusName "number name", ParticipationFactor)."""
    to_node = nodes.select(pl.col("psse_bus_number").alias("psse_bus"), "node_key")
    rows = (sources_sinks.select(pl.col("name").alias("settlement_point_id"),
                                 pl.col("bus_name").str.extract(r"^\s*(\d+)").cast(pl.Int64, strict=False).alias("psse_bus"),
                                 pl.col("participation_factor").alias("raw_weight"), pl.lit("crr_sources_sinks").alias("source"))
            .join(to_node, on="psse_bus", how="left"))
    points = (sources_sinks.select(pl.col("name").alias("settlement_point_id")).unique()
              .with_columns(_kind_from_name(pl.col("settlement_point_id")).alias("kind"), pl.lit(None, pl.String).alias("type_text")))
    return _finish(points, rows)


def dam_settlement_points(nodes: pl.DataFrame, settlement_points: pl.DataFrame, hub_buses: pl.DataFrame,
                          loads: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From one hour's ``dam_settlement_points``, ``dam_hub_buses`` and ``dam_loads``."""
    to_node = nodes.select(pl.col("psse_bus_number").alias("psse_bus"), "node_key")
    sp = settlement_points.select(pl.col("settlement_point_name").alias("settlement_point_id"), pl.col("settlement_point_type").alias("type_text"),
                                  pl.col("psse_bus_number").cast(pl.Int64).alias("psse_bus"),
                                  pl.col("combined_cycle_settlement_point").cast(pl.String).alias("_cc"))
    points = sp.select("settlement_point_id", _kind_from_type(pl.col("type_text")).alias("kind"), "type_text").unique(subset=["settlement_point_id"])

    # Resource nodes: the file's bus; a logical resource node borrows its combined-cycle point's bus.
    by_name = sp.select(pl.col("settlement_point_id").alias("_cc"), pl.col("psse_bus").alias("_cc_bus"))
    direct = (sp.join(by_name, on="_cc", how="left")
              .with_columns(pl.coalesce(pl.col("psse_bus"), pl.col("_cc_bus")).alias("psse_bus"))
              .join(points.select("settlement_point_id", "kind"), on="settlement_point_id")
              .filter((pl.col("kind") == "resource_node") | ((pl.col("kind") == "dc_tie") & pl.col("psse_bus").is_not_null()))
              .select("settlement_point_id", "psse_bus", pl.lit(1.0).alias("raw_weight"), pl.lit("dam_sp").alias("source")))

    # Hubs: the Hb file's buses at equal weight; the two average hubs are derived from the real ones.
    hb = hub_buses.select(pl.col("hub_name").cast(pl.String).alias("settlement_point_id"), pl.col("psse_bus_number").cast(pl.Int64).alias("psse_bus"))
    hub_names = points.filter(pl.col("kind") == "hub")["settlement_point_id"]
    real = hb.filter(pl.col("settlement_point_id").is_in(hub_names.implode())).with_columns(pl.lit(1.0).alias("raw_weight"))
    derived = [name for name in hub_names if name not in set(real["settlement_point_id"]) and name.upper().endswith("AVG")]
    parts = [real]
    for name in derived:
        if "BUS" in name.upper():
            parts.append(real.select(pl.lit(name).alias("settlement_point_id"), "psse_bus", pl.lit(1.0).alias("raw_weight")))
        else:
            per_hub = real.group_by("settlement_point_id").len().rename({"len": "_n"})
            parts.append(real.join(per_hub, on="settlement_point_id").select(pl.lit(name).alias("settlement_point_id"), "psse_bus", (1.0 / pl.col("_n")).alias("raw_weight")))
    hubs = pl.concat(parts).with_columns(pl.lit("dam_hub_buses").alias("source")) if parts else pl.DataFrame(schema={
        "settlement_point_id": pl.String, "psse_bus": pl.Int64, "raw_weight": pl.Float64, "source": pl.String})

    # Load zones: in-service loads of the zone, weighted by their MW distribution factor.
    zone_names = points.filter(pl.col("kind").is_in(["load_zone", "dc_tie"]))["settlement_point_id"]
    zones = (loads.filter(pl.col("load_zone_name").is_in(zone_names.implode()) & pl.col("load_status").str.to_uppercase().str.starts_with("IN"))
             .select(pl.col("load_zone_name").cast(pl.String).alias("settlement_point_id"), pl.col("psse_bus_number").cast(pl.Int64).alias("psse_bus"),
                     pl.col("raw_mw_ldf").cast(pl.Float64).alias("raw_weight"), pl.lit("dam_loads").alias("source")))
    zones = zones.filter(~pl.col("settlement_point_id").is_in(direct["settlement_point_id"].implode()))  # a DC tie with a bus of its own keeps it

    rows = pl.concat([direct, hubs, zones], how="diagonal_relaxed").join(to_node, on="psse_bus", how="left")
    # A settlement point with no row at all (a hub without Hb rows, a zone without loads) still appears, unresolved.
    missing = points.filter(~pl.col("settlement_point_id").is_in(rows["settlement_point_id"].implode())).select(
        "settlement_point_id", pl.lit(None, pl.Int64).alias("psse_bus"), pl.lit(None, pl.Float64).alias("raw_weight"),
        pl.lit("dam_sp").alias("source"), pl.lit(None, pl.String).alias("node_key"))
    rows = pl.concat([rows.select("settlement_point_id", "psse_bus", "raw_weight", "source", "node_key"), missing])
    return _finish(points, rows)


def no_settlement_points() -> tuple[pl.DataFrame, pl.DataFrame]:
    points = pl.DataFrame(schema={"settlement_point_id": pl.String, "kind": pl.String, "type_text": pl.String, "n_nodes": pl.UInt32,
                                  "n_unresolved": pl.UInt32, "weight_sum_raw": pl.Float64})
    return points, pl.DataFrame(schema=_EMPTY_NODES)
