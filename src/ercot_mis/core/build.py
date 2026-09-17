"""Build the core layer from raw: ``core/snapshot.parquet`` and ``core/node/...``.

``core.node`` holds one row per RAW bus per snapshot with its equipment-based
``node_key`` (``core/node.py``), written per package under
``core/node/emil_id=<EMIL>/<blob16>.parquet`` and registered in the catalog like a
raw artifact (key = hash of ``VERSION``, the package version and the blob).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import polars as pl

from ..raw import build as raw_build
from . import node, snapshot

LAYER = "core"
DAM_PRODUCT = snapshot.DAM_PRODUCT

# Bump when core.snapshot or core.node change (columns, keys, contraction rules).
VERSION = 1


def core_id() -> str:
    """Identity of the code that builds core tables: package and layer versions."""
    from .. import __version__

    return f"ercot-mis={__version__}|core={VERSION}|node={node.VERSION}"


def _raw(session, table: str, emil_id: str, blob_sha256: str) -> pl.DataFrame | None:
    path = session.data_dir / raw_build.artifact_path(table, emil_id, blob_sha256)
    return pl.read_parquet(path) if path.is_file() else None


def package_nodes(session, emil_id: str, blob_sha256: str, snaps: pl.DataFrame) -> pl.DataFrame:
    """``core.node`` rows for every snapshot of one package, read from its raw artifacts."""
    parts = []
    if emil_id == DAM_PRODUCT:
        tables = {t: _raw(session, t, emil_id, blob_sha256) for t in
                  ("psse_bus", "dam_lines", "dam_transformers", "dam_generators", "dam_loads", "dam_settlement_points")}
        missing = [t for t, v in tables.items() if v is None]
        if missing:
            raise FileNotFoundError(f"raw tables not built for this package: {missing}")
        for snap in snaps.iter_rows(named=True):
            hour = snap["hour"]
            nodes = node.dam_nodes(*(tables[t].filter(pl.col("hour") == hour) for t in
                                         ("psse_bus", "dam_lines", "dam_transformers", "dam_generators", "dam_loads", "dam_settlement_points")))
            parts.append(nodes.with_columns(pl.lit(snap["snapshot_id"]).alias("snapshot_id")))
    else:
        tables = {t: _raw(session, t, emil_id, blob_sha256) for t in
                  ("psse_bus", "psse_branch", "psse_transformer", "crr_mapping_autos", "crr_sources_and_sinks")}
        missing = [t for t, v in tables.items() if v is None]
        if missing:
            raise FileNotFoundError(f"raw tables not built for this package: {missing}")
        for snap in snaps.iter_rows(named=True):
            month = snap["month"]
            by_month = {t: tables[t].filter(pl.col("month") == month) for t in tables}
            if by_month["psse_bus"].is_empty():
                continue
            nodes = node.crr_nodes(by_month["psse_bus"], by_month["psse_branch"], by_month["psse_transformer"],
                                       by_month["crr_mapping_autos"], by_month["crr_sources_and_sinks"])
            parts.append(nodes.with_columns(pl.lit(snap["snapshot_id"]).alias("snapshot_id")))
    if not parts:
        return pl.DataFrame()
    frame = pl.concat(parts, how="diagonal_relaxed")
    return frame.select("snapshot_id", pl.exclude("snapshot_id"))


def build(session, *, limit: int | None = None) -> list[dict]:
    """Write ``core/snapshot.parquet`` and ``core/node/...`` for every package with raw tables."""
    snaps = snapshot.snapshots(session)
    core_dir = session.data_dir / LAYER
    core_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    snaps.write_parquet(core_dir / "snapshot.parquet", compression="zstd")
    os.chmod(core_dir / "snapshot.parquet", 0o600)

    version = core_id()
    existing = session.catalog.artifact_keys(LAYER)
    results = []
    packages = snaps.select("emil_id", "blob_sha256", "doc_id").unique(maintain_order=True)
    if limit is not None:
        packages = packages.head(limit)
    run_id = None
    for emil_id, blob, doc_id in packages.rows():
        key = raw_build.artifact_key(version, blob, "node")
        rel = Path(LAYER) / "node" / f"emil_id={emil_id}" / f"{blob[:16]}.parquet"
        record = {"emil_id": emil_id, "doc_id": doc_id, "blob_sha256": blob, "status": "skipped", "tables": 1, "rows": None, "seconds": 0.0, "error": None}
        if key in existing and (session.data_dir / rel).is_file():
            results.append(record)
            continue
        try:
            nodes = package_nodes(session, emil_id, blob, snaps.filter(pl.col("blob_sha256") == blob))
        except FileNotFoundError:
            results.append({**record, "status": "no_raw"})  # build_raw first
            continue
        except Exception as error:
            results.append({**record, "status": "failed", "error": f"{type(error).__name__}: {error}"})
            continue
        run_id = run_id or session._start_run("build_core")
        dest = session.data_dir / rel
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".parquet")
        os.close(handle)
        nodes.write_parquet(tmp, compression="zstd")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        written = raw_build.Written("node", dest, nodes.height, dest.stat().st_size, ())
        session._add_artifacts(run_id, LAYER, emil_id, blob, version, [(key, written, rel)])
        results.append({**record, "status": "built", "rows": nodes.height})
    if run_id:
        session._finish_run(run_id, failed=sum(1 for r in results if r["status"] == "failed"))
    return results
