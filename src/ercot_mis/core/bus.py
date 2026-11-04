"""``core.bus`` and ``core.node``: the points of each model, keyed by ``bus_key``.

A node is a point of a node-breaker model: one RAW record of the CRR model. A bus is a point
of a bus-branch model: one RAW record of the DAM model, or the CRR nodes that in-service ties
(breakers and switches at PSS/E's minimum reactance) join into one electrical point.
``core.node`` holds the CRR nodes; ``core.bus`` holds both models' buses.

``bus_key`` is derived from the RAW name, the voltage and the sorted names of the attached
equipment, because PSS/E numbers change between DAM hours and RAW names are substation names
shared by many buses (TOP-02, TOP-07, TOP-09, TOP-10). Details: docs/datasets/identity-and-matching.md.
"""

from __future__ import annotations

import hashlib
import re

import polars as pl

VERSION = 3  # bump when keys, columns or the contraction rule change; every core.bus and core.node artifact is rebuilt
TIE_REACTANCE = 1e-4  # |x| at or below this is a bus tie (breaker, switch, jumper)

# ``core.bus``: one row per bus. ``bus_number`` and ``bus_type`` are PSS/E's number and type code of a DAM
# bus (null for CRR, whose numbers belong to nodes); ``n_nodes`` is how many CRR nodes the bus joins (null
# for DAM); ``substation`` is the RAW name where that name is a substation (DAM) and null for CRR; ``raw_name``
# is the RAW name, or the joined names of a CRR bus's nodes.
BUS_COLUMNS = ("bus_key", "substation", "kv", "bus_type", "n_nodes", "bus_number", "raw_name", "attachments", "n_attachments", "is_ambiguous")
# ``core.node``: one row per CRR node (one RAW record) with the bus it belongs to.
NODE_COLUMNS = ("node_number", "raw_name", "kv", "node_type", "bus_key", "is_tie_member")
NODE_SCHEMA = {"node_number": pl.Int64, "raw_name": pl.String, "kv": pl.Float64, "node_type": pl.Int64, "bus_key": pl.String, "is_tie_member": pl.Boolean}

# Prefixes keep equipment kinds apart inside a key: a generator and a load with the
# same name must not collapse into one attachment.
_KIND = {"branch": "B", "generator": "G", "load": "D", "settlement_point": "S", "source_sink": "S"}


def _norm(value: str | None) -> str | None:
    return re.sub(r"\s+", " ", value.strip().upper()) if value is not None else None


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _attachments(pairs: list[tuple[pl.DataFrame, str, str, str]]) -> pl.DataFrame:
    """Long table (number, label) from (frame, number column, name column, kind) quadruples."""
    parts = [
        frame.select(pl.col(number).cast(pl.Int64).alias("number"),
                     (pl.lit(_KIND[kind] + ":") + pl.col(name).cast(pl.String).str.strip_chars().str.to_uppercase()).alias("label"))
        .drop_nulls()
        for frame, number, name, kind in pairs
    ]
    return pl.concat(parts).unique()


def _with_bus_keys(buses: pl.DataFrame, attachments: pl.DataFrame, number: str) -> pl.DataFrame:
    """Add ``attachments``, ``n_attachments``, ``bus_key`` and ``is_ambiguous`` to ``buses``, joined to ``attachments`` on ``number``."""
    joined = (attachments.group_by("number").agg(pl.col("label").sort().str.join("|").alias("attachments"),
                                                 pl.len().alias("n_attachments")))
    buses = buses.join(joined, left_on=number, right_on="number", how="left").with_columns(
        pl.col("attachments").fill_null(""), pl.col("n_attachments").fill_null(0))
    text = (pl.col("raw_name").fill_null("") + "|" + pl.col("kv").cast(pl.String) + "|" + pl.col("attachments"))
    buses = buses.with_columns(text.map_elements(_digest, return_dtype=pl.String).alias("bus_key"))
    counts = buses.group_by("bus_key").len().rename({"len": "_n"})
    buses = buses.join(counts, on="bus_key").with_columns((pl.col("_n") > 1).alias("is_ambiguous")).drop("_n")
    # Buses with the same key (usually isolated buses at one substation) get an ordinal so keys stay unique (TOP-07).
    return buses.with_columns(
        pl.when(pl.col("is_ambiguous"))
        .then(pl.col("bus_key") + "#" + pl.col(number).rank("ordinal").over("bus_key").cast(pl.String))
        .otherwise(pl.col("bus_key")).alias("bus_key")
    )


def dam_buses(bus: pl.DataFrame, lines: pl.DataFrame, transformers: pl.DataFrame, generators: pl.DataFrame,
              loads: pl.DataFrame, settlement_points: pl.DataFrame) -> pl.DataFrame:
    """``core.bus`` for one DAM hour: one row per ``psse_bus`` record, with its ``bus_key`` from the hour's ``dam_lines``, ``dam_transformers``, ``dam_generators``, ``dam_loads`` and ``dam_settlement_points``."""
    branches = pl.concat([lines.select("psse_from_bus_number", "psse_to_bus_number", "branch_name"),
                          transformers.select("psse_from_bus_number", "psse_to_bus_number", "branch_name")])
    attachments = _attachments([
        (branches, "psse_from_bus_number", "branch_name", "branch"),
        (branches, "psse_to_bus_number", "branch_name", "branch"),
        (generators, "psse_bus_number", "generator_name", "generator"),
        (loads, "psse_bus_number", "load_name", "load"),
        (settlement_points, "psse_bus_number", "settlement_point_name", "settlement_point"),
    ])
    buses = bus.select(pl.col("i").alias("bus_number"), pl.col("name").map_elements(_norm, return_dtype=pl.String).alias("raw_name"),
                       pl.col("basekv").alias("kv"), pl.col("ide").alias("bus_type"))
    return (_with_bus_keys(buses, attachments, "bus_number")
            .with_columns(pl.col("raw_name").alias("substation"), pl.lit(None, pl.Int64).alias("n_nodes"))
            .select(BUS_COLUMNS).sort("bus_number"))


def components(nodes, edges) -> dict:
    """Union-find: each of ``nodes`` mapped to the smallest member of its component under ``edges`` (pairs of nodes)."""
    parent = {n: n for n in nodes}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {n: find(n) for n in parent}


def tie_groups(branch: pl.DataFrame) -> pl.DataFrame:
    """(node_number, tie_group) for every node on an in-service branch with |x| <= TIE_REACTANCE; the group is its smallest node number."""
    ties = branch.filter((pl.col("x").abs() <= TIE_REACTANCE) & (pl.col("st") == 1)).select("i", "j").rows()
    groups = components({n for pair in ties for n in pair}, ties)
    members = sorted(groups)
    return pl.DataFrame({"node_number": members, "tie_group": [groups[n] for n in members]}, schema={"node_number": pl.Int64, "tie_group": pl.Int64})


def autos_by_key(autos: pl.DataFrame) -> pl.DataFrame:
    """``Autos`` names keyed by (from_node, to_node, ckt) in both orientations, ``is_swapped`` saying which; the sheet lists some transformers with from and to swapped relative to the RAW (NAM-02)."""
    direct = autos.select(pl.col("from_number").cast(pl.Int64, strict=False).alias("from_node"),
                          pl.col("to_number").cast(pl.Int64, strict=False).alias("to_node"),
                          pl.col("id").str.strip_chars().alias("ckt"), pl.col("crr_name").alias("name"),
                          pl.lit(False).alias("is_swapped")).drop_nulls(["from_node", "to_node"])
    swapped = direct.select(pl.col("to_node").alias("from_node"), pl.col("from_node").alias("to_node"), "ckt", "name", pl.lit(True).alias("is_swapped"))
    return pl.concat([direct, swapped]).unique(subset=["from_node", "to_node", "ckt"], keep="first")


def crr_buses(bus: pl.DataFrame, branch: pl.DataFrame, transformer: pl.DataFrame, autos: pl.DataFrame,
              sources_sinks: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """``core.node`` and ``core.bus`` for one CRR model: every ``psse_bus`` record as a node, and the buses in-service ties join them into.

    Lines are named by their RAW comment, transformers by the ``Autos`` sheet on (from, to, ckt), settlement
    points by the sources and sinks file. A bus's ``raw_name`` joins its nodes' names, its ``kv`` is their
    lowest, and its ``bus_type`` is 3 when a node is the swing, 4 when every node is isolated, else the highest
    member type.
    """
    groups = tie_groups(branch)
    nodes = (bus.select(pl.col("i").alias("node_number"), pl.col("name").map_elements(_norm, return_dtype=pl.String).alias("raw_name"),
                        pl.col("basekv").alias("kv"), pl.col("ide").alias("node_type"))
             .join(groups.rename({"tie_group": "_group"}), on="node_number", how="left")
             .with_columns(pl.col("_group").fill_null(pl.col("node_number")), pl.col("node_number").is_in(groups["node_number"].implode()).alias("is_tie_member")))
    to_group = nodes.select("node_number", "_group")

    lines = branch.filter(pl.col("x").abs() > TIE_REACTANCE).select("i", "j", pl.col("comment").alias("name"))
    xf = (transformer.select("i", "j", pl.col("ckt").str.strip_chars().alias("ckt"))
          .join(autos_by_key(autos).rename({"from_node": "i", "to_node": "j"}), on=["i", "j", "ckt"], how="left"))
    xf = xf.with_columns(pl.coalesce(pl.col("name"), pl.format("XF {} {} {}", "i", "j", "ckt")).alias("name"))
    sp = sources_sinks.select(pl.col("bus_name").str.extract(r"^\s*(\d+)").cast(pl.Int64).alias("number"), pl.col("name"))
    both = pl.concat([lines, xf.select("i", "j", "name")])
    attachments = (_attachments([(both, "i", "name", "branch"), (both, "j", "name", "branch"), (sp, "number", "name", "source_sink")])
                   .join(to_group, left_on="number", right_on="node_number", how="inner").select(pl.col("_group").alias("number"), "label"))

    # A bus's key text joins its nodes' names, so one bus has one key.
    grouped = nodes.group_by("_group").agg(
        pl.col("raw_name").sort().str.join("+").alias("raw_name"), pl.col("kv").min().alias("kv"), pl.len().cast(pl.Int64).alias("n_nodes"),
        pl.when((pl.col("node_type") == 3).any()).then(3).when((pl.col("node_type") == 4).all()).then(4)
        .otherwise(pl.col("node_type").filter(pl.col("node_type") != 4).max()).alias("bus_type"))
    buses = (_with_bus_keys(grouped, attachments, "_group")
             .with_columns(pl.lit(None, pl.String).alias("substation"), pl.lit(None, pl.Int64).alias("bus_number")))
    nodes = nodes.join(buses.select("_group", "bus_key"), on="_group", how="left").select(NODE_COLUMNS).sort("node_number")
    return nodes, buses.select(BUS_COLUMNS).sort("bus_key")
