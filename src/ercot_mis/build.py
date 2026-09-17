"""The raw layer: parsed rows from archived packages, written once, with provenance.

One Parquet artifact per (archived package, table) at
``raw/<table>/emil_id=<EMIL>/<blob sha256[:16]>.parquet``. Every row carries the
identity of its source: ``emil_id``, ``doc_id``, ``blob_sha256``, ``member_sha256``,
``member_path``, plus the package metadata parsed from member names (CRR: auction,
term, sequence, month, time_of_use; DAM: operating_date, hour).

An artifact's key is a hash of the parser's source code, the package version, the
package bytes and the table name. If the key is in the catalog and the file exists,
the package is skipped; change a parser and every artifact it produced is rebuilt.
Parsing happens in worker processes (a DAM day is 24 models, ~12 s serial); the
catalog is written by the main process only.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .parsers import _common, crr, dam, psse
from .products import Product, get_product

LAYER = "raw"
COMPRESSION = "zstd"
_MODULES = {"NP7-801-M": (crr,), "NP7-800-M": (crr,), "NP4-500-SG": (dam,)}


def parser_id(emil_id: str) -> str:
    """Hash of the parser source that produces a product's raw tables, plus the package version."""
    from . import __version__

    modules = _MODULES.get(emil_id)
    if modules is None:
        raise ValueError(f"{emil_id} has no raw-layer parser")
    digest = hashlib.sha256(__version__.encode())
    for module in (_common, psse, *modules):
        digest.update(inspect.getsource(module).encode())
    return digest.hexdigest()


def artifact_key(parser: str, blob_sha256: str, table: str) -> str:
    return hashlib.sha256(f"{parser}|{blob_sha256}|{table}".encode()).hexdigest()


def artifact_path(table: str, emil_id: str, blob_sha256: str) -> Path:
    """Relative to the data folder; hive-style so readers get ``emil_id`` for free."""
    return Path(LAYER) / table / f"emil_id={emil_id}" / f"{blob_sha256[:16]}.parquet"


@dataclass(frozen=True)
class Written:
    """One artifact written by a worker, not yet registered in the catalog."""

    table: str
    tmp_path: Path
    rows: int
    size_bytes: int
    member_sha256s: tuple[str, ...]


@dataclass(frozen=True)
class PackageResult:
    blob_sha256: str
    artifacts: tuple[Written, ...]
    seconds: float
    error: str | None = None


def _identity_columns(table: pa.Table, values: dict) -> pa.Table:
    """Prepend identity columns; a column the file already has (DAM ``hour``) is kept."""
    for name, value in reversed(list(values.items())):
        if name in table.column_names:
            continue
        if isinstance(value, date):
            array = pa.array([value] * table.num_rows, pa.date32())
        elif isinstance(value, int):
            array = pa.array([value] * table.num_rows, pa.int64())
        else:
            array = pa.array([value] * table.num_rows, pa.string())
        table = table.add_column(0, name, array)
    return table


def parse_package(path: Path, emil_id: str, doc_id: str | None, blob_sha256: str,
                  members: dict[str, str]) -> dict[str, tuple[pa.Table, list[str]]]:
    """Parse every parsed member of a package into per-table Arrow tables with identity columns.

    ``members`` maps member path to member sha256 (from the catalog). Returns
    ``{table: (rows, member hashes that contributed)}``.
    """
    module = _MODULES[emil_id][0]
    parts: dict[str, list[pa.Table]] = {}
    sources: dict[str, list[str]] = {}
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            member = module.classify_member(name)
            if member is None or not member.is_parsed:
                continue
            identity = {"emil_id": emil_id, "doc_id": doc_id, "blob_sha256": blob_sha256,
                        "member_sha256": members.get(name), "member_path": name}
            if module is crr:
                identity.update(auction=member.auction, term=member.term, sequence=member.sequence,
                                month=member.month, time_of_use=member.time_of_use)
            else:
                identity.update(operating_date=member.operating_date, hour=member.hour)
            for table, rows in module.parse_member(member, package.read(name)).items():
                parts.setdefault(table, []).append(_identity_columns(rows, identity))
                sources.setdefault(table, []).append(members.get(name) or "")
    return {table: (pa.concat_tables(chunks, promote_options="default"), sources[table]) for table, chunks in parts.items()}


def build_package(path: Path, emil_id: str, doc_id: str | None, blob_sha256: str,
                  members: dict[str, str], tmp_dir: Path) -> PackageResult:
    """Worker entry point: parse one package and write each table to a temporary Parquet file."""
    started = time.perf_counter()
    try:
        tables = parse_package(path, emil_id, doc_id, blob_sha256, members)
        written = []
        for table, (rows, sources) in tables.items():
            handle, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".parquet")
            os.close(handle)
            pq.write_table(rows, tmp, compression=COMPRESSION)
            written.append(Written(table, Path(tmp), rows.num_rows, Path(tmp).stat().st_size, tuple(sorted(set(sources)))))
    except Exception as error:  # reported per package; other packages continue
        return PackageResult(blob_sha256, (), time.perf_counter() - started, f"{type(error).__name__}: {error}")
    return PackageResult(blob_sha256, tuple(written), time.perf_counter() - started)


def build_raw(mis, product: str | int, *, workers: int | None = None, limit: int | None = None) -> list[dict]:
    """Write the raw tables of every archived package of a product that is not built yet.

    Returns one record per package: ``status`` is ``built``, ``skipped`` or ``failed``.
    """
    spec: Product = get_product(product)
    parser = parser_id(spec.emil_id)
    packages = mis.catalog.packages(spec.emil_id)
    if limit is not None:
        packages = packages[:limit]
    existing = mis.catalog.artifact_keys(LAYER)
    todo, results = [], []
    for package in packages:
        keys = {artifact_key(parser, package["sha256"], t) for t in mis.catalog.artifact_tables(LAYER, package["sha256"])}
        if keys and keys <= existing and all((mis.data_dir / p).is_file() for p in mis.catalog.artifact_paths(keys)):
            results.append({"emil_id": spec.emil_id, "doc_id": package["doc_id"], "blob_sha256": package["sha256"],
                            "status": "skipped", "tables": len(keys), "rows": None, "seconds": 0.0, "error": None})
            continue
        todo.append(package)
    if not todo:
        return results

    run_id = mis._start_run(f"build_raw {spec.emil_id}")
    tmp_dir = mis.data_dir / LAYER / ".partial"
    tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobs = {p["sha256"]: (mis.data_dir / p["path"], spec.emil_id, p["doc_id"], p["sha256"], p["members"], tmp_dir) for p in todo}
    by_sha = {p["sha256"]: p for p in todo}

    def finish(result: PackageResult) -> None:
        package = by_sha[result.blob_sha256]
        record = {"emil_id": spec.emil_id, "doc_id": package["doc_id"], "blob_sha256": result.blob_sha256,
                  "status": "failed" if result.error else "built", "tables": len(result.artifacts),
                  "rows": sum(a.rows for a in result.artifacts), "seconds": round(result.seconds, 1), "error": result.error}
        if not result.error:
            placed = []
            for artifact in result.artifacts:
                rel = artifact_path(artifact.table, spec.emil_id, result.blob_sha256)
                dest = mis.data_dir / rel
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(artifact.tmp_path, 0o600)
                os.replace(artifact.tmp_path, dest)
                placed.append((artifact_key(parser, result.blob_sha256, artifact.table), artifact, rel))
            mis._add_artifacts(run_id, LAYER, spec.emil_id, result.blob_sha256, parser, placed)
        results.append(record)

    workers = workers or (1 if len(todo) == 1 else min(os.cpu_count() or 2, 6))
    if workers == 1:
        for args in jobs.values():
            finish(build_package(*args))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(build_package, *args) for args in jobs.values()]
            for future in as_completed(futures):
                finish(future.result())
    mis._finish_run(run_id, failed=sum(1 for r in results if r["status"] == "failed"))
    return results
