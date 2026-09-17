"""Core tables built from raw: snapshots and nodes.

``core.snapshot``: one row per network model, with the snapshot IDs the rest of the
core layer is keyed by (``dam:2026-10-14:he07:r1``, ``crr:monthly:2026-10:r1``,
``crr:annual:2029.1st6:seq6:2029-01:r2``). The revision ``r<n>`` orders packages that
describe the same logical model by posting time: a DAM operating day, a CRR month, an
annual term and sequence (``_Upd`` packages are revisions).

``core.node``: one row per RAW bus per snapshot with its equipment-based ``node_key``
(see ``core/identity.py``), written per package under ``core/node/emil_id=<EMIL>/``.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq

from .. import build as raw_build
from ..parsers import crr, dam
from . import identity

LAYER = "core"
CRR_PRODUCTS = ("NP7-801-M", "NP7-800-M")
DAM_PRODUCT = "NP4-500-SG"


def core_id() -> str:
    from .. import __version__

    digest = hashlib.sha256(__version__.encode())
    for module in (identity, inspect.getmodule(core_id)):
        digest.update(inspect.getsource(module).encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class Logical:
    """What a package describes, from its member names."""

    kind: str  # "dam", "monthly", "annual"
    key: tuple  # groups packages that are revisions of one another
    months: tuple[date, ...]  # CRR months inside (annual: six); DAM: ()
    day: date | None  # DAM operating day


def logical(emil_id: str, member_paths: list[str]) -> Logical | None:
    if emil_id == DAM_PRODUCT:
        days = {m.operating_date for n in member_paths if (m := dam.classify_member(n)) and m.operating_date}
        return Logical("dam", (min(days),), (), min(days)) if days else None
    members = [m for n in member_paths if (m := crr.classify_member(n)) and m.month]
    if not members:
        return None
    months = tuple(sorted({m.month for m in members}))
    if any(m.auction == "annual" for m in members):
        first = next(m for m in members if m.auction == "annual")
        return Logical("annual", (first.term, first.sequence), months, None)
    return Logical("monthly", (months[0],), months, None)


def snapshots(mis) -> pl.DataFrame:
    """Every snapshot the archive holds, with revision numbers; one row per model."""
    rows = []
    for emil_id in (*CRR_PRODUCTS, DAM_PRODUCT):
        packages = mis.catalog.packages(emil_id)
        described = [(p, logical(emil_id, list(p["members"]))) for p in packages]
        described = [(p, l) for p, l in described if l is not None]
        by_key: dict[tuple, list] = {}
        for p, l in described:
            by_key.setdefault((l.kind, l.key), []).append((p, l))
        for (kind, key), group in by_key.items():
            ordered = sorted(group, key=lambda pl_: (pl_[0]["posted_at"] is None, pl_[0]["posted_at"] or 0, pl_[0]["sha256"]))
            for revision, (p, l) in enumerate(ordered, start=1):
                base = {"emil_id": emil_id, "doc_id": p["doc_id"], "blob_sha256": p["sha256"], "posted_at": p["posted_at"],
                        "model_kind": kind, "revision": revision}
                if kind == "dam":
                    hours = sorted({m.hour for n in p["members"] if (m := dam.classify_member(n)) and m.kind == "network_model" and m.hour})
                    for hour in hours:
                        rows.append({**base, "snapshot_id": f"dam:{l.day}:he{hour:02d}:r{revision}", "operating_date": l.day,
                                     "hour": hour, "month": None, "term": None, "sequence": None})
                else:
                    for month in l.months:
                        term, sequence = (key if kind == "annual" else (None, None))
                        prefix = f"crr:annual:{term}:seq{sequence}" if kind == "annual" else "crr:monthly"
                        rows.append({**base, "snapshot_id": f"{prefix}:{month:%Y-%m}:r{revision}", "operating_date": None,
                                     "hour": None, "month": month, "term": term, "sequence": sequence})
    schema = {"snapshot_id": pl.String, "emil_id": pl.String, "doc_id": pl.String, "blob_sha256": pl.String,
              "posted_at": pl.Datetime("us", "UTC"), "model_kind": pl.String, "revision": pl.Int64,
              "operating_date": pl.Date, "hour": pl.Int64, "month": pl.Date, "term": pl.String, "sequence": pl.Int64}
    return pl.DataFrame(rows, schema=schema).sort("snapshot_id") if rows else pl.DataFrame(schema=schema)


def _raw(mis, table: str, emil_id: str, blob_sha256: str) -> pl.DataFrame | None:
    path = mis.data_dir / raw_build.artifact_path(table, emil_id, blob_sha256)
    return pl.read_parquet(path) if path.is_file() else None


def package_nodes(mis, emil_id: str, blob_sha256: str, snaps: pl.DataFrame) -> pl.DataFrame:
    """``core.node`` rows for every snapshot of one package, read from its raw artifacts."""
    parts = []
    if emil_id == DAM_PRODUCT:
        tables = {t: _raw(mis, t, emil_id, blob_sha256) for t in
                  ("psse_bus", "dam_lines", "dam_transformers", "dam_generators", "dam_loads", "dam_settlement_points")}
        missing = [t for t, v in tables.items() if v is None]
        if missing:
            raise FileNotFoundError(f"raw tables not built for this package: {missing}")
        for snap in snaps.iter_rows(named=True):
            hour = snap["hour"]
            nodes = identity.dam_nodes(*(tables[t].filter(pl.col("hour") == hour) for t in
                                         ("psse_bus", "dam_lines", "dam_transformers", "dam_generators", "dam_loads", "dam_settlement_points")))
            parts.append(nodes.with_columns(pl.lit(snap["snapshot_id"]).alias("snapshot_id")))
    else:
        tables = {t: _raw(mis, t, emil_id, blob_sha256) for t in
                  ("psse_bus", "psse_branch", "psse_transformer", "crr_mapping_autos", "crr_sources_and_sinks")}
        missing = [t for t, v in tables.items() if v is None]
        if missing:
            raise FileNotFoundError(f"raw tables not built for this package: {missing}")
        for snap in snaps.iter_rows(named=True):
            month = snap["month"]
            by_month = {t: tables[t].filter(pl.col("month") == month) for t in tables}
            if by_month["psse_bus"].is_empty():
                continue
            nodes = identity.crr_nodes(by_month["psse_bus"], by_month["psse_branch"], by_month["psse_transformer"],
                                       by_month["crr_mapping_autos"], by_month["crr_sources_and_sinks"])
            parts.append(nodes.with_columns(pl.lit(snap["snapshot_id"]).alias("snapshot_id")))
    if not parts:
        return pl.DataFrame()
    frame = pl.concat(parts, how="diagonal_relaxed")
    return frame.select("snapshot_id", pl.exclude("snapshot_id"))


def build_core(mis, *, limit: int | None = None) -> pl.DataFrame:
    """Write ``core/snapshot.parquet`` and ``core/node/...`` for every package with raw tables."""
    snaps = snapshots(mis)
    core_dir = mis.data_dir / LAYER
    core_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    snaps.write_parquet(core_dir / "snapshot.parquet", compression="zstd")
    os.chmod(core_dir / "snapshot.parquet", 0o600)

    version = core_id()
    existing = mis.catalog.artifact_keys(LAYER)
    results = []
    packages = snaps.select("emil_id", "blob_sha256", "doc_id").unique(maintain_order=True)
    if limit is not None:
        packages = packages.head(limit)
    run_id = None
    for emil_id, blob, doc_id in packages.rows():
        key = raw_build.artifact_key(version, blob, "node")
        rel = Path(LAYER) / "node" / f"emil_id={emil_id}" / f"{blob[:16]}.parquet"
        record = {"emil_id": emil_id, "doc_id": doc_id, "blob_sha256": blob, "status": "skipped", "tables": 1, "rows": None, "seconds": 0.0, "error": None}
        if key in existing and (mis.data_dir / rel).is_file():
            results.append(record)
            continue
        try:
            nodes = package_nodes(mis, emil_id, blob, snaps.filter(pl.col("blob_sha256") == blob))
        except FileNotFoundError:
            results.append({**record, "status": "no_raw"})  # build_raw first
            continue
        except Exception as error:
            results.append({**record, "status": "failed", "error": f"{type(error).__name__}: {error}"})
            continue
        run_id = run_id or mis._start_run("build_core")
        dest = mis.data_dir / rel
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".parquet")
        os.close(handle)
        nodes.write_parquet(tmp, compression="zstd")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        written = raw_build.Written("node", dest, nodes.height, dest.stat().st_size, ())
        mis._add_artifacts(run_id, LAYER, emil_id, blob, version, [(key, written, rel)])
        results.append({**record, "status": "built", "rows": nodes.height})
    if run_id:
        mis._finish_run(run_id, failed=sum(1 for r in results if r["status"] == "failed"))
    return results
