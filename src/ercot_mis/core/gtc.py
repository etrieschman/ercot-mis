"""``core.gtc`` and ``core.gtc_member``: generic transmission constraints per snapshot.

A GTC is a base-case constraint on a factor-weighted sum of member branch flows with a
limit in MW. CRR packages ship them in the Non-Thermal Constraints CSV, one row per
member; members are named like ``core.branch`` names them (``source = "crr_csv"``).
DAM packages carry none: the hourly day-ahead limits come from the GTL workbook
(NP3-766-M, ``source = "gtl_dam"``) under human-readable names, and member definitions
from NP3-770-M are not parsed. A manual crosswalk in ``data/overrides/gtc_names.csv``
(``gtl_name,crr_gtc_id``) links a DAM row to the CRR GTC of the same constraint, so
``out.network`` can borrow the CRR member set; DAM ``core.gtc_member`` stays empty.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

VERSION = 2

GTC_COLUMNS = ("gtc_id", "source", "limit_mw", "n_members", "n_unresolved", "crr_gtc_id")
OVERRIDES = Path("overrides") / "gtc_names.csv"  # relative to the data folder
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
            .with_columns(pl.lit("crr_csv").alias("source"), pl.col("gtc_id").alias("crr_gtc_id"))
            .select(GTC_COLUMNS).sort("gtc_id"))
    return gtcs, members.select(MEMBER_COLUMNS)


def name_crosswalk(data_dir: Path) -> pl.DataFrame:
    """The manual GTL-name to CRR-GTC crosswalk, or an empty frame when none is kept."""
    path = data_dir / OVERRIDES
    schema = {"gtl_name": pl.String, "crr_gtc_id": pl.String}
    if not path.is_file():
        return pl.DataFrame(schema=schema)
    return pl.read_csv(path, comment_prefix="#", schema=schema).with_columns(pl.all().str.strip_chars())


def dam_gtcs(gtl_hourly: pl.DataFrame, crosswalk: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """DAM GTC rows for one hour from the GTL workbook's day-ahead limits; members stay empty."""
    rows = gtl_hourly.filter(pl.col("market") == "dam")
    if rows.is_empty():
        return no_gtcs()
    gtcs = (rows.select(pl.col("gtc_name").alias("gtc_id"), pl.col("limit_mw"))
            .unique(subset=["gtc_id"], keep="last")
            .join(crosswalk.rename({"gtl_name": "gtc_id"}), on="gtc_id", how="left")
            .with_columns(pl.lit("gtl_dam").alias("source"), pl.lit(0, pl.UInt32).alias("n_members"), pl.lit(0, pl.UInt32).alias("n_unresolved"))
            .select(GTC_COLUMNS).sort("gtc_id"))
    return gtcs, no_gtcs()[1]


def no_gtcs() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Empty tables with the right schema, for models that ship no GTC file."""
    gtcs = pl.DataFrame(schema={"gtc_id": pl.String, "source": pl.String, "limit_mw": pl.Float64, "n_members": pl.UInt32,
                                "n_unresolved": pl.UInt32, "crr_gtc_id": pl.String})
    members = pl.DataFrame(schema={"gtc_id": pl.String, "branch_id": pl.String, "factor": pl.Float64, "flow_direction": pl.String,
                                   "element_name": pl.String, "is_resolved": pl.Boolean})
    return gtcs, members
