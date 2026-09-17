"""``core.gtc`` and ``core.gtc_member``: generic transmission constraints per snapshot.

A GTC is a base-case constraint on a factor-weighted sum of member branch flows with a
limit in MW. CRR packages ship them in the Non-Thermal Constraints CSV, one row per
member; members are named like ``core.branch`` names them. DAM packages carry none:
definitions come from NP3-770-M and daily limits from NP3-766-M, not parsed yet, so
DAM snapshots have no GTC rows.
"""

from __future__ import annotations

import polars as pl

VERSION = 1

GTC_COLUMNS = ("gtc_id", "limit_mw", "n_members", "n_unresolved")
MEMBER_COLUMNS = ("gtc_id", "branch_id", "factor", "flow_direction", "element_name", "is_resolved")


def crr_gtcs(branches: pl.DataFrame, raw: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From ``crr_non_thermal_constraints`` (Name, Limit, DeviceName, DeviceType, FlowDirection, Factor)."""
    known = branches.select("branch_id", pl.lit(True).alias("is_resolved"))
    members = (raw.select(pl.col("name").alias("gtc_id"), pl.col("device_name").alias("branch_id"), "factor",
                          "flow_direction", pl.col("device_name").alias("element_name"), pl.col("limit"))
               .join(known, on="branch_id", how="left")
               .with_columns(pl.col("is_resolved").fill_null(False))
               .with_columns(pl.when(pl.col("is_resolved")).then(pl.col("branch_id")).otherwise(None).alias("branch_id")))
    gtcs = (members.group_by("gtc_id").agg(pl.col("limit").first().alias("limit_mw"), pl.len().alias("n_members"),
                                           (~pl.col("is_resolved")).sum().alias("n_unresolved"))
            .select(GTC_COLUMNS).sort("gtc_id"))
    return gtcs, members.select(MEMBER_COLUMNS)


def no_gtcs() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Empty tables with the right schema, for models that ship no GTC file."""
    gtcs = pl.DataFrame(schema={"gtc_id": pl.String, "limit_mw": pl.Float64, "n_members": pl.UInt32, "n_unresolved": pl.UInt32})
    members = pl.DataFrame(schema={"gtc_id": pl.String, "branch_id": pl.String, "factor": pl.Float64, "flow_direction": pl.String,
                                   "element_name": pl.String, "is_resolved": pl.Boolean})
    return gtcs, members
