"""``out.network``: one snapshot's network in one vocabulary, ready for a DC model.

A :class:`Network` is what ``ftr_align`` builds its incidence matrix and constraint rows
from: buses with a dense index, branches with reactance, tap ratio and the two limits
(base case and post-contingency), contingencies as sets of branch indexes, GTCs as
factor-weighted member sets, and settlement point weights over the buses. Core stores
facts; this module assembles them in six steps, every rule ERCOT's practice (named in
docs/assumptions.md) except the two in :class:`Options`.

A network handed to a solver is a bus-branch model: the solver sees branches and the
points they join, never a breaker as a breaker. So the rows are **buses**. For the DAM
they are the model's buses. For the CRR model ERCOT's auction keeps the closed breakers
(ties) as branches, so by default each CRR node is a bus of one node (``n_nodes`` = 1)
and ``bus_key`` repeats across the nodes of one tie group; with ``contract_ties`` the rows
are ``core.bus`` (TOP-01). The steps: buses (1); branches, the main component and the
slack (2); limits (3); contingencies (4); GTCs (5); settlement point weights (6). Every
element left out is listed in ``dropped_branches``, ``dropped_buses`` and
``dropped_contingencies`` with its reason; docs/out-network.md has the conventions.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import polars as pl

from ..core.bus import components
from ..core.snapshot import is_dam
from ..raw.table import flag

INF = math.inf

# The row of docs/assumptions.md behind every option and every reason an element is
# dropped. tests/test_register.py fails when one is missing here or from the register.
REGISTER_ROWS = {
    "option": {"contract_ties": "TOP-01", "time_of_use": "RAT-06"},
    "dropped_branches": {"out_of_service": "TOP-03", "contracted_tie": "TOP-01", "loop": "TOP-01", "island": "TOP-05"},
    "dropped_buses": {"isolated": "TOP-05", "island": "TOP-05"},
    "dropped_contingencies": {"empty": "RAT-10"},
}


@dataclass(frozen=True)
class Options:
    """The two judgment calls a check varies; every other rule of the assembly is ERCOT's practice, fixed in code and named in docs/assumptions.md."""

    contract_ties: bool = False
    """CRR: one row per bus (nodes joined by in-service bus ties) instead of one per node. ERCOT's CRR auction solves
    with the ties as branches and enforces the monitored ones (TOP-01); ``checks/network.py`` measures the cost of
    contracting. No effect on DAM."""

    time_of_use: str = "PeakWD"
    """The CRR monitored-element CSV block the limits come from; the CRR RAW is the PeakWD model (RAT-06)."""


@dataclass(frozen=True)
class Network:
    """One snapshot as a DC model reads it. Frames are sorted; ``index`` is the row number."""

    snapshot_id: str
    options: Options
    slack_bus_id: str | None
    slack_source: str  # "ercot": a swing bus the RAW marks; "fallback": none survived and the busiest bus stands in
    buses: pl.DataFrame  # index, bus_id, bus_key, raw_name, substation, kv, n_nodes, node_number, bus_number, is_slack
    branches: pl.DataFrame  # index, branch_id, kind, from_bus_id, to_bus_id, from_index, to_index, x_pu, tap_ratio, base_limit_mw, contingency_limit_mw, is_limited, is_monitored, is_secured
    contingencies: pl.DataFrame  # contingency_id, branch_ids, branch_indexes, n_outages, n_dropped, n_unresolved, n_other_rows, has_split_bus, split_branch_indexes, split_ends, n_split_unapplied, is_empty
    gtcs: pl.DataFrame  # gtc_id, source, limit_mw, n_members, n_unresolved, crr_gtc_id
    gtc_members: pl.DataFrame  # gtc_id, branch_id, branch_index, factor (signed with the branch orientation)
    settlement_points: pl.DataFrame  # settlement_point_id, kind, n_buses, weight_dropped
    settlement_point_buses: pl.DataFrame  # settlement_point_id, bus_id, bus_index, weight (summing to one per point)
    dropped_branches: pl.DataFrame  # branch_id, reason (out_of_service, contracted_tie, loop, island), is_monitored
    dropped_buses: pl.DataFrame  # bus_id, reason (isolated, island)
    dropped_contingencies: pl.DataFrame  # contingency_id, reason (empty), n_dropped, n_unresolved, n_other_rows

    @property
    def n_buses(self) -> int:
        return self.buses.height

    @property
    def n_branches(self) -> int:
        return self.branches.height

    def summary(self) -> dict:
        """Counts only; safe to print."""
        return {
            "snapshot_id": self.snapshot_id, "buses": self.n_buses, "branches": self.n_branches,
            "limited_branches": int(self.branches["is_limited"].sum()),
            "contingencies": self.contingencies.height, "empty_contingencies": int(self.contingencies["is_empty"].sum()),
            "dropped_contingencies": {r: n for r, n in self.dropped_contingencies.group_by("reason").len().sort("reason").rows()},
            "slack": {"bus_id": self.slack_bus_id, "source": self.slack_source},
            "gtcs": self.gtcs.height, "gtc_members": self.gtc_members.height,
            "settlement_points": self.settlement_points.height,
            "settlement_points_without_bus": int((self.settlement_points["n_buses"] == 0).sum()),
            "dropped_branches": {r: n for r, n in self.dropped_branches.group_by("reason").len().sort("reason").rows()},
            "dropped_buses": {r: n for r, n in self.dropped_buses.group_by("reason").len().sort("reason").rows()},
            "options": asdict(self.options),
        }


# ---------------------------------------------------------------------- building


@dataclass(frozen=True)
class CoreTables:
    """The core rows of one snapshot (``snapshot_id`` column already filtered away or present). ``node`` is empty for a DAM hour."""

    bus: pl.DataFrame
    node: pl.DataFrame
    branch: pl.DataFrame
    branch_rating: pl.DataFrame
    contingency: pl.DataFrame
    contingency_outage: pl.DataFrame
    gtc: pl.DataFrame
    gtc_member: pl.DataFrame
    settlement_point: pl.DataFrame
    settlement_point_bus: pl.DataFrame


def _bus_rows(core: CoreTables, dam: bool, contract_ties: bool) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Step 1: one row per bus of the model handed to the solver, and the map from the PSS/E numbers its branches carry to ``bus_id``.

    DAM, and CRR with ties contracted: the rows of ``core.bus``. CRR with ties kept (ERCOT's auction topology,
    TOP-01): one bus per node, ``<bus_key>@<node_number>`` for the nodes of a tie group, the bus key alone
    otherwise. DAM branches carry bus numbers; CRR branches carry node numbers, which ``core.node`` maps to a bus.
    """
    buses = core.bus.select(pl.col("bus_key").alias("bus_id"), "bus_key", "raw_name", "substation", "kv", pl.col("bus_type").alias("_type"),
                            "n_nodes", pl.lit(None, pl.Int64).alias("node_number"), "bus_number")
    if dam:
        return buses, core.bus.select(pl.col("bus_number").alias("_number"), pl.col("bus_key").alias("bus_id"))
    one_per_node = pl.when(pl.col("is_tie_member")).then(pl.col("bus_key") + "@" + pl.col("node_number").cast(pl.String)).otherwise(pl.col("bus_key"))
    nodes = core.node.with_columns((pl.col("bus_key") if contract_ties else one_per_node).alias("bus_id"))
    if not contract_ties:
        buses = nodes.select("bus_id", "bus_key", "raw_name", pl.lit(None, pl.String).alias("substation"), "kv", pl.col("node_type").alias("_type"),
                             pl.lit(1, pl.Int64).alias("n_nodes"), "node_number", pl.lit(None, pl.Int64).alias("bus_number"))
    return buses, nodes.select(pl.col("node_number").alias("_number"), "bus_id")


def _limits(branch: pl.DataFrame, ratings: pl.DataFrame, options: Options, dam: bool) -> pl.DataFrame:
    """Add ``base_limit_mw``, ``contingency_limit_mw`` and ``is_limited``."""
    source = "psse_raw" if dam else "crr_monitored"  # the model's own ratings: RAW rate A/B for DAM, the monitored CSV for CRR (RAT-04)
    rows = ratings.filter(pl.col("rating_source") == source)
    if source == "crr_monitored":
        rows = rows.filter(pl.col("time_of_use") == options.time_of_use)
        if rows.is_empty() and not ratings.filter(pl.col("rating_source") == source).is_empty():
            raise ValueError(f"no crr_monitored ratings for time_of_use={options.time_of_use!r}")
    rows = rows.select("branch_id", "base_mw", "emergency_mw").unique(subset=["branch_id"], keep="first")
    enforced = pl.col("is_secured")  # the branches ERCOT enforces: CRR monitored, DAM secured (RAT-08)
    post = pl.col("emergency_mw")  # the post-contingency limit is rate B / EmergencyRating (RAT-04)
    # A PSS/E rating of zero means "no limit"; so does a branch the CSV does not list.
    usable = pl.col("base_mw").is_not_null() & (pl.col("base_mw") > 0)
    return (branch.join(rows, on="branch_id", how="left")
            .with_columns((enforced.fill_null(False) & usable).alias("is_limited"))
            .with_columns(pl.when(pl.col("is_limited")).then(pl.col("base_mw")).otherwise(INF).alias("base_limit_mw"),
                          pl.when(pl.col("is_limited") & post.is_not_null() & (post > 0)).then(post)
                          .when(pl.col("is_limited")).then(pl.col("base_mw")).otherwise(INF).alias("contingency_limit_mw"))
            .drop("base_mw", "emergency_mw"))


def build_network(snapshot_id: str, core: CoreTables, options: Options | None = None) -> Network:
    """Assemble one snapshot's :class:`Network` from its core tables, in the steps the module docstring lists."""
    options = options or Options()
    dam = is_dam(snapshot_id)
    bus_rows, bus_of = _bus_rows(core, dam, options.contract_ties)
    ends = ("from_bus_number", "to_bus_number") if dam else ("from_node", "to_node")  # the PSS/E numbers the model's branches carry
    kept, dropped_branches, dropped_buses = _main_component(core.branch, bus_rows, bus_of, ends)
    buses, slack, slack_source = _buses(bus_rows, kept, dropped_buses)
    branches = _branches(kept, core.branch_rating, buses, options, dam)
    contingencies, dropped_contingencies = _contingencies(core, branches)
    gtcs, gtc_members = _gtcs(core, kept, branches)
    settlement_points, settlement_point_buses = _settlement_points(core, bus_rows, buses)
    for name, frame in (("dropped_branches", dropped_branches), ("dropped_buses", dropped_buses), ("dropped_contingencies", dropped_contingencies)):
        unknown = set(frame["reason"].unique()) - set(REGISTER_ROWS[name])
        if unknown:
            raise ValueError(f"{name} reason(s) {sorted(unknown)} have no row in docs/assumptions.md")
    return Network(snapshot_id, options, slack, slack_source, buses, branches, contingencies, gtcs, gtc_members,
                   settlement_points, settlement_point_buses, dropped_branches, dropped_buses, dropped_contingencies)


def _main_component(branch: pl.DataFrame, bus_rows: pl.DataFrame, bus_of: pl.DataFrame, ends: tuple[str, str]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Step 2: the branches kept, the branches dropped and the buses dropped, each with its reason; ``ends`` names the branch's PSS/E number columns."""
    branch = (branch.join(bus_of.rename({"_number": ends[0], "bus_id": "from_bus_id"}), on=ends[0], how="left")
              .join(bus_of.rename({"_number": ends[1], "bus_id": "to_bus_id"}), on=ends[1], how="left"))
    branch = branch.with_columns(pl.when(~pl.col("is_in_service")).then(pl.lit("out_of_service"))  # TOP-03
                                 .when((pl.col("from_bus_id") == pl.col("to_bus_id")) & pl.col("is_tie")).then(pl.lit("contracted_tie"))
                                 .when(pl.col("from_bus_id") == pl.col("to_bus_id")).then(pl.lit("loop"))
                                 .otherwise(None).alias("reason"))
    kept = branch.filter(pl.col("reason").is_null())
    # The largest connected component over the kept branches; everything else is dropped.
    bus_ids = sorted(bus_rows["bus_id"].unique())
    component = components(bus_ids, kept.select("from_bus_id", "to_bus_id").rows())
    sizes: dict[str, int] = {}
    for rep in component.values():
        sizes[rep] = sizes.get(rep, 0) + 1
    main = max(sizes, key=lambda r: (sizes[r], r)) if sizes else None
    touched = set(kept["from_bus_id"]) | set(kept["to_bus_id"])
    dropped_buses = pl.DataFrame({"bus_id": bus_ids}, schema={"bus_id": pl.String}).with_columns(
        pl.when(~pl.col("bus_id").is_in(list(touched))).then(pl.lit("isolated"))
        .when(pl.col("bus_id").replace_strict(component, return_dtype=pl.String) != main).then(pl.lit("island"))
        .otherwise(None).alias("reason")).filter(pl.col("reason").is_not_null()).sort("bus_id")
    gone = dropped_buses["bus_id"].implode()
    branch = branch.with_columns(pl.when(pl.col("reason").is_null() & (pl.col("from_bus_id").is_in(gone) | pl.col("to_bus_id").is_in(gone)))
                                 .then(pl.lit("island")).otherwise(pl.col("reason")).alias("reason"))
    dropped_branches = branch.filter(pl.col("reason").is_not_null()).select("branch_id", "reason", pl.col("is_monitored").fill_null(False)).sort("branch_id")
    return branch.filter(pl.col("reason").is_null()), dropped_branches, dropped_buses


def _buses(bus_rows: pl.DataFrame, kept: pl.DataFrame, dropped_buses: pl.DataFrame) -> tuple[pl.DataFrame, str | None, str]:
    """Step 2: the buses of the main component with a dense index, and ERCOT's slack."""
    buses = bus_rows.filter(~pl.col("bus_id").is_in(dropped_buses["bus_id"].implode())).sort("bus_id").with_row_index("index")
    degree = pl.concat([kept.select(pl.col("from_bus_id").alias("bus_id")), kept.select(pl.col("to_bus_id").alias("bus_id"))]).group_by("bus_id").len()
    buses = buses.join(degree, on="bus_id", how="left").with_columns(pl.col("len").fill_null(0))
    # ERCOT's swing bus (type 3); if several survive, the busiest; if none, the busiest bus (slack_source says which). TOP-06.
    marked = buses.filter(pl.col("_type") == 3)
    candidates = marked if marked.height else buses
    slack = candidates.sort("len", "kv", "bus_id", descending=[True, True, False])["bus_id"][0] if candidates.height else None
    buses = buses.with_columns((pl.col("bus_id") == slack).alias("is_slack")).select(
        "index", "bus_id", "bus_key", "raw_name", "substation", "kv", "n_nodes", "node_number", "bus_number", "is_slack")
    return buses, slack, "ercot" if marked.height else "fallback"


def _branches(kept: pl.DataFrame, ratings: pl.DataFrame, buses: pl.DataFrame, options: Options, dam: bool) -> pl.DataFrame:
    """Step 3: the kept branches with their limits and their ends' bus indexes."""
    index_of = buses.select("bus_id", "index")
    return (_limits(kept, ratings, options, dam)
            .join(index_of.rename({"bus_id": "from_bus_id", "index": "from_index"}), on="from_bus_id")
            .join(index_of.rename({"bus_id": "to_bus_id", "index": "to_index"}), on="to_bus_id")
            .sort("branch_id").with_row_index("index")
            .with_columns(pl.col("tap_ratio").fill_null(1.0), pl.col("is_monitored").fill_null(False), pl.col("is_secured").fill_null(False))
            .select("index", "branch_id", "kind", "from_bus_id", "to_bus_id", "from_index", "to_index", "x_pu", "tap_ratio",
                    "base_limit_mw", "contingency_limit_mw", "is_limited", "is_monitored", "is_secured"))


def _contingencies(core: CoreTables, branches: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Step 4: every contingency as the indexes of the branches it removes, plus the branch ends its split-bus rows move (NAM-09)."""
    outages = core.contingency_outage.join(branches.select("branch_id", pl.col("index").alias("branch_index")), on="branch_id", how="left")
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
    dropped = contingencies.filter(pl.col("is_empty")).select("contingency_id", pl.lit("empty").alias("reason"), "n_dropped", "n_unresolved", "n_other_rows")
    return contingencies.filter(~pl.col("is_empty")), dropped  # RAT-10


def _gtcs(core: CoreTables, kept: pl.DataFrame, branches: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Step 5: each GTC's members with their factors signed in the RAW's from-to; the CSV's direction refers to the member's name, which some CRR transformers list the other way (``is_name_reversed``, NAM-02, NAM-03)."""
    reversed_names = kept.select("branch_id", pl.col("is_name_reversed").fill_null(False))
    sign = (pl.when(flag(pl.col("flow_direction"), "TO")).then(-1.0).otherwise(1.0)
            * pl.when(pl.col("is_name_reversed")).then(-1.0).otherwise(1.0))
    members = (core.gtc_member.join(branches.select("branch_id", pl.col("index").alias("branch_index")), on="branch_id", how="left")
               .join(reversed_names, on="branch_id", how="left")
               .with_columns(pl.col("is_name_reversed").fill_null(False))
               .with_columns((pl.col("factor") * sign).alias("factor"), pl.col("branch_index").is_not_null().alias("_in")))
    counts = members.group_by("gtc_id").agg(pl.col("_in").sum().cast(pl.UInt32).alias("n_members"),
                                            (~pl.col("_in")).sum().cast(pl.UInt32).alias("n_unresolved"))
    gtcs = (core.gtc.select("gtc_id", "source", "limit_mw", "crr_gtc_id").join(counts, on="gtc_id", how="left")
            .with_columns(pl.col("n_members").fill_null(0), pl.col("n_unresolved").fill_null(0))
            .select("gtc_id", "source", "limit_mw", "n_members", "n_unresolved", "crr_gtc_id").sort("gtc_id"))
    return gtcs, members.filter(pl.col("_in")).select("gtc_id", "branch_id", "branch_index", "factor").sort("gtc_id", "branch_id")


def _settlement_points(core: CoreTables, bus_rows: pl.DataFrame, buses: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Step 6: each settlement point's weights on the kept buses, summing to one, and the weight that fell on dropped buses; a weight on a ``bus_key`` several rows share (a CRR tie group with ties kept) is spread equally over them (SP-06)."""
    members = bus_rows.group_by("bus_key").agg(pl.col("bus_id"))
    rows = (core.settlement_point_bus.filter(pl.col("is_resolved")).select("settlement_point_id", "bus_key", "weight")
            .join(members, on="bus_key", how="left").explode("bus_id", empty_as_null=True)
            .with_columns((pl.col("weight") / pl.col("bus_id").count().over("settlement_point_id", "bus_key")).alias("weight"))
            .join(buses.select("bus_id", pl.col("index").alias("bus_index")), on="bus_id", how="left"))
    kept_weight = rows.group_by("settlement_point_id").agg(pl.col("weight").filter(pl.col("bus_index").is_not_null()).sum().alias("_kept"),
                                                           pl.col("weight").filter(pl.col("bus_index").is_null()).sum().alias("weight_dropped"))
    point_buses = (rows.filter(pl.col("bus_index").is_not_null()).join(kept_weight, on="settlement_point_id")
                   .with_columns((pl.col("weight") / pl.col("_kept")).alias("weight"))
                   .select("settlement_point_id", "bus_id", "bus_index", "weight").sort("settlement_point_id", "bus_id"))
    points = (core.settlement_point.select("settlement_point_id", "kind").join(kept_weight, on="settlement_point_id", how="left")
              .join(point_buses.group_by("settlement_point_id").len().rename({"len": "n_buses"}), on="settlement_point_id", how="left")
              .with_columns(pl.col("n_buses").fill_null(0).cast(pl.UInt32), pl.col("weight_dropped").fill_null(0.0))
              .select("settlement_point_id", "kind", "n_buses", "weight_dropped").sort("settlement_point_id"))
    return points, point_buses


def core_tables(session, snapshot_id: str) -> CoreTables:
    """Read one snapshot's rows from every core table."""
    def rows(table: str) -> pl.DataFrame:
        return session.core_rows(table, snapshot_id).drop("snapshot_id")

    bus = rows("bus")
    if bus.is_empty():
        raise KeyError(f"no core rows for {snapshot_id!r}; run build_core()")
    return CoreTables(bus, rows("node"), rows("branch"), rows("branch_rating"), rows("contingency"), rows("contingency_outage"), rows("gtc"), rows("gtc_member"),
                      rows("settlement_point"), rows("settlement_point_bus"))
