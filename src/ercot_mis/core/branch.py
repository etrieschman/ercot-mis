"""``core.branch`` and ``core.branch_rating``: lines and transformers per snapshot.

A branch is identified across snapshots by ``branch_id``: the DAM ``Branch Name``, the
CRR RAW line comment, or the CRR ``Autos`` name for transformers (with a
``XF <from> <to> <ckt>`` fallback when the workbook has no row). Endpoints carry both
the snapshot's PSS/E numbers and the stable ``bus_key``s from ``core.node``, so a
CRR bus tie has equal endpoint keys after contraction and is flagged ``is_tie``.

Ratings are facts from two sources kept side by side in ``core.branch_rating``:
``psse_raw`` (rate A/B/C from the RAW, one row per branch) and, for CRR,
``crr_monitored`` (the monitored-element CSV, one row per time-of-use block). Nothing
is derated or merged here.
"""

from __future__ import annotations

import polars as pl

from .node import TIE_REACTANCE

VERSION = 4  # bump when columns or identities change

# ``is_name_reversed``: the branch's name lists its ends in the opposite order to the RAW's
# (from, to). CRR line comments always follow the RAW; CRR transformer names follow the
# ``Autos`` sheet, which is swapped relative to the RAW for a large minority. A CSV's
# "From-To" flow direction refers to the name's order, so consumers flip the sign here.
BRANCH_COLUMNS = ("branch_id", "kind", "from_node", "to_node", "ckt", "from_bus_key", "to_bus_key",
                  "is_in_service", "is_tie", "is_name_reversed", "r_pu", "x_pu", "b_pu", "tap_ratio", "angle_deg",
                  "is_monitored", "is_secured", "is_temporary")
RATING_COLUMNS = ("branch_id", "rating_source", "time_of_use", "base_mw", "emergency_mw", "rate_c_mw")


def autos_by_key(autos: pl.DataFrame) -> pl.DataFrame:
    """``Autos`` names keyed by (from_node, to_node, ckt) in both orientations: the sheet
    lists some transformers with from and to swapped relative to the RAW. ``is_swapped``
    says which orientation a key came from."""
    direct = autos.select(pl.col("from_number").cast(pl.Int64, strict=False).alias("from_node"),
                          pl.col("to_number").cast(pl.Int64, strict=False).alias("to_node"),
                          pl.col("id").str.strip_chars().alias("ckt"), pl.col("crr_name").alias("name"),
                          pl.lit(False).alias("is_swapped")).drop_nulls(["from_node", "to_node"])
    swapped = direct.select(pl.col("to_node").alias("from_node"), pl.col("from_node").alias("to_node"), "ckt", "name", pl.lit(True).alias("is_swapped"))
    return pl.concat([direct, swapped]).unique(subset=["from_node", "to_node", "ckt"], keep="first")


def _keys(nodes: pl.DataFrame) -> pl.DataFrame:
    return nodes.select("node_number", "bus_key")


def _with_endpoints(frame: pl.DataFrame, nodes: pl.DataFrame) -> pl.DataFrame:
    keys = _keys(nodes)
    return (frame.join(keys.rename({"node_number": "from_node", "bus_key": "from_bus_key"}), on="from_node", how="left")
            .join(keys.rename({"node_number": "to_node", "bus_key": "to_bus_key"}), on="to_node", how="left"))


def _raw_parts(psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame) -> pl.DataFrame:
    """RAW lines and transformers in one frame with the physical columns of ``core.branch``."""
    lines = psse_branch.select(
        pl.lit("line").alias("kind"), pl.col("i").alias("from_node"), pl.col("j").alias("to_node"),
        pl.col("ckt").str.strip_chars().alias("ckt"), (pl.col("st") == 1).alias("is_in_service"),
        pl.col("r").alias("r_pu"), pl.col("x").alias("x_pu"), pl.col("b").alias("b_pu"),
        pl.lit(None, pl.Float64).alias("tap_ratio"), pl.lit(None, pl.Float64).alias("angle_deg"),
        pl.col("ratea").alias("base_mw"), pl.col("rateb").alias("emergency_mw"), pl.col("ratec").alias("rate_c_mw"),
        pl.col("comment"))
    xf = psse_transformer.select(
        pl.lit("transformer").alias("kind"), pl.col("i").alias("from_node"), pl.col("j").alias("to_node"),
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
    return frame.select(BRANCH_COLUMNS).sort("kind", "from_node", "to_node", "ckt"), ratings


def dam_branches(nodes: pl.DataFrame, psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame,
                 dam_lines: pl.DataFrame, dam_transformers: pl.DataFrame,
                 tie_reactance: float = TIE_REACTANCE) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.branch`` and ``core.branch_rating`` for one DAM hour."""
    named = pl.concat([
        frame.select(pl.col("psse_from_bus_number").alias("from_node"), pl.col("psse_to_bus_number").alias("to_node"),
                     pl.col("psse_ckt_id").str.strip_chars().alias("ckt"), pl.col("branch_name").alias("branch_id"),
                     (pl.col("monitored").str.to_uppercase().str.starts_with("Y")).alias("is_monitored"),
                     (pl.col("monitored_and_secured").str.to_uppercase().str.starts_with("Y")).alias("is_secured"))
        for frame in (dam_lines, dam_transformers)])
    frame = _with_endpoints(_raw_parts(psse_branch, psse_transformer).join(named, on=["from_node", "to_node", "ckt"], how="left"), nodes)
    return _finish(frame.with_columns(pl.lit(False).alias("is_name_reversed"), pl.lit(False).alias("is_temporary")), tie_reactance)


def crr_branches(nodes: pl.DataFrame, psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame,
                 autos: pl.DataFrame, monitored: pl.DataFrame, mapping_lines: pl.DataFrame | None = None,
                 tie_reactance: float = TIE_REACTANCE) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.branch`` and ``core.branch_rating`` for one CRR month.

    ``is_temporary`` marks the branches the mapping workbook labels as temporary split-bus
    topology added for outages (its operations equipment code says so in words): ERCOT put
    them in this model for an outage, they are not part of the standing network.

    Lines are named by the RAW comment, transformers by ``Autos`` (from, to, ckt).
    ``is_monitored`` is whether the monitored-element CSV lists the branch; CRR has no
    secured flag, so ``is_secured`` equals ``is_monitored``.
    """
    auto_names = autos_by_key(autos).rename({"name": "auto_name"})
    frame = (_raw_parts(psse_branch, psse_transformer)
             .join(auto_names, on=["from_node", "to_node", "ckt"], how="left")
             .with_columns(pl.when(pl.col("kind") == "line").then(pl.col("comment"))
                           .otherwise(pl.coalesce(pl.col("auto_name"), pl.format("XF {} {} {}", "from_node", "to_node", "ckt")))
                           .alias("branch_id"),
                           ((pl.col("kind") == "transformer") & pl.col("is_swapped").fill_null(False)).alias("is_name_reversed")))
    listed = monitored.select(pl.col("device_name").alias("branch_id")).unique().with_columns(pl.lit(True).alias("is_monitored"))
    frame = (_with_endpoints(frame, nodes).join(listed, on="branch_id", how="left")
             .with_columns(pl.col("is_monitored").fill_null(False)).with_columns(pl.col("is_monitored").alias("is_secured")))
    temporary = (mapping_lines.filter(pl.col("op_eqcode").cast(pl.String).str.to_uppercase().str.contains("TEMPORARY SPLIT"))
                 .select(pl.col("crr_tag").alias("branch_id")).unique().with_columns(pl.lit(True).alias("is_temporary"))
                 if mapping_lines is not None else pl.DataFrame(schema={"branch_id": pl.String, "is_temporary": pl.Boolean}))
    frame = frame.join(temporary, on="branch_id", how="left").with_columns(pl.col("is_temporary").fill_null(False))
    branch, raw_ratings = _finish(frame, tie_reactance)
    csv_ratings = monitored.select(pl.col("device_name").alias("branch_id"), pl.lit("crr_monitored").alias("rating_source"),
                                   "time_of_use", pl.col("base_case_rating").alias("base_mw"),
                                   pl.col("emergency_rating").alias("emergency_mw"), pl.lit(None, pl.Float64).alias("rate_c_mw"))
    return branch, pl.concat([raw_ratings, csv_ratings]).select(RATING_COLUMNS)
