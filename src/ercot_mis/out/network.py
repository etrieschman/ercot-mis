"""``out.network``: one snapshot's network in one vocabulary, ready for a DC model.

A :class:`Network` is what ``ftr_align`` builds its incidence matrix and constraint
rows from: nodes with a dense index, branches with reactance, tap ratio and the two
limits (base case and post-contingency), contingencies as sets of branch indexes,
and GTCs as factor-weighted member sets. It is assembled from the core tables of one
snapshot; core stores facts, this module applies the judgment calls, each of them a
named option in :class:`Options` defaulting to ERCOT practice.

What the assembly does, in order:

1. **Nodes, or buses.** By default one row per model node (one RAW record), as ERCOT's
   engines solve it: the CRR auction keeps its bus ties (closed breakers at the minimum
   reactance) as branches and enforces the ones it monitors. With ``contract_ties`` the
   rows are buses instead (each CRR bus is its nodes joined by in-service ties;
   ``core.node`` carries the grouping); that is an analysis choice, measured to move
   shift factors by small amounts and to drop the monitored ties' limits. DAM RAWs have
   no ties, so the option does nothing there. The frames are named for the default
   (``nodes``, ``node_id``); under contraction ``n_members`` says how many nodes a bus holds.
2. **Branches.** Out-of-service branches are dropped. A branch whose two ends are the
   same node (a contracted tie, or a real branch in parallel with a tie group) carries
   no flow in a DC model and is dropped. Nodes with no branch left, and any component
   smaller than the largest one, are dropped with the branches inside them. Every
   drop is listed in ``dropped_branches`` / ``dropped_nodes`` with its reason.
   The slack is ERCOT's: the swing bus (type 3) the RAW marks inside the kept
   network. The CRR RAW marks one per island, so after the islands are dropped one
   remains; if several remained the busiest would be taken, and if none, the busiest
   node, with ``slack_source`` saying which case applied. Negative reactances (series
   capacitors) are kept as written.
3. **Limits.** ``base_limit_mw`` applies in the base case, ``contingency_limit_mw``
   under every contingency; both are ``+inf`` where the model does not enforce the
   branch (``limits``). CRR limits come from the monitored-element CSV for one
   time-of-use block, DAM limits from the RAW rates (``rating_source``); a PSS/E rate
   of zero means no limit and becomes ``+inf`` too.
4. **Contingencies.** Every contingency of the model, as the indexes of the branches
   it removes that are in the network. Outages of dropped branches (a breaker in a
   contracted CRR node), unresolved device names, and DAM's load, generator,
   settlement-point and split-bus rows are counted, not applied. A contingency whose
   set comes out empty (every element it removes is already out of service, a
   contracted tie, or unknown to the RAW) constrains nothing beyond the base case; it
   is dropped and listed in ``dropped_contingencies`` (``drop_empty_contingencies``).
5. **GTCs.** Each member's factor is signed with the branch's from-to orientation as
   the RAW lists it. The CSV's flow direction refers to the member's *name*, and CRR
   transformer names run the other way for a large minority (``is_name_reversed`` on
   ``core.branch``), so the sign is flipped for those.
   DAM GTCs carry limits and the CRR id from the manual crosswalk but no members
   (see docs/datasets/generic-transmission-limits.md).
6. **Settlement points.** Each settlement point's node weights (``core.
   settlement_point_bus``) restricted to the kept nodes and renormalized; the weight
   that fell on dropped nodes is recorded per point as ``weight_dropped``.

Nothing here decides which contingencies island the network: that depends on the
consumer's connectivity check, as in ``ftr_align``. ``Session.network`` assembles a network on first request and keeps
it on every call (assembly is cheap; a saved copy read slower than assembling).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import polars as pl

VERSION = 4

INF = math.inf

# The row of docs/assumptions.md behind every option and every reason an element is
# dropped. tests/test_register.py fails when one is missing here or from the register.
REGISTER_ROWS = {
    "option": {
        "contract_ties": "TOP-01", "rating_source": "RAT-04", "time_of_use": "RAT-06", "limits": "RAT-09",
        "contingency_rating": "RAT-04", "keep_out_of_service": "TOP-03", "drop_empty_contingencies": "RAT-10",
    },
    "dropped_branches": {"out_of_service": "TOP-03", "contracted_tie": "TOP-01", "loop": "TOP-01", "island": "TOP-05"},
    "dropped_nodes": {"isolated": "TOP-05", "island": "TOP-05"},
    "dropped_contingencies": {"empty": "RAT-10"},
}


@dataclass(frozen=True)
class Options:
    """The judgment calls, each defaulting to ERCOT practice for the model at hand."""

    contract_ties: bool = False
    """CRR: one row per bus (nodes joined by in-service bus ties) instead of one per node (an
    analysis choice; ERCOT solves with the ties and enforces the monitored ones). No effect on DAM."""

    rating_source: str | None = None
    """``"crr_monitored"`` (the CRR CSV; the CRR default) or ``"psse_raw"`` (RAW rates;
    the DAM default). None picks the model's default."""

    time_of_use: str = "PeakWD"
    """The CRR CSV block to take limits from; the CRR RAW is the PeakWD model."""

    limits: str = "enforced"
    """Which branches get finite limits: ``"enforced"`` (CRR monitored, DAM secured),
    ``"monitored"`` (DAM: monitored or secured), ``"all"`` (every branch with a rating)."""

    contingency_rating: str = "emergency"
    """``"emergency"`` (rate B / EmergencyRating) or ``"base"`` under contingencies."""

    keep_out_of_service: bool = False
    """Keep branches the RAW marks out of service (they still count as topology)."""

    drop_empty_contingencies: bool = True
    """Drop contingencies that remove no branch of the network (listed in
    ``dropped_contingencies``); with False they stay, flagged ``is_empty``."""


@dataclass(frozen=True)
class Network:
    """One snapshot as a DC model reads it. Frames are sorted; ``index`` is the row number."""

    snapshot_id: str
    options: Options
    slack_node_id: str | None
    slack_source: str
    """``"ercot"`` when the slack is a swing bus the RAW marks, ``"fallback"`` when none
    survived and the busiest node stands in."""
    nodes: pl.DataFrame
    """One row per node, or per bus when ``contract_ties`` is set. ``index, node_id, raw_name, substation, kv,
    n_members, node_number, is_slack`` (``substation`` is null for CRR models, whose RAW names nodes);
    ``n_members`` is how many nodes the row holds (more than one only for a bus under contraction)."""
    branches: pl.DataFrame
    """``index, branch_id, kind, from_node_id, to_node_id, from_index, to_index, x_pu,
    tap_ratio, base_limit_mw, contingency_limit_mw, is_limited, is_monitored, is_secured``."""
    contingencies: pl.DataFrame
    """``contingency_id, branch_ids, branch_indexes, n_outages, n_dropped, n_unresolved,
    n_other_rows, has_split_bus, split_branch_indexes, split_ends, n_split_unapplied, is_empty``.
    ``split_branch_indexes`` and ``split_ends`` (``from``/``to``) are the branch ends a
    split-bus row moves to a new bus section (NAM-09); ``n_split_unapplied`` counts split
    rows on loads, generators and settlement points, which a DC topology does not carry."""
    gtcs: pl.DataFrame
    """``gtc_id, source, limit_mw, n_members, n_unresolved, crr_gtc_id``."""
    gtc_members: pl.DataFrame
    """``gtc_id, branch_id, branch_index, factor`` (signed with the branch orientation)."""
    settlement_points: pl.DataFrame
    """``settlement_point_id, kind, n_nodes, weight_dropped``."""
    settlement_point_nodes: pl.DataFrame
    """``settlement_point_id, node_id, node_index, weight`` (weights sum to one per point)."""
    dropped_branches: pl.DataFrame
    """``branch_id, reason, is_monitored``; reasons ``out_of_service``, ``contracted_tie``,
    ``loop``, ``island``."""
    dropped_nodes: pl.DataFrame
    """``node_id, reason``; reasons ``isolated`` (no branch) and ``island``."""
    dropped_contingencies: pl.DataFrame
    """``contingency_id, reason, n_dropped, n_unresolved, n_other_rows``; reason ``empty``."""

    @property
    def n_nodes(self) -> int:
        return self.nodes.height

    @property
    def n_branches(self) -> int:
        return self.branches.height

    def summary(self) -> dict:
        """Counts only; safe to print."""
        return {
            "snapshot_id": self.snapshot_id, "nodes": self.n_nodes, "branches": self.n_branches,
            "limited_branches": int(self.branches["is_limited"].sum()),
            "contingencies": self.contingencies.height, "empty_contingencies": int(self.contingencies["is_empty"].sum()),
            "dropped_contingencies": {r: n for r, n in self.dropped_contingencies.group_by("reason").len().sort("reason").rows()},
            "slack": {"node_id": self.slack_node_id, "source": self.slack_source},
            "gtcs": self.gtcs.height, "gtc_members": self.gtc_members.height,
            "settlement_points": self.settlement_points.height,
            "settlement_points_without_node": int((self.settlement_points["n_nodes"] == 0).sum()),
            "dropped_branches": {r: n for r, n in self.dropped_branches.group_by("reason").len().sort("reason").rows()},
            "dropped_nodes": {r: n for r, n in self.dropped_nodes.group_by("reason").len().sort("reason").rows()},
            "options": asdict(self.options),
        }


# ---------------------------------------------------------------------- building


@dataclass(frozen=True)
class CoreTables:
    """The core rows of one snapshot (``snapshot_id`` column already filtered away or present)."""

    node: pl.DataFrame
    branch: pl.DataFrame
    branch_rating: pl.DataFrame
    contingency: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema={"contingency_id": pl.String, "has_split_bus": pl.Boolean}))
    contingency_outage: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(
        schema={"contingency_id": pl.String, "element_kind": pl.String, "operation": pl.String, "branch_id": pl.String, "is_resolved": pl.Boolean}))
    gtc: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(
        schema={"gtc_id": pl.String, "source": pl.String, "limit_mw": pl.Float64, "crr_gtc_id": pl.String}))
    gtc_member: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(
        schema={"gtc_id": pl.String, "branch_id": pl.String, "factor": pl.Float64, "flow_direction": pl.String, "is_resolved": pl.Boolean}))
    settlement_point: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema={"settlement_point_id": pl.String, "kind": pl.String}))
    settlement_point_bus: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(
        schema={"settlement_point_id": pl.String, "bus_key": pl.String, "weight": pl.Float64, "is_resolved": pl.Boolean}))


def is_dam(snapshot_id: str) -> bool:
    return snapshot_id.startswith("dam:")


def _components(nodes: list[str], edges: pl.DataFrame) -> dict[str, str]:
    """Union-find over ``edges`` (from_node_id, to_node_id): node -> component representative."""
    parent = {n: n for n in nodes}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in edges.select("from_node_id", "to_node_id").rows():
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {n: find(n) for n in nodes}


def _node_ids(node: pl.DataFrame, contract_ties: bool) -> pl.DataFrame:
    """One row per model node with the ``node_id`` it is listed under: its own, or its bus's when ties are contracted."""
    if contract_ties:
        return node.with_columns(pl.col("bus_key").alias("node_id"))
    # Tie members keep their own bus as a node; the key still says which group they are in.
    return node.with_columns(
        pl.when(pl.col("is_tie_member")).then(pl.col("bus_key") + "@" + pl.col("node_number").cast(pl.String))
        .otherwise(pl.col("bus_key")).alias("node_id"))


def _limits(branch: pl.DataFrame, ratings: pl.DataFrame, options: Options, dam: bool) -> pl.DataFrame:
    """Add ``base_limit_mw``, ``contingency_limit_mw`` and ``is_limited``."""
    source = options.rating_source or ("psse_raw" if dam else "crr_monitored")
    rows = ratings.filter(pl.col("rating_source") == source)
    if source == "crr_monitored":
        rows = rows.filter(pl.col("time_of_use") == options.time_of_use)
        if rows.is_empty() and not ratings.filter(pl.col("rating_source") == source).is_empty():
            raise ValueError(f"no crr_monitored ratings for time_of_use={options.time_of_use!r}")
    rows = rows.select("branch_id", "base_mw", "emergency_mw").unique(subset=["branch_id"], keep="first")
    if options.limits == "enforced":
        enforced = pl.col("is_secured")
    elif options.limits == "monitored":
        enforced = pl.col("is_secured") | pl.col("is_monitored")
    elif options.limits == "all":
        enforced = pl.lit(True)
    else:
        raise ValueError(f"limits must be 'enforced', 'monitored' or 'all', not {options.limits!r}")
    if options.contingency_rating not in ("emergency", "base"):
        raise ValueError(f"contingency_rating must be 'emergency' or 'base', not {options.contingency_rating!r}")
    post = pl.col("emergency_mw") if options.contingency_rating == "emergency" else pl.col("base_mw")
    # A PSS/E rating of zero means "no limit"; so does a branch the CSV does not list.
    usable = pl.col("base_mw").is_not_null() & (pl.col("base_mw") > 0)
    return (branch.join(rows, on="branch_id", how="left")
            .with_columns((enforced.fill_null(False) & usable).alias("is_limited"))
            .with_columns(pl.when(pl.col("is_limited")).then(pl.col("base_mw")).otherwise(INF).alias("base_limit_mw"),
                          pl.when(pl.col("is_limited") & post.is_not_null() & (post > 0)).then(post)
                          .when(pl.col("is_limited")).then(pl.col("base_mw")).otherwise(INF).alias("contingency_limit_mw"))
            .drop("base_mw", "emergency_mw"))


def build_network(snapshot_id: str, core: CoreTables, options: Options | None = None) -> Network:
    """Assemble one snapshot's :class:`Network` from its core tables."""
    options = options or Options()
    dam = is_dam(snapshot_id)
    buses = _node_ids(core.node, options.contract_ties)
    bus_to_node = buses.select("node_number", "node_id")

    branch = (core.branch.join(bus_to_node.rename({"node_number": "from_node", "node_id": "from_node_id"}), on="from_node", how="left")
              .join(bus_to_node.rename({"node_number": "to_node", "node_id": "to_node_id"}), on="to_node", how="left"))
    reason = (pl.when(~pl.col("is_in_service") & ~pl.lit(options.keep_out_of_service)).then(pl.lit("out_of_service"))
              .when((pl.col("from_node_id") == pl.col("to_node_id")) & pl.col("is_tie")).then(pl.lit("contracted_tie"))
              .when(pl.col("from_node_id") == pl.col("to_node_id")).then(pl.lit("loop"))
              .otherwise(None).alias("reason"))
    branch = branch.with_columns(reason)
    kept = branch.filter(pl.col("reason").is_null())

    # Largest connected component over the kept branches; everything else is dropped.
    node_ids = sorted(buses["node_id"].unique())
    component = _components(node_ids, kept)
    sizes: dict[str, int] = {}
    for rep in component.values():
        sizes[rep] = sizes.get(rep, 0) + 1
    main = max(sizes, key=lambda r: (sizes[r], r)) if sizes else None
    touched = set(kept["from_node_id"]) | set(kept["to_node_id"])
    dropped_nodes = pl.DataFrame({"node_id": node_ids}, schema={"node_id": pl.String}).with_columns(
        pl.when(~pl.col("node_id").is_in(list(touched))).then(pl.lit("isolated"))
        .when(pl.col("node_id").replace_strict(component, return_dtype=pl.String) != main).then(pl.lit("island"))
        .otherwise(None).alias("reason")).filter(pl.col("reason").is_not_null())
    branch = branch.with_columns(
        pl.when(pl.col("reason").is_null() & (pl.col("from_node_id").is_in(dropped_nodes["node_id"].implode()) | pl.col("to_node_id").is_in(dropped_nodes["node_id"].implode())))
        .then(pl.lit("island")).otherwise(pl.col("reason")).alias("reason"))
    dropped_branches = branch.filter(pl.col("reason").is_not_null()).select("branch_id", "reason", pl.col("is_monitored").fill_null(False)).sort("branch_id")
    kept = branch.filter(pl.col("reason").is_null())

    # One row per node (or bus) in the main component, with a representative node number.
    nodes = (buses.filter(~pl.col("node_id").is_in(dropped_nodes["node_id"].implode()))
             .group_by("node_id").agg(pl.col("raw_name").first(), pl.col("substation").first(), pl.col("kv").min(), pl.len().alias("n_members"),
                                      pl.col("node_number").min(), (pl.col("node_type") == 3).any().alias("_slack"))
             .sort("node_id").with_row_index("index"))
    degree = pl.concat([kept.select(pl.col("from_node_id").alias("node_id")), kept.select(pl.col("to_node_id").alias("node_id"))]).group_by("node_id").len()
    nodes = nodes.join(degree, on="node_id", how="left").with_columns(pl.col("len").fill_null(0))
    marked = nodes.filter(pl.col("_slack"))
    slack_source = "ercot" if marked.height else "fallback"
    candidates = marked if marked.height else nodes
    slack = candidates.sort("len", "kv", "node_id", descending=[True, True, False])["node_id"][0] if candidates.height else None
    nodes = nodes.with_columns((pl.col("node_id") == slack).alias("is_slack")).select(
        "index", "node_id", "raw_name", "substation", "kv", "n_members", "node_number", "is_slack")

    index_of = nodes.select("node_id", "index")
    branches = (_limits(kept, core.branch_rating, options, dam)
                .join(index_of.rename({"node_id": "from_node_id", "index": "from_index"}), on="from_node_id")
                .join(index_of.rename({"node_id": "to_node_id", "index": "to_index"}), on="to_node_id")
                .sort("branch_id").with_row_index("index")
                .with_columns(pl.col("tap_ratio").fill_null(1.0), pl.col("is_monitored").fill_null(False), pl.col("is_secured").fill_null(False))
                .select("index", "branch_id", "kind", "from_node_id", "to_node_id", "from_index", "to_index", "x_pu", "tap_ratio",
                        "base_limit_mw", "contingency_limit_mw", "is_limited", "is_monitored", "is_secured"))
    branch_index = branches.select("branch_id", pl.col("index").alias("branch_index"))

    # Contingencies as index sets, plus the branch ends their split-bus rows move (NAM-09).
    outages = core.contingency_outage.join(branch_index, on="branch_id", how="left")
    if "split_end" not in outages.columns:
        outages = outages.with_columns(pl.lit(None, pl.String).alias("split_end"))
    is_branch = pl.col("element_kind").is_in(["line", "transformer", "branch"]) & (pl.col("operation") == "outage")
    is_split = (pl.col("operation") == "split_bus") & pl.col("branch_index").is_not_null() & pl.col("split_end").is_not_null()
    per = (outages.group_by("contingency_id").agg(
        pl.col("branch_id").filter(is_branch & pl.col("branch_index").is_not_null()).unique().sort().alias("branch_ids"),
        pl.col("branch_index").filter(is_branch & pl.col("branch_index").is_not_null()).unique().sort().alias("branch_indexes"),
        (is_branch & pl.col("is_resolved") & pl.col("branch_index").is_null()).sum().cast(pl.UInt32).alias("n_dropped"),
        (~pl.col("is_resolved")).sum().cast(pl.UInt32).alias("n_unresolved"),
        (~is_branch).sum().cast(pl.UInt32).alias("n_other_rows"),
        (pl.col("operation") == "split_bus").any().alias("has_split_bus"),
        pl.col("branch_index").filter(is_split).alias("split_branch_indexes"),
        pl.col("split_end").filter(is_split).alias("split_ends"),
        ((pl.col("operation") == "split_bus") & ~is_split).sum().cast(pl.UInt32).alias("n_split_unapplied")))
    contingencies = (core.contingency.select("contingency_id").join(per, on="contingency_id", how="left")
                     .with_columns(pl.col("branch_ids").fill_null(pl.lit([], dtype=pl.List(pl.String))),
                                   pl.col("branch_indexes").fill_null(pl.lit([], dtype=pl.List(pl.UInt32))),
                                   pl.col("n_dropped").fill_null(0), pl.col("n_unresolved").fill_null(0), pl.col("n_other_rows").fill_null(0),
                                   pl.col("has_split_bus").fill_null(False),
                                   pl.col("split_branch_indexes").fill_null(pl.lit([], dtype=pl.List(pl.UInt32))),
                                   pl.col("split_ends").fill_null(pl.lit([], dtype=pl.List(pl.String))), pl.col("n_split_unapplied").fill_null(0))
                     .with_columns(pl.col("branch_indexes").list.len().cast(pl.UInt32).alias("n_outages"))
                     .with_columns(((pl.col("n_outages") == 0) & (pl.col("split_branch_indexes").list.len() == 0)).alias("is_empty"))
                     .select("contingency_id", "branch_ids", "branch_indexes", "n_outages", "n_dropped", "n_unresolved", "n_other_rows",
                             "has_split_bus", "split_branch_indexes", "split_ends", "n_split_unapplied", "is_empty")
                     .sort("contingency_id"))
    empty = contingencies.filter(pl.col("is_empty")) if options.drop_empty_contingencies else contingencies.clear()
    dropped_contingencies = empty.select("contingency_id", pl.lit("empty").alias("reason"), "n_dropped", "n_unresolved", "n_other_rows")
    if options.drop_empty_contingencies:
        contingencies = contingencies.filter(~pl.col("is_empty"))

    # GTCs: factor signed with the RAW orientation of the branch. "From-To" in the CSV refers to
    # the member's name, which runs the other way for some transformers (is_name_reversed).
    reversed_names = (kept.select("branch_id", pl.col("is_name_reversed").fill_null(False)) if "is_name_reversed" in kept.columns
                      else kept.select("branch_id", pl.lit(False).alias("is_name_reversed")))
    sign = (pl.when(pl.col("flow_direction").str.to_uppercase().str.starts_with("TO")).then(-1.0).otherwise(1.0)
            * pl.when(pl.col("is_name_reversed")).then(-1.0).otherwise(1.0))
    members = (core.gtc_member.join(branch_index, on="branch_id", how="left").join(reversed_names, on="branch_id", how="left")
               .with_columns(pl.col("is_name_reversed").fill_null(False))
               .with_columns((pl.col("factor") * sign).alias("factor"), pl.col("branch_index").is_not_null().alias("_in")))
    gtc_members = members.filter(pl.col("_in")).select("gtc_id", "branch_id", "branch_index", "factor").sort("gtc_id", "branch_id")
    counts = members.group_by("gtc_id").agg(pl.col("_in").sum().cast(pl.UInt32).alias("n_members"), (~pl.col("_in")).sum().cast(pl.UInt32).alias("n_unresolved"))
    gtcs = (core.gtc.select("gtc_id", "source", "limit_mw", "crr_gtc_id").join(counts, on="gtc_id", how="left")
            .with_columns(pl.col("n_members").fill_null(0), pl.col("n_unresolved").fill_null(0))
            .select("gtc_id", "source", "limit_mw", "n_members", "n_unresolved", "crr_gtc_id").sort("gtc_id"))

    # Settlement points on the kept nodes. Without contraction a CRR point's bus_key is a group key;
    # its buses are the group members, so weights are spread over them equally.
    point_rows = core.settlement_point_bus.filter(pl.col("is_resolved")).select("settlement_point_id", "bus_key", "weight")
    if options.contract_ties:
        point_rows = point_rows.with_columns(pl.col("bus_key").alias("node_id"))
    else:
        members = buses.group_by("bus_key").agg(pl.col("node_id"))
        point_rows = (point_rows.join(members, on="bus_key", how="left").explode("node_id")
                      .with_columns((pl.col("weight") / pl.col("node_id").count().over("settlement_point_id", "bus_key")).alias("weight")))
    point_rows = point_rows.join(index_of.rename({"index": "node_index"}), on="node_id", how="left")
    kept_weight = point_rows.group_by("settlement_point_id").agg(pl.col("weight").filter(pl.col("node_index").is_not_null()).sum().alias("_kept"),
                                                                 pl.col("weight").filter(pl.col("node_index").is_null()).sum().alias("weight_dropped"))
    settlement_point_nodes = (point_rows.filter(pl.col("node_index").is_not_null()).join(kept_weight, on="settlement_point_id")
                              .with_columns((pl.col("weight") / pl.col("_kept")).alias("weight"))
                              .select("settlement_point_id", "node_id", "node_index", "weight").sort("settlement_point_id", "node_id"))
    settlement_points = (core.settlement_point.select("settlement_point_id", "kind").join(kept_weight, on="settlement_point_id", how="left")
                         .join(settlement_point_nodes.group_by("settlement_point_id").len().rename({"len": "n_nodes"}), on="settlement_point_id", how="left")
                         .with_columns(pl.col("n_nodes").fill_null(0).cast(pl.UInt32), pl.col("weight_dropped").fill_null(0.0))
                         .select("settlement_point_id", "kind", "n_nodes", "weight_dropped").sort("settlement_point_id"))

    for name, frame in (("dropped_branches", dropped_branches), ("dropped_nodes", dropped_nodes), ("dropped_contingencies", dropped_contingencies)):
        unknown = set(frame["reason"].unique()) - set(REGISTER_ROWS[name])
        if unknown:
            raise ValueError(f"{name} reason(s) {sorted(unknown)} have no row in docs/assumptions.md")
    return Network(snapshot_id, options, slack, slack_source, nodes, branches, contingencies, gtcs, gtc_members,
                   settlement_points, settlement_point_nodes, dropped_branches, dropped_nodes.sort("node_id"), dropped_contingencies)


def core_tables(session, snapshot_id: str) -> CoreTables:
    """Read one snapshot's rows from every core table."""
    def rows(table: str) -> pl.DataFrame:
        return session.core(table).filter(pl.col("snapshot_id") == snapshot_id).collect().drop("snapshot_id")

    node = rows("node")
    if node.is_empty():
        raise KeyError(f"no core rows for {snapshot_id!r}; run build_core()")
    return CoreTables(node, rows("branch"), rows("branch_rating"), rows("contingency"), rows("contingency_outage"), rows("gtc"), rows("gtc_member"),
                      rows("settlement_point"), rows("settlement_point_bus"))


# ---------------------------------------------------------------------- on disk

