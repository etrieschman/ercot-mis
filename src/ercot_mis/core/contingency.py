"""``core.contingency`` and ``core.contingency_outage``: what each contingency removes.

One ``core.contingency`` row per (snapshot, name) and one ``core.contingency_outage``
row per element it acts on, in the model's own vocabulary resolved to core keys:

- CRR rows name a LINE or XFMR by the same name ``core.branch`` uses for
  ``branch_id`` (RAW comment, ``Autos`` name), with one action code.
- DAM rows are Branch (resolved to ``branch_id`` by (from, to, ckt) in the same
  hour), Load, Generator and SettlementPoint (resolved to a ``node_key`` by bus
  number), and split-bus operations, which change topology rather than remove an
  element and are kept as their own ``operation``.

Unresolved references (a name or key with no branch or node in the snapshot) keep
null keys and set ``is_resolved`` false; nothing is dropped.
"""

from __future__ import annotations

import polars as pl

VERSION = 1

CONTINGENCY_COLUMNS = ("contingency_id", "n_outages", "n_unresolved", "has_split_bus")
OUTAGE_COLUMNS = ("contingency_id", "element_kind", "operation", "action", "branch_id", "node_key",
                  "psse_bus", "psse_id", "element_name", "is_resolved")

_CRR_KINDS = {"LINE": "line", "XFMR": "transformer", "TRANSFORMER": "transformer"}
_DAM_KINDS = {"BRANCH": "branch", "LOAD": "load", "GENERATOR": "generator", "SETTLEMENTPOINT": "settlement_point"}


def _summarize(outages: pl.DataFrame) -> pl.DataFrame:
    return (outages.group_by("contingency_id").agg(
        pl.len().alias("n_outages"), (~pl.col("is_resolved")).sum().alias("n_unresolved"),
        (pl.col("operation") == "split_bus").any().alias("has_split_bus"))
        .select(CONTINGENCY_COLUMNS).sort("contingency_id"))


def crr_contingencies(branches: pl.DataFrame, raw: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From ``crr_contingencies`` (Contingency, DeviceName, DeviceType, Action)."""
    known = branches.select("branch_id", pl.lit(True).alias("is_resolved"))
    outages = (raw.select(pl.col("contingency").alias("contingency_id"),
                          pl.col("device_type").str.to_uppercase().replace_strict(_CRR_KINDS, default="unknown").alias("element_kind"),
                          pl.lit("outage").alias("operation"), pl.col("action"),
                          pl.col("device_name").alias("branch_id"), pl.lit(None, pl.String).alias("node_key"),
                          pl.lit(None, pl.Int64).alias("psse_bus"), pl.lit(None, pl.String).alias("psse_id"),
                          pl.col("device_name").alias("element_name"))
               .join(known, on="branch_id", how="left")
               .with_columns(pl.col("is_resolved").fill_null(False))
               .with_columns(pl.when(pl.col("is_resolved")).then(pl.col("branch_id")).otherwise(None).alias("branch_id"))
               .select(OUTAGE_COLUMNS))
    return _summarize(outages), outages


def dam_contingencies(branches: pl.DataFrame, nodes: pl.DataFrame, raw: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """From one hour's ``dam_contingencies`` CSV."""
    keyed = branches.select(pl.col("from_bus").alias("psse_from_bus_number"), pl.col("to_bus").alias("psse_to_bus_number"),
                            pl.col("ckt").alias("_ckt"), "branch_id")
    keyed = pl.concat([keyed, keyed.select(pl.col("psse_to_bus_number").alias("psse_from_bus_number"),
                                          pl.col("psse_from_bus_number").alias("psse_to_bus_number"), "_ckt", "branch_id")]).unique()
    node_keys = nodes.select(pl.col("psse_bus_number").alias("psse_bus"), "node_key")
    frame = (raw.with_columns(pl.col("equipment_type").str.to_uppercase().str.replace_all(r"[^A-Z]", "").replace_strict(_DAM_KINDS, default="unknown").alias("element_kind"),
                              pl.when(pl.col("contingency_operation").str.to_uppercase().str.contains("SPLIT")).then(pl.lit("split_bus")).otherwise(pl.lit("outage")).alias("operation"),
                              pl.col("psse_ckt_id").str.strip_chars().alias("_ckt"),
                              pl.col("psse_gen_or_load_or_sp_bus_number").alias("psse_bus"),
                              pl.col("psse_gen_or_load_id").str.strip_chars().alias("psse_id"))
             .join(keyed, on=["psse_from_bus_number", "psse_to_bus_number", "_ckt"], how="left")
             .join(node_keys, on="psse_bus", how="left"))
    outages = frame.select(
        pl.col("contingency_name").alias("contingency_id"), "element_kind", "operation", pl.lit(None, pl.String).alias("action"),
        pl.when(pl.col("element_kind") == "branch").then(pl.col("branch_id")).otherwise(None).alias("branch_id"),
        pl.when(pl.col("element_kind") != "branch").then(pl.col("node_key")).otherwise(None).alias("node_key"),
        "psse_bus", "psse_id",
        pl.when(pl.col("element_kind") == "branch").then(pl.col("branch_id"))
          .otherwise(pl.coalesce(pl.col("station_name_psse_bus_name"), pl.col("psse_id"))).alias("element_name"),
        pl.when(pl.col("element_kind") == "branch").then(pl.col("branch_id").is_not_null())
          .otherwise(pl.col("node_key").is_not_null()).alias("is_resolved"),
    ).select(OUTAGE_COLUMNS)
    return _summarize(outages), outages
