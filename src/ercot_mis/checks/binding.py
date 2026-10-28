"""The DAM's binding constraints for one hour, resolved onto our network.

NP4-191-CD lists every constraint that bound in the day-ahead market: a branch or a GTC,
the contingency it bound under, its shadow price, its limit and the flow ERCOT computed.
``resolve`` finds each row's branch (or a GTC's members), the direction the row reads the
branch in (PRC-02), and the network it bound in: the base case, or the base case with the
contingency applied (PRC-05). ``outcome`` says how the row was applied, or why it was not.
The price check and the flow check both read their rows from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache

import polars as pl

from ..clock import TZ, hour_ending_start_expr
from ..core.match import name_key
from ..out.network import Network
from ..out.shift_factors import DcSystem

BASE_CASE = "BASECASE"


@dataclass
class Binding:
    """One binding row and where it lands in our network."""

    constraint: str
    contingency: str
    shadow_price: float
    limit_mw: float | None
    flow_mw: float | None  # ERCOT's constraint_value
    outcome: str
    branch: int | None = None  # index into net.branches; set whenever the name is one of our branches
    sign: float | None = None  # +1 when the row reads the branch from-to, -1 against it
    gtc_members: list[tuple[int, float]] = field(default_factory=list)  # (branch index, signed factor)
    system: DcSystem | None = None  # the network the row bound in; None when the row cannot be applied


@dataclass
class Resolved:
    bindings: list[Binding]
    systems: dict[str, DcSystem]  # one per contingency solved
    gtcs: dict[str, tuple[list[tuple[int, float]], int]]


def hour_rows(session, table: str, start_utc: datetime) -> pl.DataFrame:
    """One hour of a published DAM report, from its latest posting (TOP-11).

    ERCOT's delivery date, hour ending and DST flag become the hour's UTC start (NAM-08).
    """
    day = f"{start_utc.astimezone(TZ):%m/%d/%Y}"
    rows = (session.raw(table).filter(pl.col("delivery_date") == day)
            .with_columns(hour_ending_start_expr(pl.col("delivery_date").str.to_date("%m/%d/%Y"), pl.col("hour_ending"), pl.col("dst_flag")).alias("_start"))
            .filter(pl.col("_start") == start_utc).drop("_start").collect())
    if rows["doc_id"].n_unique() > 1:
        rows = rows.filter(pl.col("doc_id") == session.latest_doc(rows["emil_id"][0], rows["doc_id"].unique()))
    return rows


def resolve(session, snapshot_id: str, net: Network, system: DcSystem, shadow: pl.DataFrame) -> Resolved:
    """Every row of ``shadow`` (one hour of NP4-191-CD) resolved onto ``net``."""
    substation = dict(net.nodes.select("index", name_key(pl.col("substation"))).rows())
    kv = dict(net.nodes.select("index", "kv").rows())
    branch_of = {k: (j, f, t) for k, j, f, t in net.branches.select(name_key(pl.col("branch_id")), "index", "from_index", "to_index").rows()}
    ctg = net.contingencies.select(name_key(pl.col("contingency_id")).alias("k"), "branch_indexes", "split_branch_indexes", "split_ends", "n_unresolved", "n_dropped")
    outaged = {k: b for k, b in ctg.select("k", "branch_indexes").rows()}
    splits = {k: list(zip(i, e)) for k, i, e in ctg.select("k", "split_branch_indexes", "split_ends").rows() if i}
    partial = {k for k, u, d in ctg.select("k", "n_unresolved", "n_dropped").rows() if u or d}
    # Contingencies the network dropped as empty: those with branches it lacks, and those with only non-branch rows.
    dropped = {k: d + u for k, d, u in net.dropped_contingencies.select(name_key(pl.col("contingency_id")), "n_dropped", "n_unresolved").rows()}
    gtcs = borrowed_gtcs(session, snapshot_id, net)
    systems: dict[str, DcSystem] = {}

    bindings = []
    columns = ("constraint_name", "contingency_name", "from_station", "to_station")
    rows = shadow.select(*[name_key(pl.col(c)) for c in columns], "shadow_price", "constraint_limit", "constraint_value", "from_station_kv", "to_station_kv")
    for name, contingency, from_station, to_station, mu, limit, value, from_kv, to_kv in rows.rows():
        row = Binding(name, contingency, mu, limit, value, outcome="")
        bindings.append(row)
        if name in gtcs:
            row.gtc_members, missing = gtcs[name]
            row.outcome, row.system = ("used_gtc_crr_members_incomplete" if missing else "used_gtc_crr_members"), system
            continue
        if name not in branch_of:
            row.outcome = "constraint_not_a_branch"
            continue
        row.branch, f, t = branch_of[name]
        row.sign = row_direction(substation, kv, f, t, from_station, to_station, from_kv, to_kv)
        if row.sign is None:
            row.outcome = "direction_not_recognized"
        elif contingency == BASE_CASE:
            row.outcome, row.system = "used_base_case", system
        elif contingency in dropped:
            # A contingency whose branches our model lacks is a topology discrepancy, recorded and not applied (RAT-10).
            # One with only generator, load or settlement point rows leaves the DC topology as the base case.
            row.outcome, row.system = (("binding_under_contingency_absent_in_model", None) if dropped[contingency]
                                       else ("used_non_topological_contingency", system))
        elif contingency not in outaged:
            row.outcome = "contingency_unknown"
        elif row.branch in outaged[contingency]:
            row.outcome = "constraint_is_outaged_by_its_contingency"
        else:
            if contingency not in systems:
                systems[contingency] = system.outaged(outaged[contingency], splits.get(contingency, ()))
            post = systems[contingency]
            if not (post.connected[f] and post.connected[t]):
                row.outcome = "constraint_islanded_by_its_contingency"
            else:
                row.system = post
                row.outcome = ("used_contingency_partial" if contingency in partial else "used_contingency_islanding" if post.n_islanded
                               else "used_contingency_split_bus" if contingency in splits else "used_contingency")
    return Resolved(bindings, systems, gtcs)


def row_direction(substation: dict, kv: dict, f: int, t: int, from_station: str, to_station: str, from_kv: float, to_kv: float) -> float | None:
    """+1 when a binding row's from-to runs with branch ``f``-``t``, -1 against it, None when its ends are not recognized.

    A row names its ends by substation. Inside one substation the voltages tell them apart, to the tenth:
    ERCOT uses the tenths digit of the base kV to tell bus sections of one level apart.
    """
    if substation[f] != substation[t]:
        ends = (substation[f], substation[t])
        return 1.0 if ends == (from_station, to_station) else -1.0 if ends == (to_station, from_station) else None
    ends, published = (round(kv[f], 1), round(kv[t], 1)), (round(from_kv, 1), round(to_kv, 1))
    if published[0] == published[1] or set(ends) != set(published):
        return None
    return 1.0 if ends == published else -1.0


def borrowed_gtcs(session, snapshot_id: str, net: Network) -> dict[str, tuple[list[tuple[int, float]], int]]:
    """GTCs as signed DAM branch sets, borrowed from the CRR model of the same month (PRC-04).

    The DAM package carries no GTC members; the CRR package does, under the codes the shadow
    price file uses. Each member is translated through ``core.match_branch``, and its factor
    re-signed when the DAM branch runs the other way (ends translated through ``core.match_bus``).
    Returns ``{gtc code: ([(branch index, factor)], members lost)}``.
    """
    day = snapshot_id.split(":")[1]
    crr_ids = session.core("snapshot").filter(pl.col("snapshot_id").str.starts_with(f"crr:monthly:{day[:7]}:")).sort("revision").collect()["snapshot_id"]
    if crr_ids.is_empty():
        return {}
    crr_id = crr_ids[-1]  # the highest revision (TOP-11)
    crr = _network(session, crr_id)
    pairs = session.match_branches(crr_id, snapshot_id).drop_nulls(["crr_branch_id", "dam_branch_id"]).select("crr_branch_id", "dam_branch_id")
    bus_of = dict(session.match_buses(crr_id, snapshot_id).drop_nulls(["crr_bus_key", "dam_bus_key"]).select("crr_bus_key", "dam_bus_key").rows())
    bus = lambda e: e.str.split("@").list.first().replace_strict(bus_of, default=None, return_dtype=pl.String)  # noqa: E731
    rows = (crr.gtc_members.join(crr.branches.select("branch_id", bus(pl.col("from_node_id")).alias("cf"), bus(pl.col("to_node_id")).alias("ct")), on="branch_id", how="left")
            .join(pairs, left_on="branch_id", right_on="crr_branch_id", how="left")
            .join(net.branches.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("index").alias("j"), pl.col("from_node_id").alias("df"), pl.col("to_node_id").alias("dt")),
                  on="dam_branch_id", how="left"))
    declared = dict(crr.gtcs.select(name_key(pl.col("gtc_id")), pl.col("n_members") + pl.col("n_unresolved")).rows())
    out: dict[str, list[tuple[int, float]]] = {}
    for gtc, factor, cf, ct, j, df, dt in rows.select(name_key(pl.col("gtc_id")), "factor", "cf", "ct", "j", "df", "dt").rows():
        members = out.setdefault(gtc, [])
        if j is None:
            continue
        same, crossed = (cf == df) + (ct == dt), (cf == dt) + (ct == df)
        if same != crossed:
            members.append((j, factor if same > crossed else -factor))
    return {gtc: (members, declared.get(gtc, len(members)) - len(members)) for gtc, members in out.items()}


@lru_cache(maxsize=2)
def _network(session, snapshot_id: str) -> Network:
    """The CRR model's network, built once per process: every DAM hour of a month borrows from the same one."""
    return session.network(snapshot_id)

