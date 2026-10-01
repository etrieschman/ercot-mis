"""``core.diff_branch``, ``core.diff_settlement_point`` and ``core.diff_load``: how the two models' descriptions
of one element differ.

``core.match_*`` records identity; this module records the differences between the
two descriptions of one element, side by side and never smoothed over. One row per
matched pair (``match_method`` carried along) with, for each side, the kind, the
reactance, the voltage levels at the ends, the in-service flag and the ratings the
model enforces, plus ``same_*`` verdicts:

- ``same_reactance``: both magnitudes raised to the DAM floor and compared within the
  matcher's tolerance (``core.match.reactance_agrees``);
- ``same_kv``: the endpoints' voltage levels with the tenths digit dropped (both RAWs
  use it to tell buses of one substation apart);
- ``same_kind``: line versus transformer.

CRR ratings are the monitored-element CSV block for one time of use (null where the
CRR does not monitor the branch); DAM ratings are the RAW rate A and B.

``diff_settlement_point`` compares, per settlement point name (identical in both
models), the node sets the two models put it on: the CRR nodes are translated to DAM
bus keys through ``core.match_bus`` and compared with the DAM nodes, with the
weight they share. ``same_buses`` is true when the translated CRR set equals the DAM
set; ``n_crr_buses_unmatched`` says how much of the CRR side could not be translated.
"""

from __future__ import annotations

import polars as pl

from .match import reactance_agrees

VERSION = 2

COLUMNS = ("crr_branch_id", "dam_branch_id", "match_method",
           "crr_kind", "dam_kind", "same_kind",
           "crr_x_pu", "dam_x_pu", "same_reactance",
           "crr_kv_lo", "crr_kv_hi", "dam_kv_lo", "dam_kv_hi", "same_kv",
           "crr_in_service", "dam_in_service",
           "crr_enforced", "dam_enforced", "crr_base_mw", "dam_base_mw", "crr_emergency_mw", "dam_emergency_mw")


def _side(branch: pl.DataFrame, nodes: pl.DataFrame, ratings: pl.DataFrame, prefix: str, enforced: str,
          rating_source: str, time_of_use: str | None) -> pl.DataFrame:
    kv = nodes.select("bus_key", pl.col("kv").floor().alias("_kv")).unique(subset=["bus_key"])
    rows = ratings.filter(pl.col("rating_source") == rating_source)
    if time_of_use is not None:
        rows = rows.filter(pl.col("time_of_use") == time_of_use)
    rows = rows.select("branch_id", "base_mw", "emergency_mw").unique(subset=["branch_id"], keep="first")
    frame = (branch.join(kv.rename({"bus_key": "from_bus_key", "_kv": "_f"}), on="from_bus_key", how="left")
             .join(kv.rename({"bus_key": "to_bus_key", "_kv": "_t"}), on="to_bus_key", how="left")
             .join(rows, on="branch_id", how="left"))
    return frame.select(pl.col("branch_id").alias(f"{prefix}_branch_id"), pl.col("kind").alias(f"{prefix}_kind"),
                        pl.col("x_pu").alias(f"{prefix}_x_pu"),
                        pl.min_horizontal("_f", "_t").alias(f"{prefix}_kv_lo"), pl.max_horizontal("_f", "_t").alias(f"{prefix}_kv_hi"),
                        pl.col("is_in_service").alias(f"{prefix}_in_service"), pl.col(enforced).fill_null(False).alias(f"{prefix}_enforced"),
                        pl.col("base_mw").alias(f"{prefix}_base_mw"), pl.col("emergency_mw").alias(f"{prefix}_emergency_mw"))


def diff_branches(matches: pl.DataFrame, crr_branch: pl.DataFrame, crr_nodes: pl.DataFrame, crr_ratings: pl.DataFrame,
                  dam_branch: pl.DataFrame, dam_nodes: pl.DataFrame, dam_ratings: pl.DataFrame,
                  time_of_use: str = "PeakWD") -> pl.DataFrame:
    """One row per matched pair in ``matches`` (``core.match_branch``), differences side by side."""
    pairs = matches.filter(pl.col("match_method") != "unmatched").select("crr_branch_id", "dam_branch_id", "match_method")
    crr = _side(crr_branch, crr_nodes, crr_ratings, "crr", "is_monitored", "crr_monitored", time_of_use)
    dam = _side(dam_branch, dam_nodes, dam_ratings, "dam", "is_secured", "psse_raw", None)
    return (pairs.join(crr, on="crr_branch_id", how="left").join(dam, on="dam_branch_id", how="left")
            .with_columns((pl.col("crr_kind") == pl.col("dam_kind")).alias("same_kind"),
                          reactance_agrees(pl.col("crr_x_pu"), pl.col("dam_x_pu")).alias("same_reactance"),
                          ((pl.col("crr_kv_lo") == pl.col("dam_kv_lo")) & (pl.col("crr_kv_hi") == pl.col("dam_kv_hi"))).alias("same_kv"))
            .select(COLUMNS).sort("crr_branch_id", "dam_branch_id"))


SP_COLUMNS = ("settlement_point_id", "crr_kind", "dam_kind", "same_kind", "n_crr_buses", "n_dam_buses", "n_crr_buses_unmatched",
              "n_shared_buses", "shared_weight_crr", "shared_weight_dam", "same_buses")


def diff_settlement_points(crr_points: pl.DataFrame, crr_point_nodes: pl.DataFrame, dam_points: pl.DataFrame,
                           dam_point_nodes: pl.DataFrame, node_matches: pl.DataFrame) -> pl.DataFrame:
    """One row per settlement point present in both snapshots (``core.settlement_point[_node]`` rows)."""
    translate = node_matches.filter(pl.col("match_method") != "unmatched").select(pl.col("crr_bus_key").alias("bus_key"), pl.col("dam_bus_key").alias("_dam"))
    crr = (crr_point_nodes.filter(pl.col("is_resolved")).join(translate, on="bus_key", how="left")
           .select("settlement_point_id", pl.col("_dam").alias("bus_key"), pl.col("weight").alias("_w_crr")))
    dam = dam_point_nodes.filter(pl.col("is_resolved")).select("settlement_point_id", "bus_key", pl.col("weight").alias("_w_dam"))
    both = (crr_points.select("settlement_point_id", pl.col("kind").alias("crr_kind"))
            .join(dam_points.select("settlement_point_id", pl.col("kind").alias("dam_kind")), on="settlement_point_id", how="inner"))
    crr_counts = crr.group_by("settlement_point_id").agg(pl.len().cast(pl.UInt32).alias("n_crr_buses"), pl.col("bus_key").is_null().sum().cast(pl.UInt32).alias("n_crr_buses_unmatched"))
    dam_counts = dam.group_by("settlement_point_id").agg(pl.len().cast(pl.UInt32).alias("n_dam_buses"))
    shared = (crr.filter(pl.col("bus_key").is_not_null()).join(dam, on=["settlement_point_id", "bus_key"], how="inner")
              .group_by("settlement_point_id").agg(pl.len().cast(pl.UInt32).alias("n_shared_buses"), pl.col("_w_crr").sum().alias("shared_weight_crr"),
                                                   pl.col("_w_dam").sum().alias("shared_weight_dam")))
    return (both.join(crr_counts, on="settlement_point_id", how="left").join(dam_counts, on="settlement_point_id", how="left")
            .join(shared, on="settlement_point_id", how="left")
            .with_columns(pl.col("n_crr_buses").fill_null(0), pl.col("n_dam_buses").fill_null(0), pl.col("n_crr_buses_unmatched").fill_null(0),
                          pl.col("n_shared_buses").fill_null(0), pl.col("shared_weight_crr").fill_null(0.0), pl.col("shared_weight_dam").fill_null(0.0))
            .with_columns((pl.col("crr_kind") == pl.col("dam_kind")).alias("same_kind"),
                          ((pl.col("n_shared_buses") == pl.col("n_crr_buses")) & (pl.col("n_shared_buses") == pl.col("n_dam_buses")) & (pl.col("n_shared_buses") > 0)).alias("same_buses"))
            .select(SP_COLUMNS).sort("settlement_point_id"))


LOAD_COLUMNS = ("crr_bus_key", "dam_bus_key", "match_method", "n_crr_loads", "n_crr_in_service", "crr_mw_in_service", "crr_mw_out_of_service",
                "n_dam_loads", "n_dam_in_service", "dam_mw_in_service", "dam_mw_ldf_in_service", "dam_mw_ldf_out_of_service",
                "n_dam_rollover_capable", "same_n_loads", "same_n_in_service")


def diff_loads(crr_loads: pl.DataFrame, dam_loads: pl.DataFrame, node_matches: pl.DataFrame) -> pl.DataFrame:
    """One row per node that carries a load in either model (``core.load`` rows), the two sides next to each other.

    The models share no load name (the CRR RAW names only the bus), so loads are compared
    where they sit: per pair of matched nodes, how many loads each model has there, how
    many are in service and their MW; for the DAM also the distribution factor in and out
    of service and how many loads can roll over. A node with loads that has no match keeps
    its row with the other side null. Nothing is reconciled: a load in service on one
    side only is what this table is for.
    """
    crr = crr_loads.group_by(pl.col("bus_key").alias("crr_bus_key")).agg(
        pl.len().cast(pl.UInt32).alias("n_crr_loads"), pl.col("is_in_service").sum().cast(pl.UInt32).alias("n_crr_in_service"),
        pl.col("mw").filter(pl.col("is_in_service")).sum().alias("crr_mw_in_service"), pl.col("mw").filter(~pl.col("is_in_service")).sum().alias("crr_mw_out_of_service"))
    dam = dam_loads.group_by(pl.col("bus_key").alias("dam_bus_key")).agg(
        pl.len().cast(pl.UInt32).alias("n_dam_loads"), pl.col("is_in_service").sum().cast(pl.UInt32).alias("n_dam_in_service"),
        pl.col("mw").filter(pl.col("is_in_service")).sum().alias("dam_mw_in_service"),
        pl.col("mw_ldf").filter(pl.col("is_in_service")).sum().alias("dam_mw_ldf_in_service"), pl.col("mw_ldf").filter(~pl.col("is_in_service")).sum().alias("dam_mw_ldf_out_of_service"),
        pl.col("is_rollover_capable").sum().cast(pl.UInt32).alias("n_dam_rollover_capable"))
    pairs = node_matches.select("crr_bus_key", "dam_bus_key", "match_method")
    matched = pairs.filter(pl.col("match_method") != "unmatched")
    left = crr.join(matched, on="crr_bus_key", how="left").with_columns(pl.col("match_method").fill_null("unmatched"))
    both = left.filter(pl.col("dam_bus_key").is_not_null()).join(dam, on="dam_bus_key", how="left")
    crr_only = left.filter(pl.col("dam_bus_key").is_null())
    seen = both["dam_bus_key"].implode()
    dam_only = (dam.filter(~pl.col("dam_bus_key").is_in(seen)).join(matched, on="dam_bus_key", how="left")
                .with_columns(pl.col("match_method").fill_null("unmatched")))
    zero = [pl.col(c).fill_null(0) for c in ("n_crr_loads", "n_crr_in_service", "n_dam_loads", "n_dam_in_service")]
    return (pl.concat([both, crr_only, dam_only], how="diagonal_relaxed").with_columns(zero)
            .with_columns((pl.col("n_crr_loads") == pl.col("n_dam_loads")).alias("same_n_loads"),
                          (pl.col("n_crr_in_service") == pl.col("n_dam_in_service")).alias("same_n_in_service"))
            .select(LOAD_COLUMNS).sort("crr_bus_key", "dam_bus_key", nulls_last=True))
