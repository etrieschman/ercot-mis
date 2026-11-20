"""Binding rows resolved onto our network: the branch or GTC each row names, the direction it reads the branch in, and the network it bound in.

The DAM's shadow price report (NP4-191-CD) and the CRR auction's binding constraints both
list a constraint, the contingency it bound under, its shadow price, its limit and the flow.
``Topology`` keys one network's branches and contingencies the way the rows name them,
``resolve`` walks every row through one ladder for both markets, and ``sensitivity`` turns a
resolved row into its shadow price times its shift factor at every bus. ``resolve_dam`` reads
the DAM report: GTC members borrowed from the CRR month (PRC-04) and the direction from the
row's substations and voltages (PRC-02). The price check, the flow check and the CRR price
check read their rows from here. An ``outcome`` says how a row was applied, or why it was not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

import numpy as np
import polars as pl

from ..clock import TZ, hour_ending_start_expr
from ..core.match import name_key
from ..out.network import Network
from ..out.shift_factors import DcSystem

BASE_CASE = "BASECASE"


@dataclass(frozen=True)
class Binding:
    """One binding row and where it lands in our network."""

    constraint: str
    contingency: str  # keyed name; empty for the base case
    shadow_price: float
    limit_mw: float | None
    flow_mw: float | None  # ERCOT's flow at the constraint
    outcome: str
    branch: int | None = None  # index into net.branches; set whenever the name is one of our branches
    sign: float | None = None  # +1 when the row reads the branch from-to, -1 against it
    gtc_members: tuple[tuple[int, float], ...] = ()  # (branch index, signed factor)
    system: DcSystem | None = None  # the network the row bound in; None when the row cannot be applied


@dataclass(frozen=True)
class Topology:
    """One network's branches and contingencies, keyed as binding rows name them (``core.match.name_key``)."""

    branch_of: dict[str, tuple[int, int, int]]  # branch index, from index, to index
    outaged: dict[str, list[int]]  # the branch indexes a contingency removes
    splits: dict[str, list[tuple[int, str]]]  # the branch ends it moves (NAM-09)
    partial: frozenset[str]  # contingencies with rows the network could not apply
    dropped: dict[str, int]  # contingencies the network dropped as empty: how many branches it lacked (zero: only non-branch rows)


@dataclass(frozen=True)
class Resolved:
    bindings: tuple[Binding, ...]
    systems: dict[str, DcSystem]  # one per contingency solved


# A row: the constraint and contingency as name keys (the base case empty), shadow price, limit, flow, and whatever the market gives as a direction.
Row = tuple[str, str, float, float | None, float | None, object]


def hour_rows(session, table: str, start_utc: datetime) -> pl.DataFrame:
    """One hour of a published DAM report, from its latest posting (TOP-11).

    ERCOT's delivery date, hour ending and DST flag become the hour's UTC start (NAM-08).
    """
    day = f"{start_utc.astimezone(TZ):%m/%d/%Y}"
    rows = session.raw(table).filter(pl.col("delivery_date") == day).collect()
    if rows.is_empty():  # a day ERCOT did not post; polars 1.44 panics on the hour expression over no rows
        return rows
    rows = (rows.with_columns(hour_ending_start_expr(pl.col("delivery_date").str.to_date("%m/%d/%Y"), pl.col("hour_ending"), pl.col("dst_flag")).alias("_start"))
            .filter(pl.col("_start") == start_utc).drop("_start"))
    return session.latest_posting(rows)


def topology(net: Network) -> Topology:
    """``net`` as binding rows name it."""
    ctg = net.contingencies.select(name_key(pl.col("contingency_id")).alias("k"), "branch_indexes", "split_branch_indexes", "split_ends", "n_unresolved", "n_dropped")
    return Topology(
        branch_of={k: (j, f, t) for k, j, f, t in net.branches.select(name_key(pl.col("branch_id")), "index", "from_index", "to_index").rows()},
        outaged=dict(ctg.select("k", "branch_indexes").rows()),
        splits={k: list(zip(i, e)) for k, i, e in ctg.select("k", "split_branch_indexes", "split_ends").rows() if i},
        partial=frozenset(k for k, u, d in ctg.select("k", "n_unresolved", "n_dropped").rows() if u or d),
        dropped={k: d + u for k, d, u in net.dropped_contingencies.select(name_key(pl.col("contingency_id")), "n_dropped", "n_unresolved").rows()},
    )


def resolve(rows: Iterable[Row], topo: Topology, system: DcSystem, gtcs: dict[str, tuple[tuple[tuple[int, float], ...], str]],
            sign_of: Callable[[object, int, int], float | None], systems: dict[str, DcSystem] | None = None) -> Resolved:
    """Every row resolved onto the network ``topo`` describes, with ``system`` its base-case solve.

    ``gtcs`` maps a GTC's key to its signed members and the outcome to record; ``sign_of(direction, f, t)`` reads a row's
    direction against its branch's from and to bus indexes. ``systems`` collects the network under each contingency
    solved; callers that resolve several sets of rows on one topology pass the same dict and solve each contingency once.
    """
    systems = {} if systems is None else systems
    bindings = []
    for constraint, contingency, mu, limit, flow, direction in rows:
        if constraint in gtcs:
            members, outcome = gtcs[constraint]
            bindings.append(Binding(constraint, contingency, mu, limit, flow, outcome, gtc_members=members, system=system))
            continue
        if constraint not in topo.branch_of:
            bindings.append(Binding(constraint, contingency, mu, limit, flow, "not_a_branch"))
            continue
        j, f, t = topo.branch_of[constraint]
        sign = sign_of(direction, f, t)
        outcome, under = ("direction_not_recognized", None) if sign is None else _under(contingency, j, f, t, topo, system, systems)
        bindings.append(Binding(constraint, contingency, mu, limit, flow, outcome, branch=j, sign=sign, system=under))
    return Resolved(tuple(bindings), systems)


def _under(contingency: str, j: int, f: int, t: int, topo: Topology, system: DcSystem, systems: dict[str, DcSystem]) -> tuple[str, DcSystem | None]:
    """The outcome and the network a branch row binds in under ``contingency``."""
    if not contingency:
        return "used_base_case", system
    if contingency in topo.dropped:
        # A contingency whose branches our model lacks is a topology discrepancy, recorded and not applied (RAT-10).
        # One with only generator, load or settlement point rows leaves the DC topology as the base case.
        return ("binding_under_contingency_absent_in_model", None) if topo.dropped[contingency] else ("used_non_topological_contingency", system)
    if contingency not in topo.outaged:
        return "contingency_unknown", None
    if j in topo.outaged[contingency]:
        return "outaged_by_its_contingency", None
    if contingency not in systems:
        systems[contingency] = system.outaged(topo.outaged[contingency], topo.splits.get(contingency, ()))
    post = systems[contingency]
    if not (post.connected[f] and post.connected[t]):
        return "islanded_by_its_contingency", None
    outcome = ("used_contingency_partial" if contingency in topo.partial else "used_contingency_islanding" if post.n_islanded
               else "used_contingency_split_bus" if contingency in topo.splits else "used_contingency")
    return outcome, post


def sensitivity(row: Binding) -> np.ndarray:
    """A resolved row's shadow price times its shift factor at every bus: a GTC's factor-weighted members, or the branch in the row's direction."""
    if row.gtc_members:
        js, factors = zip(*row.gtc_members)
        return row.shadow_price * (np.asarray(factors) @ row.system.shift_factors(list(js)))
    return row.sign * row.shadow_price * row.system.shift_factors([row.branch])[0]


def tally(bindings: Iterable[Binding]) -> dict:
    """Rows and shadow price by outcome, and the share of the shadow price the network applied."""
    rows: dict[str, int] = {}
    mu: dict[str, float] = {}
    used = total = 0.0
    for row in bindings:
        rows[row.outcome] = rows.get(row.outcome, 0) + 1
        mu[row.outcome] = round(mu.get(row.outcome, 0.0) + row.shadow_price, 2)
        total += abs(row.shadow_price)
        if row.system is not None:
            used += abs(row.shadow_price)
    return {"rows_by_outcome": dict(sorted(rows.items())), "shadow_price_by_outcome": dict(sorted(mu.items())),
            "share_of_shadow_price_used": round(used / total, 4) if total else None}


# ------------------------------------------------------------------ the DAM's rows


def resolve_dam(session, snapshot_id: str, net: Network, system: DcSystem, shadow: pl.DataFrame) -> Resolved:
    """Every row of ``shadow`` (one hour of NP4-191-CD) resolved onto ``net``."""
    substation = dict(net.buses.select("index", name_key(pl.col("substation"))).rows())
    kv = dict(net.buses.select("index", "kv").rows())
    columns = ("constraint_name", "contingency_name", "from_station", "to_station")
    rows = [(name, "" if contingency == BASE_CASE else contingency, mu, limit, value, (from_sub, to_sub, from_kv, to_kv))
            for name, contingency, from_sub, to_sub, mu, limit, value, from_kv, to_kv
            in shadow.select(*[name_key(pl.col(c)) for c in columns], "shadow_price", "constraint_limit", "constraint_value", "from_station_kv", "to_station_kv").rows()]

    def sign_of(direction, f: int, t: int) -> float | None:
        return row_direction(substation, kv, f, t, *direction)

    return resolve(rows, topology(net), system, borrowed_gtcs(session, snapshot_id, net), sign_of)


def row_direction(substation: dict, kv: dict, f: int, t: int, from_sub: str, to_sub: str, from_kv: float, to_kv: float) -> float | None:
    """+1 when a binding row reads the branch from ``f`` to ``t``, -1 against it, None when its ends are not recognized; a row names its ends by substation and base kV, whose tenths digit tells bus sections of one level apart (PRC-02)."""
    if substation[f] != substation[t]:
        ends = (substation[f], substation[t])
        return 1.0 if ends == (from_sub, to_sub) else -1.0 if ends == (to_sub, from_sub) else None
    ends, published = (round(kv[f], 1), round(kv[t], 1)), (round(from_kv, 1), round(to_kv, 1))
    if published[0] == published[1] or set(ends) != set(published):
        return None
    return 1.0 if ends == published else -1.0


def borrowed_gtcs(session, snapshot_id: str, net: Network) -> dict[str, tuple[tuple[tuple[int, float], ...], str]]:
    """``{gtc code: (signed DAM members, outcome)}``: each GTC's members borrowed from the CRR model of the same month through ``core.match_branch`` and re-signed through ``core.match_bus``; the outcome says when members were lost (PRC-04)."""
    day = snapshot_id.split(":")[1]
    crr_id = session.latest_snapshot(model_kind="crr_monthly", month=datetime.strptime(day[:7], "%Y-%m").date())
    if crr_id is None:
        return {}
    crr = _network(session, crr_id)
    pairs = session.match_branches(crr_id, snapshot_id).drop_nulls(["crr_branch_id", "dam_branch_id"]).select("crr_branch_id", "dam_branch_id")
    bus_of = dict(session.match_buses(crr_id, snapshot_id).drop_nulls(["crr_bus_key", "dam_bus_key"]).select("crr_bus_key", "dam_bus_key").rows())
    bus = lambda e: e.str.split("@").list.first().replace_strict(bus_of, default=None, return_dtype=pl.String)
    rows = (crr.gtc_members.join(crr.branches.select("branch_id", bus(pl.col("from_bus_id")).alias("cf"), bus(pl.col("to_bus_id")).alias("ct")), on="branch_id", how="left")
            .join(pairs, left_on="branch_id", right_on="crr_branch_id", how="left")
            .join(net.branches.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("index").alias("j"), pl.col("from_bus_id").alias("df"), pl.col("to_bus_id").alias("dt")),
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
    return {gtc: (tuple(members), "used_gtc_crr_members_incomplete" if declared.get(gtc, len(members)) > len(members) else "used_gtc_crr_members")
            for gtc, members in out.items()}


@lru_cache(maxsize=2)
def _network(session, snapshot_id: str) -> Network:
    """The CRR model's network, built once per process: every DAM hour of a month borrows from the same one."""
    return session.network(snapshot_id)
