"""The CRR price identity (CRA-01): do our shift factors on the CRR network rebuild the auction's source and sink shadow prices?

The monthly CRR auction clears against the monthly CRR network model (NP7-800-M) and posts
(NP7-803-M) its binding constraints with their shadow prices and the shadow price of every
settlement point, per time-of-use block (Protocols 7.5.3.1). A settlement point's shadow price
is the marginal cost of injecting there: the sum over the binding rows of shadow price times
shift factor, so a PTP Obligation clears at the source's price minus the sink's (CRA-02), and a
linked three-block bid at the hour-weighted mean of the blocks (CRA-04). A binding row's shadow
price is for one MW over the whole block, so the block's hours turn it into dollars per MWh
(CRA-01); the reference bus is not posted, so the median of the fitted prices stands in. A row
names its device and contingency as the CRR model does, and its direction refers to the device
name (CRA-03). The report is one JSON per auction month under ``data/reports/crr_prices/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import polars as pl
from scipy import sparse

from ..clock import block_hours
from ..core.match import name_key
from ..out.network import Network
from ..out.shift_factors import DcSystem
from . import quantiles, weight_matrix, write_report
from .binding import Resolved, Topology, resolve, sensitivity, tally, topology

WITHIN = (0.01, 0.1, 1.0)


@dataclass(frozen=True)
class Model:
    """The monthly CRR model as the auction's rows meet it: one network, its base-case solve, its topology, its GTC members, which transformer names run against the RAW, and the settlement point weights."""

    net: Network
    system: DcSystem
    topo: Topology
    gtcs: dict[str, tuple[tuple[tuple[int, float], ...], str]]
    reversed: dict[str, bool]
    W: sparse.csr_matrix
    points: pl.DataFrame


def months_with_results(session) -> list[date]:
    """Auction months with both monthly results and a monthly CRR model, oldest first."""
    results = set(session.raw("crr_auction_source_sink_shadow_prices").filter(pl.col("auction") == "monthly").select("month").unique().collect()["month"])
    models = set(session.core("snapshot").filter(pl.col("model_kind") == "crr_monthly").select("month").unique().collect()["month"])
    return sorted(results & models)


def results(session, table: str, month: date) -> pl.DataFrame:
    """One monthly auction's rows of a results table, from the latest posting (TOP-11)."""
    return session.latest_posting(session.raw(table).filter((pl.col("auction") == "monthly") & (pl.col("month") == month)).collect())


def model(session, snapshot_id: str) -> Model:
    """The monthly model prepared once for every block: the blocks differ only in their limits, which the price identity never reads."""
    net = session.network(snapshot_id)
    W, points = weight_matrix(net)
    gtcs: dict[str, list[tuple[int, float]]] = {}
    for k, j, factor in net.gtc_members.select(name_key(pl.col("gtc_id")), "branch_index", "factor").rows():
        gtcs.setdefault(k, []).append((j, factor))
    reversed_ = dict(session.core_rows("branch", snapshot_id).select(name_key(pl.col("branch_id")), pl.col("is_name_reversed").fill_null(False)).rows())
    return Model(net, DcSystem(net), topology(net), {k: (tuple(m), "used_gtc") for k, m in gtcs.items()}, reversed_, W, points)


def resolve_crr(binding: pl.DataFrame, m: Model, systems: dict[str, DcSystem]) -> Resolved:
    """Every binding row of one block resolved onto the model: its branch and sign, or its GTC members, and the system it bound in (CRA-03).

    A row's direction refers to the device name, so a transformer named against the RAW flips the sign; the base case has a null contingency.
    """
    rows = [(device, contingency, mu, limit, flow, (direction, m.reversed.get(device, False)))
            for device, direction, contingency, mu, limit, flow in binding.select(
                name_key(pl.col("device_name")), pl.col("direction").str.to_uppercase().str.strip_chars(),
                name_key(pl.col("contingency").fill_null("")), "shadow_price", "limit", "flow").rows()]

    def sign_of(direction, f: int, t: int) -> float | None:
        text, is_reversed = direction
        sign = 1.0 if text.startswith("FROM") else -1.0 if text.startswith("TO") else None
        return None if sign is None else -sign if is_reversed else sign

    return resolve(rows, m.topo, m.system, m.gtcs, sign_of, systems)


def check_block(block: str, hours: int, binding: pl.DataFrame, published: pl.DataFrame, m: Model, systems: dict[str, DcSystem]) -> dict:
    """The price identity for one time-of-use block: every settlement point's posted shadow price against ours (CRA-01)."""
    resolved = resolve_crr(binding, m, systems)
    cost = np.zeros(m.points.height)  # the marginal cost of injecting at each point, per MW over the block
    for row in resolved.bindings:
        if row.system is not None:
            cost += m.W @ sensitivity(row)
    frame = (published.select(pl.col("source_sink").alias("settlement_point_id"), pl.col("shadow_price_per_mwh").alias("price"))
             .join(m.points.with_columns(pl.Series("cost", cost / hours)), on="settlement_point_id")
             .join(m.net.settlement_points.select("settlement_point_id", "kind"), on="settlement_point_id"))
    fitted = frame["price"].to_numpy() - frame["cost"].to_numpy()
    residual = fitted - np.median(fitted)
    by_kind = frame.with_columns(pl.Series("r", np.abs(residual))).group_by("kind").agg(
        pl.len().alias("n"), pl.col("r").median().round(3).alias("p50_abs"), pl.col("r").max().round(2).alias("max_abs")).sort("kind")
    return {"block": block, "hours": hours, "binding_rows": binding.height, **tally(resolved.bindings),
            "points_posted": published.height, "points_compared": frame.height, "reference_offset": round(float(np.median(fitted)), 3),
            "residual": quantiles(residual, 3, within=WITHIN), "residual_by_kind": {r.pop("kind"): r for r in by_kind.to_dicts()}}


def obligation_prices(market_results: pl.DataFrame, published: pl.DataFrame, hours: dict[str, int]) -> dict:
    """Every awarded PTP Obligation's clearing price against its source's shadow price minus its sink's (CRA-02).

    A single-block award clears at the difference in its block; a linked three-block bid (``bid_24_hour``, Protocols
    7.3(7)) clears at the mean of the three blocks' differences weighted by the blocks' hours (CRA-04).
    """
    prices = published.select("source_sink", "time_of_use", pl.col("shadow_price_per_mwh").alias("sp"))
    awards = (market_results.filter(pl.col("hedge_type") == "OBL")
              .join(prices.rename({"source_sink": "source", "sp": "sp_source"}), on=["source", "time_of_use"], how="left")
              .join(prices.rename({"source_sink": "sink", "sp": "sp_sink"}), on=["sink", "time_of_use"], how="left")
              .with_columns((pl.col("sp_source") - pl.col("sp_sink")).alias("block_price"), pl.col("time_of_use").replace_strict(hours, return_dtype=pl.Int64).alias("hours")))
    linked = pl.col("bid_24_hour") == "Yes"
    weighted = (awards.filter(linked).group_by("source", "sink")
                .agg(((pl.col("block_price") * pl.col("hours")).sum() / pl.col("hours").sum()).alias("linked_price")))
    awards = (awards.join(weighted, on=["source", "sink"], how="left")
              .with_columns(pl.when(linked).then(pl.col("linked_price")).otherwise(pl.col("block_price")).alias("expected")))
    out = {"awards": awards.height, "unpriced_ends": int(awards["block_price"].null_count())}
    for label, frame in (("single_block", awards.filter(~linked)), ("linked_three_blocks", awards.filter(linked))):
        d = (frame["shadow_price_per_mwh"] - frame["expected"]).drop_nulls().to_numpy()
        out[label] = quantiles(d, 4, within=WITHIN) if len(d) else {"n": 0}
    return out


def run(session, month: date) -> dict:
    """Every time-of-use block of one monthly auction; writes ``data/reports/crr_prices/<month>.json``."""
    snapshot_id = session.latest_snapshot(model_kind="crr_monthly", month=month)
    hours = block_hours(month)
    binding = results(session, "crr_auction_binding_constraints", month)
    published = results(session, "crr_auction_source_sink_shadow_prices", month)
    m = model(session, snapshot_id)
    systems: dict[str, DcSystem] = {}  # the blocks bind under the same contingencies; each is solved once
    blocks = [check_block(block, hours[block], binding.filter(pl.col("time_of_use") == block), published.filter(pl.col("time_of_use") == block), m, systems)
              for block in sorted(set(binding["time_of_use"]) | set(published["time_of_use"]))]
    result = {"month": str(month), "snapshot_id": snapshot_id, "contingencies_solved": len(systems), "blocks": blocks,
              "obligation_clearing_prices": obligation_prices(results(session, "crr_auction_market_results", month), published, hours)}
    write_report(session, "crr_prices", f"{month:%Y-%m}", result)
    return result


def summary_line(month: date, result: dict) -> str:
    """One log line for an auction month: the residual's typical, tail and worst values over the blocks."""
    blocks = result["blocks"]
    if not blocks:
        return f"crr price check {month:%Y-%m}: no blocks with results and a model"
    r = [b["residual"] for b in blocks]
    share = min((b["share_of_shadow_price_used"] or 1.0) for b in blocks)
    return (f"crr price check {month:%Y-%m}: {len(blocks)} blocks, p50 {max(x['p50_abs'] for x in r):.3f}, p99 {max(x['p99_abs'] for x in r):.2f}, "
            f"max {max(x['max_abs'] for x in r):.2f} $/MWh (worst block), {sum(b['binding_rows'] for b in blocks)} binding rows, "
            f"least share of shadow price applied {share:.2f}")
