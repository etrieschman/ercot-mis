"""Layer 4: the layers tested against what ERCOT published.

Each check reads the archive's own ERCOT reports (prices, binding constraints, awards,
distribution factors) and compares them with what our network and tables say. A check
is a report, never a pass or fail: it writes JSON under ``data/reports/<check>/``.

- ``binding``: the DAM's binding constraints of one hour, resolved onto our network
- ``prices``: published prices rebuilt from shadow prices and our shift factors
- ``flows``: the DAM's awards pushed through our network, against ERCOT's flows
- ``injections``: whether the awards balance every hour
- ``ldf``: ERCOT's load distribution factors against the DAM load file
- ``network``: the DC solve on assembled networks, ties kept and contracted
- ``register``: a measurement for each row of ``docs/assumptions.md``
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl


def write_report(session, check: str, name: str, result: dict) -> Path:
    """Write ``result`` to ``data/reports/<check>/<name>.json`` with the time it was measured."""
    folder = session.data_dir / "reports" / check
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{name.replace(':', '-')}.json"
    path.write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1, default=str))
    path.chmod(0o600)
    return path


def delivery_days(table: pl.LazyFrame) -> set[date]:
    """The operating days of an hourly ERCOT table, from its ``delivery_date`` column (``MM/DD/YYYY``)."""
    return {datetime.strptime(d, "%m/%d/%Y").date() for d in table.select("delivery_date").unique().collect()["delivery_date"]}


def modelled_days(session) -> set[date]:
    """The operating days with a DAM network model in ``core.snapshot``."""
    return set(session.core("snapshot").filter(pl.col("model_kind") == "dam").select("operating_date").unique().collect()["operating_date"])


def quantiles(values: np.ndarray, decimals: int, within: tuple[float, ...] = ()) -> dict | None:
    """A vector of differences summarized: count, absolute p50, p90, p99 and max, the signed median, how many fall within each tolerance."""
    if values.size == 0:
        return None
    a = np.abs(values)
    return {"n": int(a.size), "p50_abs": round(float(np.quantile(a, 0.5)), decimals), "p90_abs": round(float(np.quantile(a, 0.9)), decimals),
            "p99_abs": round(float(np.quantile(a, 0.99)), decimals), "max_abs": round(float(a.max()), decimals),
            "median_signed": round(float(np.median(values)), decimals), **{f"within_{w}": int((a <= w).sum()) for w in within}}


def histogram(values: np.ndarray, bins: list[float]) -> dict:
    """Counts per bin, keyed by each bin's upper edge."""
    counts, _ = np.histogram(values, bins=bins)
    return {f"<{e:g}": int(n) for n, e in zip(counts, bins[1:])}
