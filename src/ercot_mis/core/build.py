"""Build the core layer from raw: ``core/snapshot.parquet`` and, per package,
``core/<table>/emil_id=<EMIL>/<blob16>.parquet`` for ``node``, ``branch`` and
``branch_rating``.

Each table is registered in the catalog like a raw artifact (key = ``VERSION``s, the
package version and the blob). Tables are computed per snapshot (a DAM hour, a CRR
month) from that package's raw tables and stacked with ``snapshot_id`` first.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import polars as pl

from ..raw import build as raw_build
from . import branch, contingency, gtc, node, snapshot

LAYER = "core"
DAM_PRODUCT = snapshot.DAM_PRODUCT

# Bump when the set of tables or how they are assembled changes.
VERSION = 4
TABLES = ("node", "branch", "branch_rating", "contingency", "contingency_outage", "gtc", "gtc_member")


def core_id() -> str:
    """Identity of the code that builds core tables: package and layer versions."""
    from .. import __version__

    parts = [f"core={VERSION}"] + [f"{m.__name__.rsplit('.', 1)[-1]}={m.VERSION}" for m in (node, branch, contingency, gtc)]
    return f"ercot-mis={__version__}|" + "|".join(parts)


def _raw(session, table: str, emil_id: str, blob_sha256: str) -> pl.DataFrame | None:
    path = session.data_dir / raw_build.artifact_path(table, emil_id, blob_sha256)
    return pl.read_parquet(path) if path.is_file() else None


DAM_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "dam_lines", "dam_transformers", "dam_generators", "dam_loads",
              "dam_settlement_points", "dam_contingencies")
CRR_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "crr_mapping_autos", "crr_sources_and_sinks",
              "crr_monitored_lines_and_transformers", "crr_contingencies", "crr_non_thermal_constraints")


def _gtl_for_day(session, day) -> pl.DataFrame:
    """The latest GTL workbook rows for a delivery date, or an empty frame when none is archived."""
    folder = session.data_dir / "raw" / "gtl_hourly"
    if not folder.is_dir():
        return pl.DataFrame(schema={"hour_ending": pl.Int64, "gtc_name": pl.String, "market": pl.String, "limit_mw": pl.Float64})
    rows = session.raw("gtl_hourly").filter(pl.col("delivery_date") == day).collect()
    if rows.is_empty():
        return rows.select("hour_ending", "gtc_name", "market", "limit_mw")
    latest = rows.filter(pl.col("doc_id") == rows.sort("doc_id")["doc_id"][-1])  # doc IDs grow with posting time
    return latest.select("hour_ending", "gtc_name", "market", "limit_mw")


def package_tables(session, emil_id: str, blob_sha256: str, snaps: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Core tables for every snapshot of one package, read from its raw artifacts."""
    names = DAM_TABLES if emil_id == DAM_PRODUCT else CRR_TABLES
    raw = {t: _raw(session, t, emil_id, blob_sha256) for t in names}
    missing = [t for t, v in raw.items() if v is None]
    if missing:
        raise FileNotFoundError(f"raw tables not built for this package: {missing}")
    if emil_id == DAM_PRODUCT:
        day = snaps["operating_date"][0]
        gtl = _gtl_for_day(session, day)
        crosswalk = gtc.name_crosswalk(session.data_dir)
    parts: dict[str, list[pl.DataFrame]] = {t: [] for t in TABLES}
    for snap in snaps.iter_rows(named=True):
        if emil_id == DAM_PRODUCT:
            r = {t: raw[t].filter(pl.col("hour") == snap["hour"]) for t in names}
            nodes = node.dam_nodes(r["psse_bus"], r["dam_lines"], r["dam_transformers"], r["dam_generators"], r["dam_loads"], r["dam_settlement_points"])
            branches, ratings = branch.dam_branches(nodes, r["psse_branch"], r["psse_transformer"], r["dam_lines"], r["dam_transformers"])
            contingencies, outages = contingency.dam_contingencies(branches, nodes, r["dam_contingencies"])
            gtcs, members = gtc.dam_gtcs(gtl.filter(pl.col("hour_ending") == snap["hour"]), crosswalk)
        else:
            r = {t: raw[t].filter(pl.col("month") == snap["month"]) for t in names}
            if r["psse_bus"].is_empty():
                continue
            nodes = node.crr_nodes(r["psse_bus"], r["psse_branch"], r["psse_transformer"], r["crr_mapping_autos"], r["crr_sources_and_sinks"])
            branches, ratings = branch.crr_branches(nodes, r["psse_branch"], r["psse_transformer"], r["crr_mapping_autos"], r["crr_monitored_lines_and_transformers"])
            contingencies, outages = contingency.crr_contingencies(branches, r["crr_contingencies"])
            gtcs, members = gtc.crr_gtcs(branches, r["crr_non_thermal_constraints"])
        for table, frame in (("node", nodes), ("branch", branches), ("branch_rating", ratings), ("contingency", contingencies),
                             ("contingency_outage", outages), ("gtc", gtcs), ("gtc_member", members)):
            parts[table].append(frame.with_columns(pl.lit(snap["snapshot_id"]).alias("snapshot_id")))
    return {table: pl.concat(frames, how="diagonal_relaxed").select("snapshot_id", pl.exclude("snapshot_id"))
            for table, frames in parts.items() if frames}  # empty GTC frames still carry the schema


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
        keys = {t: raw_build.artifact_key(version, blob, t) for t in TABLES}
        rels = {t: Path(LAYER) / t / f"emil_id={emil_id}" / f"{blob[:16]}.parquet" for t in TABLES}
        record = {"emil_id": emil_id, "doc_id": doc_id, "blob_sha256": blob, "status": "skipped", "tables": len(TABLES), "rows": None, "seconds": 0.0, "error": None}
        if set(keys.values()) <= existing and all((session.data_dir / rel).is_file() for rel in rels.values()):
            results.append(record)
            continue
        try:
            tables = package_tables(session, emil_id, blob, snaps.filter(pl.col("blob_sha256") == blob))
        except FileNotFoundError:
            results.append({**record, "status": "no_raw"})  # build_raw first
            continue
        except Exception as error:
            results.append({**record, "status": "failed", "error": f"{type(error).__name__}: {error}"})
            continue
        run_id = run_id or session._start_run("build_core")
        placed = []
        for table, frame in tables.items():
            dest = session.data_dir / rels[table]
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            handle, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".parquet")
            os.close(handle)
            frame.write_parquet(tmp, compression="zstd")
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
            placed.append((keys[table], raw_build.Written(table, dest, frame.height, dest.stat().st_size, ()), rels[table]))
        session._add_artifacts(run_id, LAYER, emil_id, blob, version, placed)
        results.append({**record, "status": "built", "tables": len(placed), "rows": sum(f.height for f in tables.values())})
    if run_id:
        session._finish_run(run_id, failed=sum(1 for r in results if r["status"] == "failed"))
    return results
