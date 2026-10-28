"""Build the core layer from raw.

``core/snapshot.parquet`` lists every network model. Then, per archived package, one file
per table in ``TABLES`` at ``core/<table>/emil_id=<EMIL>/<blob16>.parquet``, and per 60-day
disclosure day one ``hourly_award`` file. Tables are computed per snapshot (a DAM hour, a
CRR month) from the package's raw tables and stacked with ``snapshot_id`` first.

Each file is registered in the catalog like a raw artifact. Its key covers the code
(``core_id``), everything else that shapes it (``package_inputs``) and the package bytes,
so a package is rebuilt exactly when one of those changes.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import date
from pathlib import Path

import polars as pl

from ..raw import build as raw_build
from . import award, branch, contingency, gtc, load, node, settlement_point, snapshot

LAYER = "core"
DAM_PRODUCT = snapshot.DAM_PRODUCT
GTL_PRODUCT = "NP3-766-M"

# Bump when the set of tables or how they are assembled changes.
VERSION = 10
TABLES = ("node", "branch", "branch_rating", "contingency", "contingency_outage", "gtc", "gtc_member",
          "settlement_point", "settlement_point_bus", "load")
DAM_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "psse_load", "dam_lines", "dam_transformers", "dam_generators", "dam_loads",
              "dam_settlement_points", "dam_hub_buses", "dam_contingencies")
CRR_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "psse_load", "crr_mapping_autos", "crr_mapping_lines", "crr_sources_and_sinks",
              "crr_monitored_lines_and_transformers", "crr_contingencies", "crr_non_thermal_constraints")
AWARD_TABLES = {"gen": "dam_60d_gen_resource_data", "esr": "dam_60d_esr_data", "energy_only_offers": "dam_60d_energy_only_offer_awards",
                "energy_bids": "dam_60d_energy_bid_awards", "ptp_bids": "dam_60d_ptp_obligation_bid_awards", "ptp_options": "dam_60d_ptp_obligation_option_awards"}
GTL_SCHEMA = {"interval_start_utc": pl.Datetime("us", "UTC"), "gtc_name": pl.String, "market": pl.String, "limit_mw": pl.Float64}


def core_id() -> str:
    """Identity of the code that builds core tables: package and layer versions."""
    from .. import __version__

    parts = [f"core={VERSION}"] + [f"{m.__name__.rsplit('.', 1)[-1]}={m.VERSION}" for m in (node, branch, contingency, gtc, settlement_point, load)]
    return f"ercot-mis={__version__}|" + "|".join(parts)


def build(session, *, limit: int | None = None) -> list[dict]:
    """Write ``core/snapshot.parquet`` and every core table not built yet; one record per package."""
    snaps = snapshot.snapshots(session)
    core_dir = session.data_dir / LAYER
    tmp_dir = core_dir / ".partial"  # outside core/<table>/, so a crash leaves no half file where readers scan
    tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(snaps, core_dir / "snapshot.parquet", tmp_dir)

    existing = session.catalog.artifact_keys(LAYER)
    gtl_days = latest_gtl_by_day(session)
    run_id = session.start_run("build_core")
    results = []
    packages = snaps.select("emil_id", "blob_sha256", "doc_id").unique(maintain_order=True)
    if limit is not None:
        packages = packages.head(limit)
    for emil_id, blob, doc_id in packages.rows():
        record = {"emil_id": emil_id, "doc_id": doc_id, "blob_sha256": blob, "status": "skipped", "tables": len(TABLES), "rows": None, "seconds": 0.0, "error": None}
        package = snaps.filter(pl.col("blob_sha256") == blob)
        gtl = gtl_days.get(package["operating_date"][0], (pl.DataFrame(schema=GTL_SCHEMA), None)) if emil_id == DAM_PRODUCT else None
        identity = f"{core_id()}|{package_inputs(session, emil_id, package, gtl[1] if gtl else None)}"
        keys = {t: raw_build.artifact_key(identity, blob, t) for t in TABLES}
        none_key = raw_build.artifact_key(identity, blob, raw_build.NONE)
        rels = {t: Path(LAYER) / t / f"emil_id={emil_id}" / f"{blob[:16]}.parquet" for t in TABLES}
        if none_key in existing or (set(keys.values()) <= existing and all((session.data_dir / rel).is_file() for rel in rels.values())):
            results.append(record)
            continue
        try:
            tables = package_tables(session, emil_id, blob, package, gtl[0] if gtl else None)
        except FileNotFoundError:
            results.append({**record, "status": "no_raw"})  # build_raw first
            continue
        except Exception as error:
            results.append({**record, "status": "failed", "error": f"{type(error).__name__}: {error}"})
            continue
        if not tables:  # every month filtered out: remember that, or it is rebuilt every run
            session.record_artifacts(run_id, LAYER, emil_id, blob, identity, [(none_key, raw_build.Written(raw_build.NONE, Path(raw_build.NONE), 0, 0, ()), Path(raw_build.NONE))])
            results.append({**record, "status": "built", "tables": 0, "rows": 0})
            continue
        placed = [(keys[t], _write(frame, session.data_dir / rels[t], tmp_dir, t), rels[t]) for t, frame in tables.items()]
        session.record_artifacts(run_id, LAYER, emil_id, blob, identity, placed)
        results.append({**record, "status": "built", "tables": len(placed), "rows": sum(f.height for f in tables.values())})
    results += build_awards(session, existing, tmp_dir, run_id)
    session.finish_run(run_id, failed=sum(1 for r in results if r["status"] == "failed"))
    return results


def package_inputs(session, emil_id: str, snaps: pl.DataFrame, gtl_blob: str | None) -> str:
    """Everything besides the package bytes and the code that shapes a package's core tables, as a cache-key string.

    The raw parser (a parser bump rebuilds core), the GTL document a DAM day's limits come from
    (it may arrive after the models), the GTC name crosswalk, and the package's snapshot ids
    (a revision renumbering rebuilds the rows that carry them).
    """
    parts = [raw_build.parser_id(emil_id)]
    if emil_id == DAM_PRODUCT:
        parts.append(f"gtl={gtl_blob[:16] if gtl_blob else 'none'}")
    crosswalk = session.data_dir / gtc.OVERRIDES
    parts.append(f"xwalk={hashlib.sha256(crosswalk.read_bytes()).hexdigest()[:16] if crosswalk.is_file() else 'none'}")
    parts.append("snapshots=" + ",".join(sorted(snaps["snapshot_id"])))
    return "|".join(parts)


def latest_gtl_by_day(session) -> dict[date, tuple[pl.DataFrame, str]]:
    """Per delivery date: the rows of ERCOT's latest GTL workbook for it (NAM-04, TOP-11) and that workbook's blob."""
    if not (session.data_dir / "raw" / "gtl_hourly").is_dir():
        return {}
    rows = session.raw("gtl_hourly").collect()
    out = {}
    for (day,), day_rows in rows.partition_by("delivery_date", as_dict=True).items():
        latest = day_rows.filter(pl.col("doc_id") == session.latest_doc(GTL_PRODUCT, day_rows["doc_id"].unique()))
        out[day] = (latest.select(GTL_SCHEMA.keys()), latest["blob_sha256"][0])
    return out


def package_tables(session, emil_id: str, blob_sha256: str, snaps: pl.DataFrame, gtl: pl.DataFrame | None = None) -> dict[str, pl.DataFrame]:
    """Core tables for every snapshot of one package, from its raw tables (``gtl``: the day's GTL rows, DAM only)."""
    is_dam = emil_id == DAM_PRODUCT
    raw = {t: _raw(session, t, emil_id, blob_sha256) for t in (DAM_TABLES if is_dam else CRR_TABLES)}
    missing = [t for t, v in raw.items() if v is None]
    if missing:
        raise FileNotFoundError(f"raw tables not built for this package: {missing}")
    by = "hour" if is_dam else "month"
    split = {t: frame.partition_by(by, as_dict=True) for t, frame in raw.items()}
    crosswalk = gtc.name_crosswalk(session.data_dir) if is_dam else None
    gtl = gtl if gtl is not None else pl.DataFrame(schema=GTL_SCHEMA)
    parts: dict[str, list[pl.DataFrame]] = {t: [] for t in TABLES}
    for snap in snaps.iter_rows(named=True):
        r = {t: groups.get((snap[by],), raw[t].clear()) for t, groups in split.items()}
        if is_dam:
            tables = _dam_snapshot(r, gtl.filter(pl.col("interval_start_utc") == snap["interval_start_utc"]), crosswalk)
        elif r["psse_bus"].is_empty():
            continue
        else:
            tables = _crr_snapshot(r)
        for table, frame in tables.items():
            parts[table].append(frame.with_columns(pl.lit(snap["snapshot_id"]).alias("snapshot_id")))
    return {t: pl.concat(frames, how="diagonal_relaxed").select("snapshot_id", pl.exclude("snapshot_id")) for t, frames in parts.items() if frames}


def _dam_snapshot(r: dict[str, pl.DataFrame], gtl: pl.DataFrame, crosswalk: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """One DAM hour's core tables from its raw tables."""
    nodes = node.dam_nodes(r["psse_bus"], r["dam_lines"], r["dam_transformers"], r["dam_generators"], r["dam_loads"], r["dam_settlement_points"])
    branches, ratings = branch.dam_branches(nodes, r["psse_branch"], r["psse_transformer"], r["dam_lines"], r["dam_transformers"])
    contingencies, outages = contingency.dam_contingencies(branches, nodes, r["dam_contingencies"])
    gtcs, members = gtc.dam_gtcs(gtl, crosswalk)
    points, point_buses = settlement_point.dam_settlement_points(nodes, r["dam_settlement_points"], r["dam_hub_buses"], r["dam_loads"], r["dam_generators"])
    return {"node": nodes, "branch": branches, "branch_rating": ratings, "contingency": contingencies, "contingency_outage": outages,
            "gtc": gtcs, "gtc_member": members, "settlement_point": points, "settlement_point_bus": point_buses,
            "load": load.dam_loads(nodes, r["psse_load"], r["dam_loads"])}


def _crr_snapshot(r: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """One CRR month's core tables from its raw tables."""
    nodes = node.crr_nodes(r["psse_bus"], r["psse_branch"], r["psse_transformer"], r["crr_mapping_autos"], r["crr_sources_and_sinks"])
    branches, ratings = branch.crr_branches(nodes, r["psse_branch"], r["psse_transformer"], r["crr_mapping_autos"],
                                            r["crr_monitored_lines_and_transformers"], r["crr_mapping_lines"])
    contingencies, outages = contingency.crr_contingencies(branches, r["crr_contingencies"])
    gtcs, members = gtc.crr_gtcs(branches, r["crr_non_thermal_constraints"])
    points, point_buses = settlement_point.crr_settlement_points(nodes, r["crr_sources_and_sinks"])
    return {"node": nodes, "branch": branches, "branch_rating": ratings, "contingency": contingencies, "contingency_outage": outages,
            "gtc": gtcs, "gtc_member": members, "settlement_point": points, "settlement_point_bus": point_buses,
            "load": load.crr_loads(nodes, r["psse_load"])}


def build_awards(session, existing: set[str], tmp_dir: Path, run_id: str) -> list[dict]:
    """``core.hourly_award`` per 60-day disclosure package (one operating day each)."""
    product = award.DISCLOSURE_PRODUCT
    identity = f"core={VERSION}|award={award.VERSION}|{raw_build.parser_id(product)}"
    results = []
    for package in session.catalog.packages(product):
        blob = package["sha256"]
        key = raw_build.artifact_key(identity, blob, "hourly_award")
        rel = Path(LAYER) / "hourly_award" / f"emil_id={product}" / f"{blob[:16]}.parquet"
        record = {"emil_id": product, "doc_id": package["doc_id"], "blob_sha256": blob, "status": "skipped", "tables": 1, "rows": None, "seconds": 0.0, "error": None}
        if key in existing and (session.data_dir / rel).is_file():
            results.append(record)
            continue
        raw = {name: _raw(session, table, product, blob) for name, table in AWARD_TABLES.items()}
        if any(v is None for v in raw.values()):
            results.append({**record, "status": "no_raw"})
            continue
        try:
            frame = award.hourly_awards(**raw, doc_id=package["doc_id"])
        except Exception as error:
            results.append({**record, "status": "failed", "error": f"{type(error).__name__}: {error}"})
            continue
        session.record_artifacts(run_id, LAYER, product, blob, identity, [(key, _write(frame, session.data_dir / rel, tmp_dir, "hourly_award"), rel)])
        results.append({**record, "status": "built", "rows": frame.height})
    return results


def _raw(session, table: str, emil_id: str, blob_sha256: str) -> pl.DataFrame | None:
    path = session.data_dir / raw_build.artifact_path(table, emil_id, blob_sha256)
    return pl.read_parquet(path) if path.is_file() else None


def _write(frame: pl.DataFrame, dest: Path, tmp_dir: Path, table: str = "") -> raw_build.Written:
    """Write ``frame`` to a temporary file, then move it into place, owner-only."""
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".parquet")
    os.close(handle)
    try:
        frame.write_parquet(tmp, compression="zstd")
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return raw_build.Written(table, dest, frame.height, dest.stat().st_size, ())
