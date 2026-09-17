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


# ------------------------------------------------------------------ nodes

NODE_COLUMNS = ("crr_node_key", "dam_node_key", "match_method", "settlement_point", "n_votes", "n_candidates")


def _settlement_points(nodes: pl.DataFrame) -> pl.DataFrame:
    """(settlement_point, node_key) pairs from the ``S:`` labels in ``attachments``."""
    return (nodes.filter(pl.col("attachments") != "").select("node_key", pl.col("attachments").str.split("|").alias("label")).explode("label", empty_as_null=False)
            .filter(pl.col("label").str.starts_with("S:"))
            .select(pl.col("label").str.slice(2).alias("settlement_point"), "node_key").unique())


def match_nodes(crr_nodes: pl.DataFrame, dam_nodes: pl.DataFrame, crr_branch: pl.DataFrame, dam_branch: pl.DataFrame,
                branch_matches: pl.DataFrame) -> pl.DataFrame:
    """One row per CRR node with its DAM node, plus unmatched DAM nodes.

    Methods, first that succeeds wins:

    1. ``settlement_point``: one settlement point name attached to exactly one node
       on each side (zones and hubs attach to many CRR buses and are skipped);
    2. ``branch_endpoints``: over the matched branches, count how often a CRR node
       and a DAM node sit at the same end (either orientation); accept a pair when it
       is the top vote for both nodes and either has at least two votes or both nodes
       have a single matched branch;
    3. ``unmatched``, with the number of competing candidates.
    """
    crr_keys = crr_nodes.select(pl.col("node_key").alias("crr_node_key")).unique()
    dam_keys = dam_nodes.select(pl.col("node_key").alias("dam_node_key")).unique()

    crr_sp = _settlement_points(crr_nodes).group_by("settlement_point").agg(pl.col("node_key").unique().alias("k")).filter(pl.col("k").list.len() == 1).with_columns(pl.col("k").list.first())
    dam_sp = _settlement_points(dam_nodes).group_by("settlement_point").agg(pl.col("node_key").unique().alias("k")).filter(pl.col("k").list.len() == 1).with_columns(pl.col("k").list.first())
    by_sp = (crr_sp.join(dam_sp, on="settlement_point", suffix="_dam")
             .select(pl.col("k").alias("crr_node_key"), pl.col("k_dam").alias("dam_node_key"), "settlement_point")
             .unique(subset=["crr_node_key"], keep="first").unique(subset=["dam_node_key"], keep="first")
             .with_columns(pl.lit("settlement_point").alias("match_method"), pl.lit(None, pl.UInt32).alias("n_votes"), pl.lit(1, pl.UInt32).alias("n_candidates")))

    # Endpoint votes from matched branches.
    ends = (branch_matches.filter(pl.col("match_method") != "unmatched").select("crr_branch_id", "dam_branch_id")
            .join(crr_branch.select(pl.col("branch_id").alias("crr_branch_id"), pl.col("from_node_key").alias("cf"), pl.col("to_node_key").alias("ct")), on="crr_branch_id")
            .join(dam_branch.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("from_node_key").alias("df"), pl.col("to_node_key").alias("dt")), on="dam_branch_id"))
    votes = pl.concat([ends.select(pl.col("cf").alias("crr_node_key"), pl.col("df").alias("dam_node_key")),
                       ends.select(pl.col("ct").alias("crr_node_key"), pl.col("dt").alias("dam_node_key")),
                       ends.select(pl.col("cf").alias("crr_node_key"), pl.col("dt").alias("dam_node_key")),
                       ends.select(pl.col("ct").alias("crr_node_key"), pl.col("df").alias("dam_node_key"))]).drop_nulls()
    votes = votes.group_by("crr_node_key", "dam_node_key").len().rename({"len": "n_votes"})
    taken_crr, taken_dam = by_sp["crr_node_key"].implode(), by_sp["dam_node_key"].implode()
    votes = votes.filter(~pl.col("crr_node_key").is_in(taken_crr) & ~pl.col("dam_node_key").is_in(taken_dam))
    best_crr = votes.group_by("crr_node_key").agg(pl.col("n_votes").max().alias("_max_c"), pl.len().alias("n_candidates"))
    best_dam = votes.group_by("dam_node_key").agg(pl.col("n_votes").max().alias("_max_d"))
    degree_crr = pl.concat([ends.select(pl.col("cf").alias("k")), ends.select(pl.col("ct").alias("k"))]).group_by("k").len().rename({"k": "crr_node_key", "len": "_deg_c"})
    degree_dam = pl.concat([ends.select(pl.col("df").alias("k")), ends.select(pl.col("dt").alias("k"))]).group_by("k").len().rename({"k": "dam_node_key", "len": "_deg_d"})
    scored = (votes.join(best_crr, on="crr_node_key").join(best_dam, on="dam_node_key")
              .join(degree_crr, on="crr_node_key").join(degree_dam, on="dam_node_key")
              .filter((pl.col("n_votes") == pl.col("_max_c")) & (pl.col("n_votes") == pl.col("_max_d"))
                      & ((pl.col("n_votes") >= 2) | ((pl.col("_deg_c") == 1) & (pl.col("_deg_d") == 1)))))
    # A node whose top vote ties between two partners stays unmatched.
    ties = scored.group_by("crr_node_key").len().filter(pl.col("len") > 1)["crr_node_key"]
    by_ends = (scored.filter(~pl.col("crr_node_key").is_in(ties.implode())).unique(subset=["dam_node_key"], keep="none")
               .select("crr_node_key", "dam_node_key", pl.lit(None, pl.String).alias("settlement_point"), pl.lit("branch_endpoints").alias("match_method"),
                       pl.col("n_votes").cast(pl.UInt32), pl.col("n_candidates").cast(pl.UInt32)))

    matched = pl.concat([by_sp.select(NODE_COLUMNS), by_ends.select(NODE_COLUMNS)])
    rest_crr = (crr_keys.filter(~pl.col("crr_node_key").is_in(matched["crr_node_key"].implode()))
                .join(best_crr.select("crr_node_key", "n_candidates"), on="crr_node_key", how="left")
                .select("crr_node_key", pl.lit(None, pl.String).alias("dam_node_key"), pl.lit("unmatched").alias("match_method"),
                        pl.lit(None, pl.String).alias("settlement_point"), pl.lit(None, pl.UInt32).alias("n_votes"), pl.col("n_candidates").fill_null(0).cast(pl.UInt32)))
    rest_dam = (dam_keys.filter(~pl.col("dam_node_key").is_in(matched["dam_node_key"].implode()))
                .select(pl.lit(None, pl.String).alias("crr_node_key"), "dam_node_key", pl.lit("unmatched").alias("match_method"),
                        pl.lit(None, pl.String).alias("settlement_point"), pl.lit(None, pl.UInt32).alias("n_votes"), pl.lit(0, pl.UInt32).alias("n_candidates")))
    return pl.concat([matched, rest_crr, rest_dam]).select(NODE_COLUMNS).sort("match_method", "crr_node_key", "dam_node_key")
