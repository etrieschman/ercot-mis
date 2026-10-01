"""``core.match_branch``: which CRR branch is which DAM branch.

The CRR mapping workbook gives every CRR line and transformer an ``Operations_Name``
(the Network Operations Model name). DAM names its branches with that name plus
DAM's own circuit designator (one or two characters), so a match is found in this
order, and the first method that succeeds is recorded as ``match_method``:

1. ``exact``: the names are equal once punctuation and case are ignored;
2. ``ops+ckt``: the DAM name is the operations name followed by the CRR circuit id;
3. ``prefix``: exactly one DAM name is the operations name followed by at most two
   characters;
4. ``prefix+x``: several DAM names fit, but exactly one of those not already matched
   to another CRR branch has the same reactance (see ``reactance_agrees``);
5. ``unmatched``: the CRR branch has no operations name, a placeholder, or several
   DAM candidates (``n_candidates`` says how many fit the prefix).

Every CRR branch appears once; DAM branches nothing matched appear with a null CRR
side. Differences are never smoothed over: a match records identity only.
"""

from __future__ import annotations

import re

import polars as pl

VERSION = 4

# The row of docs/assumptions.md behind every match_method (tests/test_register.py).
REGISTER_ROWS = {
    "match_branch": {"exact": "MAT-01", "ops+ckt": "MAT-01", "prefix": "MAT-02", "prefix+x": "MAT-02", "unmatched": "MAT-05"},
    "match_bus": {"settlement_point": "MAT-03", "branch_endpoints": "MAT-03", "unmatched": "MAT-05"},
    "match_contingency": {"name": "MAT-04", "members": "MAT-04", "unmatched": "MAT-05"},
}


def _registered(frame: pl.DataFrame, table: str) -> pl.DataFrame:
    """``frame`` unchanged; raises when a ``match_method`` has no register row."""
    unknown = set(frame["match_method"].unique()) - set(REGISTER_ROWS[table])
    if unknown:
        raise ValueError(f"{table} method(s) {sorted(unknown)} have no row in docs/assumptions.md")
    return frame


COLUMNS = ("crr_branch_id", "dam_branch_id", "match_method", "operations_name", "n_candidates")

# The DAM model clamps every branch reactance to at least this (per unit): CRR bus ties
# (|x| = 0.0001) and short branches below it appear in DAM at exactly the floor.
DAM_REACTANCE_FLOOR = 5e-4
REACTANCE_TOLERANCE = 0.01  # relative; measured on real packages by scripts/measure_identity.py


def reactance_agrees(crr_x: pl.Expr, dam_x: pl.Expr, tolerance: float = REACTANCE_TOLERANCE) -> pl.Expr:
    """Whether two reactances describe the same element, allowing for the DAM floor.

    Both magnitudes are raised to ``DAM_REACTANCE_FLOOR`` before the relative difference
    is taken, so a CRR tie matched to a DAM branch at the floor counts as agreeing.
    """
    a = pl.max_horizontal(crr_x.abs(), pl.lit(DAM_REACTANCE_FLOOR))
    b = pl.max_horizontal(dam_x.abs(), pl.lit(DAM_REACTANCE_FLOOR))
    return (a - b).abs() <= tolerance * pl.max_horizontal(a, b)


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

    # Several candidates: keep the ones no other CRR branch has claimed, and accept a
    # single survivor whose reactance agrees. Needs ``x_pu`` on both sides.
    if "x_pu" in crr_branch.columns and "x_pu" in dam_branch.columns:
        taken = pl.concat([exact["dam_branch_id"], by_ckt["dam_branch_id"], prefix["dam_branch_id"].drop_nulls()])
        several = (prefix.filter(pl.col("n_candidates") > 1).select("crr_branch_id", pl.col("_cands").alias("dam_branch_id")).explode("dam_branch_id")
                   .filter(~pl.col("dam_branch_id").is_in(taken.implode()))
                   .join(crr_branch.select(pl.col("branch_id").alias("crr_branch_id"), pl.col("x_pu").alias("_cx")), on="crr_branch_id")
                   .join(dam_branch.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("x_pu").alias("_dx")).unique(subset=["dam_branch_id"]), on="dam_branch_id")
                   .filter(reactance_agrees(pl.col("_cx"), pl.col("_dx")))
                   .group_by("crr_branch_id").agg(pl.col("dam_branch_id")).filter(pl.col("dam_branch_id").list.len() == 1)
                   .with_columns(pl.col("dam_branch_id").list.first()))
        several = several.unique(subset=["dam_branch_id"], keep="none")  # two CRR branches agreeing on one DAM branch stay unmatched
        prefix = (prefix.join(several.rename({"dam_branch_id": "_by_x"}), on="crr_branch_id", how="left")
                  .with_columns(pl.coalesce(pl.col("dam_branch_id"), pl.col("_by_x")).alias("dam_branch_id"),
                                pl.when(pl.col("_by_x").is_not_null()).then(pl.lit("prefix+x")).otherwise(pl.col("match_method")).alias("match_method")))

    matched = pl.concat([f.select("crr_branch_id", "dam_branch_id", "match_method", "operations_name", pl.lit(1, pl.UInt32).alias("n_candidates"))
                         for f in (exact, by_ckt)] + [prefix.select("crr_branch_id", "dam_branch_id", "match_method", "operations_name", pl.col("n_candidates").cast(pl.UInt32))])
    leftover = dam.filter(~pl.col("dam_branch_id").is_in(matched["dam_branch_id"].drop_nulls().implode())).select(
        pl.lit(None, pl.String).alias("crr_branch_id"), "dam_branch_id", pl.lit("unmatched").alias("match_method"),
        pl.lit(None, pl.String).alias("operations_name"), pl.lit(0, pl.UInt32).alias("n_candidates"))
    return _registered(pl.concat([matched, leftover]).select(COLUMNS).sort("match_method", "crr_branch_id", "dam_branch_id"), "match_branch")


# ------------------------------------------------------------------ nodes

NODE_COLUMNS = ("crr_bus_key", "dam_bus_key", "match_method", "settlement_point", "n_votes", "n_candidates")


def _settlement_points(nodes: pl.DataFrame) -> pl.DataFrame:
    """(settlement_point, bus_key) pairs from the ``S:`` labels in ``attachments``."""
    return (nodes.filter(pl.col("attachments") != "").select("bus_key", pl.col("attachments").str.split("|").alias("label")).explode("label", empty_as_null=False)
            .filter(pl.col("label").str.starts_with("S:"))
            .select(pl.col("label").str.slice(2).alias("settlement_point"), "bus_key").unique())


def match_buses(crr_nodes: pl.DataFrame, dam_nodes: pl.DataFrame, crr_branch: pl.DataFrame, dam_branch: pl.DataFrame,
                branch_matches: pl.DataFrame) -> pl.DataFrame:
    """One row per CRR node with its DAM node, plus unmatched DAM nodes.

    Methods, first that succeeds wins:

    1. ``settlement_point``: settlement point names attached to exactly one node on
       each side (zones and hubs attach to many CRR buses and are skipped) vote for a
       (CRR node, DAM node) pair; a pair is accepted when every point on either node
       agrees on it, so several resource nodes on one bus count as one match with
       ``n_votes`` points behind it;
    2. ``branch_endpoints``: over the matched branches, count how often a CRR node
       and a DAM node sit at the same end (either orientation); accept a pair when it
       is the top vote for both nodes and either has at least two votes or both nodes
       have a single matched branch;
    3. ``unmatched``, with the number of competing candidates.
    """
    crr_keys = crr_nodes.select(pl.col("bus_key").alias("crr_bus_key")).unique()
    dam_keys = dam_nodes.select(pl.col("bus_key").alias("dam_bus_key")).unique()

    crr_sp = _settlement_points(crr_nodes).group_by("settlement_point").agg(pl.col("bus_key").unique().alias("k")).filter(pl.col("k").list.len() == 1).with_columns(pl.col("k").list.first())
    dam_sp = _settlement_points(dam_nodes).group_by("settlement_point").agg(pl.col("bus_key").unique().alias("k")).filter(pl.col("k").list.len() == 1).with_columns(pl.col("k").list.first())
    sp_pairs = (crr_sp.join(dam_sp, on="settlement_point", suffix="_dam")
                .select(pl.col("k").alias("crr_bus_key"), pl.col("k_dam").alias("dam_bus_key"), "settlement_point"))
    agreed = (sp_pairs.group_by("crr_bus_key", "dam_bus_key").agg(pl.col("settlement_point").sort().first(), pl.len().alias("n_votes")))
    one_dam = agreed.group_by("crr_bus_key").len().filter(pl.col("len") == 1)["crr_bus_key"]
    one_crr = agreed.group_by("dam_bus_key").len().filter(pl.col("len") == 1)["dam_bus_key"]
    by_sp = (agreed.filter(pl.col("crr_bus_key").is_in(one_dam.implode()) & pl.col("dam_bus_key").is_in(one_crr.implode()))
             .with_columns(pl.lit("settlement_point").alias("match_method"), pl.col("n_votes").cast(pl.UInt32), pl.lit(1, pl.UInt32).alias("n_candidates")))

    # Endpoint votes from matched branches.
    ends = (branch_matches.filter(pl.col("match_method") != "unmatched").select("crr_branch_id", "dam_branch_id")
            .join(crr_branch.select(pl.col("branch_id").alias("crr_branch_id"), pl.col("from_bus_key").alias("cf"), pl.col("to_bus_key").alias("ct")), on="crr_branch_id")
            .join(dam_branch.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("from_bus_key").alias("df"), pl.col("to_bus_key").alias("dt")), on="dam_branch_id"))
    votes = pl.concat([ends.select(pl.col("cf").alias("crr_bus_key"), pl.col("df").alias("dam_bus_key")),
                       ends.select(pl.col("ct").alias("crr_bus_key"), pl.col("dt").alias("dam_bus_key")),
                       ends.select(pl.col("cf").alias("crr_bus_key"), pl.col("dt").alias("dam_bus_key")),
                       ends.select(pl.col("ct").alias("crr_bus_key"), pl.col("df").alias("dam_bus_key"))]).drop_nulls()
    votes = votes.group_by("crr_bus_key", "dam_bus_key").len().rename({"len": "n_votes"})
    taken_crr, taken_dam = by_sp["crr_bus_key"].implode(), by_sp["dam_bus_key"].implode()
    votes = votes.filter(~pl.col("crr_bus_key").is_in(taken_crr) & ~pl.col("dam_bus_key").is_in(taken_dam))
    best_crr = votes.group_by("crr_bus_key").agg(pl.col("n_votes").max().alias("_max_c"), pl.len().alias("n_candidates"))
    best_dam = votes.group_by("dam_bus_key").agg(pl.col("n_votes").max().alias("_max_d"))
    degree_crr = pl.concat([ends.select(pl.col("cf").alias("k")), ends.select(pl.col("ct").alias("k"))]).group_by("k").len().rename({"k": "crr_bus_key", "len": "_deg_c"})
    degree_dam = pl.concat([ends.select(pl.col("df").alias("k")), ends.select(pl.col("dt").alias("k"))]).group_by("k").len().rename({"k": "dam_bus_key", "len": "_deg_d"})
    scored = (votes.join(best_crr, on="crr_bus_key").join(best_dam, on="dam_bus_key")
              .join(degree_crr, on="crr_bus_key").join(degree_dam, on="dam_bus_key")
              .filter((pl.col("n_votes") == pl.col("_max_c")) & (pl.col("n_votes") == pl.col("_max_d"))
                      & ((pl.col("n_votes") >= 2) | ((pl.col("_deg_c") == 1) & (pl.col("_deg_d") == 1)))))
    # A node whose top vote ties between two partners stays unmatched.
    ties = scored.group_by("crr_bus_key").len().filter(pl.col("len") > 1)["crr_bus_key"]
    by_ends = (scored.filter(~pl.col("crr_bus_key").is_in(ties.implode())).unique(subset=["dam_bus_key"], keep="none")
               .select("crr_bus_key", "dam_bus_key", pl.lit(None, pl.String).alias("settlement_point"), pl.lit("branch_endpoints").alias("match_method"),
                       pl.col("n_votes").cast(pl.UInt32), pl.col("n_candidates").cast(pl.UInt32)))

    matched = pl.concat([by_sp.select(NODE_COLUMNS), by_ends.select(NODE_COLUMNS)])
    rest_crr = (crr_keys.filter(~pl.col("crr_bus_key").is_in(matched["crr_bus_key"].implode()))
                .join(best_crr.select("crr_bus_key", "n_candidates"), on="crr_bus_key", how="left")
                .select("crr_bus_key", pl.lit(None, pl.String).alias("dam_bus_key"), pl.lit("unmatched").alias("match_method"),
                        pl.lit(None, pl.String).alias("settlement_point"), pl.lit(None, pl.UInt32).alias("n_votes"), pl.col("n_candidates").fill_null(0).cast(pl.UInt32)))
    rest_dam = (dam_keys.filter(~pl.col("dam_bus_key").is_in(matched["dam_bus_key"].implode()))
                .select(pl.lit(None, pl.String).alias("crr_bus_key"), "dam_bus_key", pl.lit("unmatched").alias("match_method"),
                        pl.lit(None, pl.String).alias("settlement_point"), pl.lit(None, pl.UInt32).alias("n_votes"), pl.lit(0, pl.UInt32).alias("n_candidates")))
    return _registered(pl.concat([matched, rest_crr, rest_dam]).select(NODE_COLUMNS).sort("match_method", "crr_bus_key", "dam_bus_key"), "match_bus")


# ------------------------------------------------------------------ contingencies

CONTINGENCY_COLUMNS = ("crr_contingency_id", "dam_contingency_id", "match_method", "n_crr_branches", "n_dam_branches",
                       "n_shared_branches", "n_dam_other_rows", "has_split_bus", "n_candidates")


def _branch_sets(outages: pl.DataFrame, column: str) -> pl.DataFrame:
    return (outages.filter(pl.col(column).is_not_null()).group_by("contingency_id")
            .agg(pl.col(column).unique().sort().alias("branches")))


def match_contingencies(crr_outages: pl.DataFrame, dam_outages: pl.DataFrame, branch_matches: pl.DataFrame) -> pl.DataFrame:
    """One row per CRR contingency with its DAM contingency, plus unmatched DAM contingencies.

    CRR outages are translated into DAM branch ids through ``branch_matches`` before any
    comparison. Methods, first that succeeds wins: ``name`` (same name, case and
    whitespace ignored), then ``members`` (the translated CRR branch set equals exactly
    one DAM contingency's branch set), else ``unmatched``. Every matched pair records
    how many branches each side outages and how many they share, and how many DAM rows
    are loads, generators or settlement points, which CRR never lists.
    """
    translate = branch_matches.filter(pl.col("match_method") != "unmatched").select("crr_branch_id", "dam_branch_id")
    crr_dam = (crr_outages.filter(pl.col("branch_id").is_not_null())
               .join(translate, left_on="branch_id", right_on="crr_branch_id", how="left"))
    crr_sets = (crr_dam.group_by("contingency_id")
                .agg(pl.col("branch_id").n_unique().alias("n_crr_branches"),
                     pl.col("dam_branch_id").drop_nulls().unique().sort().alias("branches")))
    dam_branch_rows = dam_outages.filter(pl.col("element_kind") == "branch")
    dam_sets = (dam_outages.group_by("contingency_id")
                .agg(pl.col("branch_id").drop_nulls().unique().sort().alias("branches"),
                     (pl.col("element_kind") != "branch").sum().alias("n_dam_other_rows"),
                     (pl.col("operation") == "split_bus").any().alias("has_split_bus")))
    crr_names = crr_outages.select("contingency_id").unique().join(crr_sets, on="contingency_id", how="left").with_columns(
        pl.col("contingency_id").str.to_uppercase().str.strip_chars().alias("_name"),
        pl.col("branches").fill_null(pl.lit([], dtype=pl.List(pl.String))), pl.col("n_crr_branches").fill_null(0))
    dam_names = dam_sets.with_columns(pl.col("contingency_id").str.to_uppercase().str.strip_chars().alias("_name"))

    by_name = (crr_names.join(dam_names, on="_name", suffix="_dam")
               .unique(subset=["contingency_id"], keep="first").unique(subset=["contingency_id_dam"], keep="first")
               .with_columns(pl.lit("name").alias("match_method"), pl.lit(1, pl.UInt32).alias("n_candidates")))
    rest = crr_names.filter(~pl.col("contingency_id").is_in(by_name["contingency_id"].implode()))
    dam_rest = dam_names.filter(~pl.col("contingency_id").is_in(by_name["contingency_id_dam"].implode()))
    dam_by_set = (dam_rest.filter(pl.col("branches").list.len() > 0)
                  .with_columns(pl.col("branches").list.join("\x1f").alias("_set"))
                  .group_by("_set").agg(pl.col("contingency_id").alias("_ids"), pl.col("branches").first(),
                                        pl.col("n_dam_other_rows").first(), pl.col("has_split_bus").first()))
    by_set = (rest.filter(pl.col("branches").list.len() > 0)
              .with_columns(pl.col("branches").list.join("\x1f").alias("_set"))
              .join(dam_by_set.rename({"branches": "branches_dam"}), on="_set", how="inner")
              .with_columns(pl.col("_ids").list.len().cast(pl.UInt32).alias("n_candidates"))
              .filter(pl.col("n_candidates") == 1)
              .with_columns(pl.col("_ids").list.first().alias("contingency_id_dam"), pl.lit("members").alias("match_method")))
    by_set = by_set.unique(subset=["contingency_id_dam"], keep="none")

    def finish(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.select(
            pl.col("contingency_id").alias("crr_contingency_id"), pl.col("contingency_id_dam").alias("dam_contingency_id"),
            "match_method", pl.col("n_crr_branches").cast(pl.UInt32), pl.col("branches_dam").list.len().cast(pl.UInt32).alias("n_dam_branches"),
            pl.col("branches").list.set_intersection(pl.col("branches_dam")).list.len().cast(pl.UInt32).alias("n_shared_branches"),
            pl.col("n_dam_other_rows").cast(pl.UInt32), "has_split_bus", "n_candidates")

    matched = pl.concat([finish(by_name), finish(by_set)])
    rest = rest.filter(~pl.col("contingency_id").is_in(matched["crr_contingency_id"].implode())).select(
        pl.col("contingency_id").alias("crr_contingency_id"), pl.lit(None, pl.String).alias("dam_contingency_id"),
        pl.lit("unmatched").alias("match_method"), pl.col("n_crr_branches").cast(pl.UInt32), pl.lit(0, pl.UInt32).alias("n_dam_branches"),
        pl.lit(0, pl.UInt32).alias("n_shared_branches"), pl.lit(0, pl.UInt32).alias("n_dam_other_rows"), pl.lit(False).alias("has_split_bus"),
        pl.lit(0, pl.UInt32).alias("n_candidates"))
    dam_rest = dam_names.filter(~pl.col("contingency_id").is_in(matched["dam_contingency_id"].implode())).select(
        pl.lit(None, pl.String).alias("crr_contingency_id"), pl.col("contingency_id").alias("dam_contingency_id"),
        pl.lit("unmatched").alias("match_method"), pl.lit(0, pl.UInt32).alias("n_crr_branches"), pl.col("branches").list.len().cast(pl.UInt32).alias("n_dam_branches"),
        pl.lit(0, pl.UInt32).alias("n_shared_branches"), pl.col("n_dam_other_rows").cast(pl.UInt32), "has_split_bus", pl.lit(0, pl.UInt32).alias("n_candidates"))
    return _registered(pl.concat([matched, rest, dam_rest]).select(CONTINGENCY_COLUMNS).sort("match_method", "crr_contingency_id", "dam_contingency_id"), "match_contingency")
