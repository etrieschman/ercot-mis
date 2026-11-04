"""``core.branch`` and ``core.branch_rating``: the lines and transformers of a snapshot, and their ratings from every source.

``branch_id`` identifies a branch across snapshots: the DAM ``Branch Name``, the CRR RAW line
comment, or the CRR ``Autos`` name for transformers (``XF <from> <to> <ckt>`` when the workbook
has no row). Each end carries the model's PSS/E number (``from_node``/``to_node`` for CRR nodes,
``from_bus_number``/``to_bus_number`` for DAM buses) and its ``bus_key``; a CRR tie joins two
nodes of one bus and is flagged ``is_tie`` (TOP-10). ``core.branch_rating`` keeps the RAW's rate
A/B/C (``psse_raw``) and the CRR monitored-element CSV's ratings per time-of-use block
(``crr_monitored``) side by side, unchanged (RAT-04). Details: docs/datasets/crr-network-model.md
and docs/datasets/dam-network-model.md.
"""

from __future__ import annotations

import polars as pl

from ..raw.table import flag
from .bus import TIE_REACTANCE, autos_by_key

VERSION = 5  # bump when columns or identities change

# ``is_name_reversed``: the branch's name lists its ends in the opposite order to the RAW's (from, to).
# CRR line comments follow the RAW (NAM-01); CRR transformer names follow the ``Autos`` sheet, which is
# swapped relative to the RAW for some transformers (NAM-02). A CSV's "From-To" flow direction refers
# to the name's order, so consumers flip the sign where the name is reversed.
BRANCH_COLUMNS = ("branch_id", "kind", "from_node", "to_node", "from_bus_number", "to_bus_number", "ckt", "from_bus_key", "to_bus_key",
                  "is_in_service", "is_tie", "is_name_reversed", "r_pu", "x_pu", "b_pu", "tap_ratio", "angle_deg",
                  "is_monitored", "is_secured", "is_temporary")
RATING_COLUMNS = ("branch_id", "rating_source", "time_of_use", "base_mw", "emergency_mw", "rate_c_mw")


def resolved(rows: pl.DataFrame, branches: pl.DataFrame) -> pl.DataFrame:
    """``rows`` with ``is_resolved`` (its ``branch_id`` names one of ``branches``) and ``branch_id`` nulled where it does not."""
    known = branches.select("branch_id", pl.lit(True).alias("is_resolved"))
    return (rows.join(known, on="branch_id", how="left").with_columns(pl.col("is_resolved").fill_null(False))
            .with_columns(pl.when(pl.col("is_resolved")).then(pl.col("branch_id")).otherwise(None).alias("branch_id")))


def _with_endpoints(frame: pl.DataFrame, points: pl.DataFrame, number: str) -> pl.DataFrame:
    """Add ``from_bus_key`` and ``to_bus_key``: ``points`` is ``core.node`` (CRR, on ``node_number``) or ``core.bus`` (DAM, on ``bus_number``)."""
    keys = points.select(pl.col(number).alias("from_number"), pl.col("bus_key").alias("from_bus_key"))
    return (frame.join(keys, on="from_number", how="left")
            .join(keys.rename({"from_number": "to_number", "from_bus_key": "to_bus_key"}), on="to_number", how="left"))


def _raw_parts(psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame) -> pl.DataFrame:
    """RAW lines and transformers in one frame with the physical columns of ``core.branch``."""
    lines = psse_branch.select(
        pl.lit("line").alias("kind"), pl.col("i").alias("from_number"), pl.col("j").alias("to_number"),
        pl.col("ckt").str.strip_chars().alias("ckt"), (pl.col("st") == 1).alias("is_in_service"),
        pl.col("r").alias("r_pu"), pl.col("x").alias("x_pu"), pl.col("b").alias("b_pu"),
        pl.lit(None, pl.Float64).alias("tap_ratio"), pl.lit(None, pl.Float64).alias("angle_deg"),
        pl.col("ratea").alias("base_mw"), pl.col("rateb").alias("emergency_mw"), pl.col("ratec").alias("rate_c_mw"),
        pl.col("comment"))
    _check_transformer_conventions(psse_transformer)
    xf = psse_transformer.select(
        pl.lit("transformer").alias("kind"), pl.col("i").alias("from_number"), pl.col("j").alias("to_number"),
        pl.col("ckt").str.strip_chars().alias("ckt"), (pl.col("stat") == 1).alias("is_in_service"),
        pl.col("r1_2").alias("r_pu"), pl.col("x1_2").alias("x_pu"), pl.lit(0.0).alias("b_pu"),
        (pl.col("windv1") / pl.col("windv2")).alias("tap_ratio"), pl.col("ang1").alias("angle_deg"),
        pl.col("rata1").alias("base_mw"), pl.col("ratb1").alias("emergency_mw"), pl.col("ratc1").alias("rate_c_mw"),
        pl.col("comment"))
    return pl.concat([lines, xf])


def _check_transformer_conventions(psse_transformer: pl.DataFrame) -> None:
    """RAT-01: a transformer is read as a reactance and a tap ratio, which holds under PSS/E's plain conventions only:
    ``CW = 1`` (winding voltages as turns ratios), ``CZ = 1`` (impedance on the system base) and ``ang1 = 0`` (no phase
    shift). A RAW that breaks one fails the build; the message carries counts, never names.
    """
    checks = {"cw": pl.col("cw") != 1, "cz": pl.col("cz") != 1, "ang1": pl.col("ang1").fill_null(0.0) != 0.0}
    bad = {name: int(psse_transformer.filter(expr).height) for name, expr in checks.items()}
    if any(bad.values()):
        raise ValueError(f"transformer conventions the DC model assumes do not hold (rows breaking each): {bad}; see RAT-01 in docs/assumptions.md")


def _ties_and_raw_ratings(frame: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Flag the ties, then split the frame into ``core.branch`` columns and the RAW's rating rows."""
    frame = frame.with_columns(((pl.col("kind") == "line") & (pl.col("x_pu").abs() <= TIE_REACTANCE)).alias("is_tie"))
    ratings = frame.select("branch_id", pl.lit("psse_raw").alias("rating_source"), pl.lit(None, pl.String).alias("time_of_use"),
                           "base_mw", "emergency_mw", "rate_c_mw")
    return frame.sort("kind", "from_number", "to_number", "ckt").select(BRANCH_COLUMNS), ratings


def dam_branches(buses: pl.DataFrame, psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame,
                 dam_lines: pl.DataFrame, dam_transformers: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.branch`` and ``core.branch_rating`` for one DAM hour; ``buses`` is the hour's ``core.bus``."""
    named = pl.concat([
        frame.select(pl.col("psse_from_bus_number").alias("from_number"), pl.col("psse_to_bus_number").alias("to_number"),
                     pl.col("psse_ckt_id").str.strip_chars().alias("ckt"), pl.col("branch_name").alias("branch_id"),
                     (flag(pl.col("monitored"), "Y")).alias("is_monitored"),
                     (flag(pl.col("monitored_and_secured"), "Y")).alias("is_secured"))
        for frame in (dam_lines, dam_transformers)])
    frame = _with_endpoints(_raw_parts(psse_branch, psse_transformer).join(named, on=["from_number", "to_number", "ckt"], how="left"), buses, "bus_number")
    return _ties_and_raw_ratings(frame.with_columns(pl.lit(False).alias("is_name_reversed"), pl.lit(False).alias("is_temporary"),
                                      pl.lit(None, pl.Int64).alias("from_node"), pl.lit(None, pl.Int64).alias("to_node"),
                                      pl.col("from_number").alias("from_bus_number"), pl.col("to_number").alias("to_bus_number")))


def crr_branches(nodes: pl.DataFrame, psse_branch: pl.DataFrame, psse_transformer: pl.DataFrame,
                 autos: pl.DataFrame, monitored: pl.DataFrame, mapping_lines: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.branch`` and ``core.branch_rating`` for one CRR month; ``nodes`` is the month's ``core.node``.

    Lines are named by the RAW comment, transformers by the ``Autos`` sheet on (from, to, ckt). ``is_monitored``
    is whether the monitored-element CSV lists the branch, and ``is_secured`` equals it, since the CRR has no
    secured flag. ``is_temporary`` marks the branches the mapping workbook labels as temporary split-bus
    topology added for an outage.
    """
    auto_names = autos_by_key(autos).rename({"name": "auto_name", "from_node": "from_number", "to_node": "to_number"})
    frame = (_raw_parts(psse_branch, psse_transformer)
             .join(auto_names, on=["from_number", "to_number", "ckt"], how="left")
             .with_columns(pl.when(pl.col("kind") == "line").then(pl.col("comment"))
                           .otherwise(pl.coalesce(pl.col("auto_name"), pl.format("XF {} {} {}", "from_number", "to_number", "ckt")))
                           .alias("branch_id"),
                           ((pl.col("kind") == "transformer") & pl.col("is_swapped").fill_null(False)).alias("is_name_reversed")))
    listed = monitored.select(pl.col("device_name").alias("branch_id")).unique().with_columns(pl.lit(True).alias("is_monitored"))
    frame = (_with_endpoints(frame, nodes, "node_number").join(listed, on="branch_id", how="left")
             .with_columns(pl.col("is_monitored").fill_null(False)).with_columns(pl.col("is_monitored").alias("is_secured")))
    temporary = (mapping_lines.filter(pl.col("op_eqcode").cast(pl.String).str.to_uppercase().str.contains("TEMPORARY SPLIT"))
                 .select(pl.col("crr_tag").alias("branch_id")).unique().with_columns(pl.lit(True).alias("is_temporary")))
    frame = (frame.join(temporary, on="branch_id", how="left")
             .with_columns(pl.col("is_temporary").fill_null(False), pl.col("from_number").alias("from_node"), pl.col("to_number").alias("to_node"),
                           pl.lit(None, pl.Int64).alias("from_bus_number"), pl.lit(None, pl.Int64).alias("to_bus_number")))
    branch, raw_ratings = _ties_and_raw_ratings(frame)
    csv_ratings = monitored.select(pl.col("device_name").alias("branch_id"), pl.lit("crr_monitored").alias("rating_source"),
                                   "time_of_use", pl.col("base_case_rating").alias("base_mw"),
                                   pl.col("emergency_rating").alias("emergency_mw"), pl.lit(None, pl.Float64).alias("rate_c_mw"))
    return branch, pl.concat([raw_ratings, csv_ratings]).select(RATING_COLUMNS)
