"""``core.node``: every node of a model, and the stable key of the bus it belongs to.

Vocabulary (docs/datasets/identity-and-matching.md): a **node** is one RAW record, the
finest connection point the model gives (PSS/E calls it a bus, and ``node_number``
keeps the file's word); a **bus** is the nodes joined by closed breakers.

DAM hourly models renumber every node, and their RAW names are substation names
shared by up to sixteen nodes. CRR models keep their numbers but are closer to
node-breaker: thousands of zero-impedance branches (ties: closed breakers and
switches) join nodes that the DAM model has already merged into one bus. Neither
number nor name identifies anything across snapshots or across models.

What is stable is the equipment attached: branch names, generator names, load names
and settlement point names all resolve to the same substation and voltage hour after
hour. So a **bus key** is derived from the substation, the voltage and the sorted set
of attached equipment names. The PSS/E number becomes a per-snapshot attribute used
only for joins within that snapshot.

For CRR, nodes joined by in-service ties are grouped into one bus first and share its
key; the members and the ties are kept so monitored ties can be reported. A DAM node
is a bus on its own.
"""

from __future__ import annotations

import hashlib
import re

import polars as pl

VERSION = 2  # bump when keys or the contraction rule change; every core.node artifact is rebuilt
TIE_REACTANCE = 1e-4  # |x| at or below this is a bus tie (breaker, switch, jumper)

# ``core.node`` columns, the same for both models. ``node_number`` and ``node_type`` are what
# PSS/E calls the bus number and bus type code. ``raw_name`` is the record's name in the RAW;
# ``substation`` is filled only where that name is a substation (DAM), and is null for CRR,
# whose RAW names nodes. ``bus_group`` is the number of the bus's representative node (the
# smallest member of a CRR tie group, the node itself otherwise).
COLUMNS = ("node_number", "raw_name", "substation", "kv", "node_type", "bus_group", "is_tie_member",
           "attachments", "n_attachments", "bus_key", "is_ambiguous")

# Prefixes keep equipment kinds apart inside a key: a generator and a load with the
# same name must not collapse into one attachment.
_KIND = {"branch": "B", "generator": "G", "load": "D", "settlement_point": "S", "source_sink": "S"}


def _norm(value: str | None) -> str | None:
    return re.sub(r"\s+", " ", value.strip().upper()) if value is not None else None


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _attachments(pairs: list[tuple[pl.DataFrame, str, str, str]]) -> pl.DataFrame:
    """Long table (bus, label) from (frame, bus column, name column, kind) quadruples."""
    parts = [
        frame.select(pl.col(bus).cast(pl.Int64).alias("bus"),
                     (pl.lit(_KIND[kind] + ":") + pl.col(name).cast(pl.String).str.strip_chars().str.to_uppercase()).alias("label"))
        .drop_nulls()
        for frame, bus, name, kind in pairs
    ]
    return pl.concat(parts).unique()


def _keys(nodes: pl.DataFrame, attachments: pl.DataFrame, group: str) -> pl.DataFrame:
    """Add ``attachments``, ``n_attachments``, ``bus_key`` and ``is_ambiguous`` per ``group``."""
    joined = (attachments.group_by("bus").agg(pl.col("label").sort().str.join("|").alias("attachments"),
                                              pl.len().alias("n_attachments")))
    nodes = nodes.join(joined, left_on=group, right_on="bus", how="left").with_columns(
        pl.col("attachments").fill_null(""), pl.col("n_attachments").fill_null(0))
    text = (pl.col("raw_name").fill_null("") + "|" + pl.col("kv").cast(pl.String) + "|" + pl.col("attachments"))
    nodes = nodes.with_columns(text.map_elements(_digest, return_dtype=pl.String).alias("bus_key"))
    counts = nodes.group_by("bus_key").len().rename({"len": "_n"})
    nodes = nodes.join(counts, on="bus_key").with_columns((pl.col("_n") > 1).alias("is_ambiguous")).drop("_n")
    # Buses with the same key (usually isolated buses at one substation) get an ordinal so keys stay unique.
    return nodes.with_columns(
        pl.when(pl.col("is_ambiguous"))
        .then(pl.col("bus_key") + "#" + pl.col(group).rank("ordinal").over("bus_key").cast(pl.String))
        .otherwise(pl.col("bus_key")).alias("bus_key")
    )


def dam_nodes(bus: pl.DataFrame, lines: pl.DataFrame, transformers: pl.DataFrame, generators: pl.DataFrame,
              loads: pl.DataFrame, settlement_points: pl.DataFrame) -> pl.DataFrame:
    """One row per RAW bus of one DAM hourly model, with its stable ``bus_key``.

    Inputs are the raw tables of one hour (``psse_bus``, ``dam_lines``, ``dam_transformers``,
    ``dam_generators``, ``dam_loads``, ``dam_settlement_points``).
    """
    branches = pl.concat([lines.select("psse_from_bus_number", "psse_to_bus_number", "branch_name"),
                          transformers.select("psse_from_bus_number", "psse_to_bus_number", "branch_name")])
    attachments = _attachments([
        (branches, "psse_from_bus_number", "branch_name", "branch"),
        (branches, "psse_to_bus_number", "branch_name", "branch"),
        (generators, "psse_bus_number", "generator_name", "generator"),
        (loads, "psse_bus_number", "load_name", "load"),
        (settlement_points, "psse_bus_number", "settlement_point_name", "settlement_point"),
    ])
    nodes = bus.select(pl.col("i").alias("node_number"), pl.col("name").map_elements(_norm, return_dtype=pl.String).alias("raw_name"),
                       pl.col("basekv").alias("kv"), pl.col("ide").alias("node_type"))
    nodes = _keys(nodes, attachments, "node_number")
    return nodes.with_columns(pl.col("node_number").alias("bus_group"), pl.lit(False).alias("is_tie_member"), pl.col("raw_name").alias("substation")).select(COLUMNS).sort("node_number")


def tie_groups(branch: pl.DataFrame, tie_reactance: float = TIE_REACTANCE) -> pl.DataFrame:
    """Union-find over in-service branches with |x| <= tie_reactance: (node_number, tie_group)."""
    ties = branch.filter((pl.col("x").abs() <= tie_reactance) & (pl.col("st") == 1)).select("i", "j")
    parent: dict[int, int] = {}

    def find(a: int) -> int:
        while parent.setdefault(a, a) != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, j in ties.rows():
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    members = sorted(parent)
    return pl.DataFrame({"node_number": members, "tie_group": [find(b) for b in members]}, schema={"node_number": pl.Int64, "tie_group": pl.Int64})


def crr_nodes(bus: pl.DataFrame, branch: pl.DataFrame, transformer: pl.DataFrame, autos: pl.DataFrame,
              sources_sinks: pl.DataFrame, tie_reactance: float = TIE_REACTANCE) -> pl.DataFrame:
    """One row per RAW bus of a CRR model with its ``bus_key`` after contracting bus ties.

    Inputs are ``psse_bus``, ``psse_branch``, ``psse_transformer``, ``crr_mapping_autos``
    and ``crr_sources_and_sinks``. Lines are named by their RAW comment, transformers by
    the ``Autos`` sheet matched on (from, to, ckt), settlement points by the source/sink
    whose ``BusName`` is "<number> <name>". Buses in one tie group share ``bus_key`` and
    ``tie_group`` (the smallest member number).
    """
    groups = tie_groups(branch, tie_reactance)
    nodes = (bus.select(pl.col("i").alias("node_number"), pl.col("name").map_elements(_norm, return_dtype=pl.String).alias("raw_name"),
                        pl.col("basekv").alias("kv"), pl.col("ide").alias("node_type"))
             .join(groups.rename({"tie_group": "bus_group"}), on="node_number", how="left")
             .with_columns(pl.col("bus_group").fill_null(pl.col("node_number"))))
    to_group = nodes.select("node_number", "bus_group")

    lines = branch.filter(pl.col("x").abs() > tie_reactance).select("i", "j", pl.col("comment").alias("name"))
    from .branch import autos_by_key  # both orientations of the sheet's (from, to, ckt)

    xf = (transformer.select("i", "j", pl.col("ckt").str.strip_chars().alias("ckt"))
          .join(autos_by_key(autos).rename({"from_node": "i", "to_node": "j"}), on=["i", "j", "ckt"], how="left"))
    xf = xf.with_columns(pl.coalesce(pl.col("name"), pl.format("XF {} {} {}", "i", "j", "ckt")).alias("name"))
    sp = sources_sinks.select(pl.col("bus_name").str.extract(r"^\s*(\d+)").cast(pl.Int64).alias("bus"), pl.col("name"))
    both = pl.concat([lines, xf.select("i", "j", "name")])
    attachments = _attachments([
        (both, "i", "name", "branch"), (both, "j", "name", "branch"), (sp, "bus", "name", "source_sink"),
    ]).join(to_group, left_on="bus", right_on="node_number", how="inner").select(pl.col("bus_group").alias("bus"), "label")

    # A group's key text joins its member names, so one contraction has one key.
    grouped = nodes.group_by("bus_group").agg(pl.col("raw_name").sort().str.join("+").alias("raw_name"), pl.col("kv").min().alias("kv"))
    keyed = _keys(grouped, attachments, "bus_group").drop("raw_name", "kv")
    return (nodes.join(keyed, on="bus_group", how="left")
            .with_columns(pl.col("node_number").is_in(groups["node_number"].implode()).alias("is_tie_member"), pl.lit(None, pl.String).alias("substation"))
            .select(COLUMNS).sort("node_number"))
