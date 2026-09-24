"""``core.diff_branch``: how matched CRR and DAM branches differ.

``core.match_*`` records identity; this module records the differences between the
two descriptions of one element, side by side and never smoothed over. One row per
matched pair (``match_method`` carried along) with, for each side, the kind, the
reactance, the voltage levels at the ends, the in-service flag and the ratings the
model enforces, plus ``same_*`` verdicts:

- ``same_reactance``: both magnitudes raised to the DAM floor and compared within the
  matcher's tolerance (``core.match.reactance_agrees``);
- ``same_kv``: the endpoints' voltage levels with the tenths digit dropped (both RAWs
  use it to tell buses of one station apart);
- ``same_kind``: line versus transformer.

CRR ratings are the monitored-element CSV block for one time of use (null where the
CRR does not monitor the branch); DAM ratings are the RAW rate A and B.
"""

from __future__ import annotations

import polars as pl

from .match import reactance_agrees

VERSION = 1

COLUMNS = ("crr_branch_id", "dam_branch_id", "match_method",
           "crr_kind", "dam_kind", "same_kind",
           "crr_x_pu", "dam_x_pu", "same_reactance",
           "crr_kv_lo", "crr_kv_hi", "dam_kv_lo", "dam_kv_hi", "same_kv",
           "crr_in_service", "dam_in_service",
           "crr_enforced", "dam_enforced", "crr_base_mw", "dam_base_mw", "crr_emergency_mw", "dam_emergency_mw")


def _side(branch: pl.DataFrame, nodes: pl.DataFrame, ratings: pl.DataFrame, prefix: str, enforced: str,
          rating_source: str, time_of_use: str | None) -> pl.DataFrame:
    kv = nodes.select("node_key", pl.col("kv").floor().alias("_kv")).unique(subset=["node_key"])
    rows = ratings.filter(pl.col("rating_source") == rating_source)
    if time_of_use is not None:
        rows = rows.filter(pl.col("time_of_use") == time_of_use)
    rows = rows.select("branch_id", "base_mw", "emergency_mw").unique(subset=["branch_id"], keep="first")
    frame = (branch.join(kv.rename({"node_key": "from_node_key", "_kv": "_f"}), on="from_node_key", how="left")
             .join(kv.rename({"node_key": "to_node_key", "_kv": "_t"}), on="to_node_key", how="left")
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
