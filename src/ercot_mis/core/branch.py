"""``core.branch`` and ``core.branch_rating``: lines and transformers per snapshot.

A branch is identified across snapshots by ``branch_id``: the DAM ``Branch Name``, the
CRR RAW line comment, or the CRR ``Autos`` name for transformers (with a
``XF <from> <to> <ckt>`` fallback when the workbook has no row). Endpoints carry both
the snapshot's PSS/E numbers and the stable ``node_key``s from ``core.node``, so a
CRR bus tie has equal endpoint keys after contraction and is flagged ``is_tie``.

Ratings are facts from two sources kept side by side in ``core.branch_rating``:
``psse_raw`` (rate A/B/C from the RAW, one row per branch) and, for CRR,
``crr_monitored`` (the monitored-element CSV, one row per time-of-use block). Nothing
is derated or merged here.
"""

from __future__ import annotations

import polars as pl

from .node import TIE_REACTANCE

VERSION = 1  # bump when columns or identities change

BRANCH_COLUMNS = ("branch_id", "kind", "from_bus", "to_bus", "ckt", "from_node_key", "to_node_key",
                  "is_in_service", "is_tie", "r_pu", "x_pu", "b_pu", "tap_ratio", "angle_deg",
                  "is_monitored", "is_secured")
RATING_COLUMNS = ("branch_id", "rating_source", "time_of_use", "base_mw", "emergency_mw", "rate_c_mw")


def _keys(nodes: pl.DataFrame) -> pl.DataFrame:
    return nodes.select("psse_bus_number", "node_key")


def _with_endpoints(frame: pl.DataFrame, nodes: pl.DataFrame) -> pl.DataFrame:
    keys = _keys(nodes)
    return (frame.join(keys.rename({"psse_bus_number": "from_bus", "node_key": "from_node_key"}), on="from_bus", how="left")
            .join(keys.rename({"psse_bus_number": "to_bus", "node_key": "to_node_key"}), on="to_bus", how="left"))


def _raw_parts(psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame) -> pl.DataFrame:
    """RAW lines and transformers in one frame with the physical columns of ``core.branch``."""
    lines = psse_branch.select(
        pl.lit("line").alias("kind"), pl.col("i").alias("from_bus"), pl.col("j").alias("to_bus"),
        pl.col("ckt").str.strip_chars().alias("ckt"), (pl.col("st") == 1).alias("is_in_service"),
        pl.col("r").alias("r_pu"), pl.col("x").alias("x_pu"), pl.col("b").alias("b_pu"),
        pl.lit(None, pl.Float64).alias("tap_ratio"), pl.lit(None, pl.Float64).alias("angle_deg"),
        pl.col("ratea").alias("base_mw"), pl.col("rateb").alias("emergency_mw"), pl.col("ratec").alias("rate_c_mw"),
        pl.col("comment"))
    xf = psse_transformer.select(
        pl.lit("transformer").alias("kind"), pl.col("i").alias("from_bus"), pl.col("j").alias("to_bus"),
        pl.col("ckt").str.strip_chars().alias("ckt"), (pl.col("stat") == 1).alias("is_in_service"),
        pl.col("r1_2").alias("r_pu"), pl.col("x1_2").alias("x_pu"), pl.lit(0.0).alias("b_pu"),
        (pl.col("windv1") / pl.col("windv2")).alias("tap_ratio"), pl.col("ang1").alias("angle_deg"),
        pl.col("rata1").alias("base_mw"), pl.col("ratb1").alias("emergency_mw"), pl.col("ratc1").alias("rate_c_mw"),
        pl.col("comment"))
    return pl.concat([lines, xf])


def _finish(frame: pl.DataFrame, tie_reactance: float) -> tuple[pl.DataFrame, pl.DataFrame]:
    frame = frame.with_columns(((pl.col("kind") == "line") & (pl.col("x_pu").abs() <= tie_reactance)).alias("is_tie"))
    ratings = frame.select("branch_id", pl.lit("psse_raw").alias("rating_source"), pl.lit(None, pl.String).alias("time_of_use"),
                           "base_mw", "emergency_mw", "rate_c_mw")
    return frame.select(BRANCH_COLUMNS).sort("kind", "from_bus", "to_bus", "ckt"), ratings


def dam_branches(nodes: pl.DataFrame, psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame,
                 dam_lines: pl.DataFrame, dam_transformers: pl.DataFrame,
                 tie_reactance: float = TIE_REACTANCE) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.branch`` and ``core.branch_rating`` for one DAM hour."""
    named = pl.concat([
        frame.select(pl.col("psse_from_bus_number").alias("from_bus"), pl.col("psse_to_bus_number").alias("to_bus"),
                     pl.col("psse_ckt_id").str.strip_chars().alias("ckt"), pl.col("branch_name").alias("branch_id"),
                     (pl.col("monitored").str.to_uppercase().str.starts_with("Y")).alias("is_monitored"),
                     (pl.col("monitored_and_secured").str.to_uppercase().str.starts_with("Y")).alias("is_secured"))
        for frame in (dam_lines, dam_transformers)])
    frame = _with_endpoints(_raw_parts(psse_branch, psse_transformer).join(named, on=["from_bus", "to_bus", "ckt"], how="left"), nodes)
    return _finish(frame, tie_reactance)


def crr_branches(nodes: pl.DataFrame, psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame,
                 autos: pl.DataFrame, monitored: pl.DataFrame,
                 tie_reactance: float = TIE_REACTANCE) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.branch`` and ``core.branch_rating`` for one CRR month.

    Lines are named by the RAW comment, transformers by ``Autos`` (from, to, ckt).
    ``is_monitored`` is whether the monitored-element CSV lists the branch; CRR has no
    secured flag, so ``is_secured`` equals ``is_monitored``.
    """
    auto_names = autos.select(pl.col("from_number").cast(pl.Int64, strict=False).alias("from_bus"),
                              pl.col("to_number").cast(pl.Int64, strict=False).alias("to_bus"),
                              pl.col("id").str.strip_chars().alias("ckt"), pl.col("crr_name").alias("auto_name")).drop_nulls(["from_bus", "to_bus"])
    frame = (_raw_parts(psse_branch, psse_transformer)
             .join(auto_names, on=["from_bus", "to_bus", "ckt"], how="left")
             .with_columns(pl.when(pl.col("kind") == "line").then(pl.col("comment"))
                           .otherwise(pl.coalesce(pl.col("auto_name"), pl.format("XF {} {} {}", "from_bus", "to_bus", "ckt")))
                           .alias("branch_id")))
    listed = monitored.select(pl.col("device_name").alias("branch_id")).unique().with_columns(pl.lit(True).alias("is_monitored"))
    frame = (_with_endpoints(frame, nodes).join(listed, on="branch_id", how="left")
             .with_columns(pl.col("is_monitored").fill_null(False)).with_columns(pl.col("is_monitored").alias("is_secured")))
    branch, raw_ratings = _finish(frame, tie_reactance)
    csv_ratings = monitored.select(pl.col("device_name").alias("branch_id"), pl.lit("crr_monitored").alias("rating_source"),
                                   "time_of_use", pl.col("base_case_rating").alias("base_mw"),
                                   pl.col("emergency_rating").alias("emergency_mw"), pl.lit(None, pl.Float64).alias("rate_c_mw"))
    return branch, pl.concat([raw_ratings, csv_ratings]).select(RATING_COLUMNS)
