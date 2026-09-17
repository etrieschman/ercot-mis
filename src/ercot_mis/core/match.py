"""``core.match_branch``: which CRR branch is which DAM branch.

The CRR mapping workbook gives every CRR line and transformer an ``Operations_Name``
(the Network Operations Model name). DAM names its branches with that name plus
DAM's own circuit designator (one or two characters), so a match is found in this
order, and the first method that succeeds is recorded as ``match_method``:

1. ``exact``: the names are equal once punctuation and case are ignored;
2. ``ops+ckt``: the DAM name is the operations name followed by the CRR circuit id;
3. ``prefix``: exactly one DAM name is the operations name followed by at most two
   characters;
4. ``unmatched``: the CRR branch has no operations name, a placeholder, or several
   DAM candidates (``n_candidates`` says how many).

Every CRR branch appears once; DAM branches nothing matched appear with a null CRR
side. Differences are never smoothed over: a match records identity only.
"""

from __future__ import annotations

import re

import polars as pl

VERSION = 1

COLUMNS = ("crr_branch_id", "dam_branch_id", "match_method", "operations_name", "n_candidates")


def _key(expr: pl.Expr) -> pl.Expr:
    return expr.str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")


def placeholder_name(mapping_lines: pl.DataFrame) -> str | None:
    """The one operations name ERCOT uses for rows it could not map (shared by many tags)."""
    counts = mapping_lines.group_by("operations_name").len().filter(pl.col("len") > 5).sort("len", descending=True)
    return counts["operations_name"][0] if counts.height else None


def match_branches(crr_branch: pl.DataFrame, mapping_lines: pl.DataFrame, mapping_autos: pl.DataFrame,
                   dam_branch: pl.DataFrame) -> pl.DataFrame:
    """One row per CRR branch with its DAM branch, plus unmatched DAM branches."""
    placeholder = placeholder_name(mapping_lines)
    ops = pl.concat([
        mapping_lines.select(pl.col("crr_tag").alias("crr_branch_id"), "operations_name"),
        mapping_autos.select(pl.col("crr_name").alias("crr_branch_id"), "operations_name"),
    ]).unique(subset=["crr_branch_id"], keep="first")
    if placeholder is not None:
        ops = ops.with_columns(pl.when(pl.col("operations_name") == placeholder).then(None).otherwise(pl.col("operations_name")).alias("operations_name"))
    crr = crr_branch.select("branch_id", "ckt").rename({"branch_id": "crr_branch_id"}).join(ops, on="crr_branch_id", how="left")
    crr = crr.with_columns(_key(pl.col("operations_name")).alias("_ops"), (_key(pl.col("operations_name")) + _key(pl.col("ckt"))).alias("_ops_ckt"))
    dam = dam_branch.select(pl.col("branch_id").alias("dam_branch_id")).unique().with_columns(_key(pl.col("dam_branch_id")).alias("_dam"))

    exact = crr.join(dam, left_on="_ops", right_on="_dam", how="inner").with_columns(pl.lit("exact").alias("match_method"))
    rest = crr.filter(~pl.col("crr_branch_id").is_in(exact["crr_branch_id"].implode()))
    by_ckt = rest.join(dam, left_on="_ops_ckt", right_on="_dam", how="inner").with_columns(pl.lit("ops+ckt").alias("match_method"))
    rest = rest.filter(~pl.col("crr_branch_id").is_in(by_ckt["crr_branch_id"].implode()))

    # Prefix: DAM key = ops key + 1-2 characters. Join on the DAM key with its last one or two characters removed.
    stems = pl.concat([dam.with_columns(pl.col("_dam").str.slice(0, pl.col("_dam").str.len_chars() - n).alias("_stem")) for n in (1, 2)])
    candidates = (rest.filter(pl.col("_ops").is_not_null() & (pl.col("_ops") != ""))
                  .join(stems, left_on="_ops", right_on="_stem", how="inner")
                  .group_by("crr_branch_id").agg(pl.col("dam_branch_id").unique().alias("_cands")))
    prefix = (rest.join(candidates, on="crr_branch_id", how="left")
              .with_columns(pl.col("_cands").list.len().fill_null(0).alias("n_candidates"))
              .with_columns(pl.when(pl.col("n_candidates") == 1).then(pl.col("_cands").list.first()).otherwise(None).alias("dam_branch_id"),
                            pl.when(pl.col("n_candidates") == 1).then(pl.lit("prefix")).otherwise(pl.lit("unmatched")).alias("match_method")))

    matched = pl.concat([f.select("crr_branch_id", "dam_branch_id", "match_method", "operations_name", pl.lit(1, pl.UInt32).alias("n_candidates"))
                         for f in (exact, by_ckt)] + [prefix.select("crr_branch_id", "dam_branch_id", "match_method", "operations_name", pl.col("n_candidates").cast(pl.UInt32))])
    leftover = dam.filter(~pl.col("dam_branch_id").is_in(matched["dam_branch_id"].drop_nulls().implode())).select(
        pl.lit(None, pl.String).alias("crr_branch_id"), "dam_branch_id", pl.lit("unmatched").alias("match_method"),
        pl.lit(None, pl.String).alias("operations_name"), pl.lit(0, pl.UInt32).alias("n_candidates"))
    return pl.concat([matched, leftover]).select(COLUMNS).sort("match_method", "crr_branch_id", "dam_branch_id")
