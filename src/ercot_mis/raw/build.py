"""The raw build: every archived package parsed into Parquet, one file per package and table, each row carrying its source.

A file lives at ``raw/<table>/emil_id=<EMIL>/<blob sha256[:16]>.parquet``. Every row carries
``emil_id``, ``doc_id``, ``blob_sha256``, ``member_sha256``, ``member_path`` and what the member's
name says (CRR: auction, term, sequence, month, time_of_use; DAM: operating_date, hour). A file's
key hashes the parser versions, the package bytes and the table name: a key in the catalog with
its file in place is skipped, and a version bump rebuilds every file it produced. Workers parse;
the main process alone writes the catalog (CLAUDE.md, "Provenance").
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from polars.exceptions import PanicException

from ..products import Product, get_product
from . import crr, crr_auction, dam, disclosure, gtl, mappings, prices, psse, table

LAYER = "raw"
COMPRESSION = "zstd"
NONE = "<none>"  # the artifact row of a package that yields no table (a document shape the parser archives but does not read)
# The parser of each product, in build order; each module has VERSION, classify_member and parse_member.
PARSERS = {"NP7-801-M": crr, "NP7-800-M": crr, "NP7-803-M": crr_auction, "NP7-802-M": crr_auction, "NP4-500-SG": dam, "NP3-766-M": gtl,
           "NP4-191-CD": prices, "NP4-183-CD": prices, "NP4-190-CD": prices, "NP4-523-CD": prices, "NP4-200-CD": prices, "NP4-158-SG": prices,
           "NP4-231-CD": prices, "NP4-159-CD": prices, "NP4-160-SG": mappings, "NP3-220-SG": mappings, "NP3-966-ER": disclosure}


def parser_id(emil_id: str) -> str:
    """The identity of the code behind a product's raw tables: the package version and every parser version."""
    from .. import __version__

    if emil_id not in PARSERS:
        raise ValueError(f"{emil_id} has no raw-layer parser")
    parts = [f"ercot-mis={__version__}", f"build={VERSION}"] + [f"{m.__name__.rsplit('.', 1)[-1]}={m.VERSION}" for m in (table, psse, PARSERS[emil_id])]
    return "|".join(parts)


def artifact_key(parser: str, blob_sha256: str, table: str) -> str:
    return hashlib.sha256(f"{parser}|{blob_sha256}|{table}".encode()).hexdigest()


def artifact_path(layer: str, table: str, emil_id: str, blob_sha256: str) -> Path:
    """Where a package's table lives in a layer, relative to the data folder; the hive folder gives readers ``emil_id``."""
    return Path(layer) / table / f"emil_id={emil_id}" / f"{blob_sha256[:16]}.parquet"


@dataclass(frozen=True)
class Written:
    """One file a worker wrote, not yet registered in the catalog."""

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


# Identity columns are typed by name so a null (a monthly package's sequence, a daily file's hour) keeps the type.
IDENTITY_TYPES = {"sequence": pa.int64(), "hour": pa.int64(), "month": pa.date32(), "operating_date": pa.date32()}
VERSION = 2  # bump when identity columns or the artifact layout change


def _identity_columns(table: pa.Table, values: dict) -> pa.Table:
    """The table with the identity columns in front; a column the file already has (the DAM ``hour``) is kept."""
    for name, value in reversed(list(values.items())):
        if name in table.column_names:
            continue
        table = table.add_column(0, name, pa.repeat(pa.scalar(value, IDENTITY_TYPES.get(name, pa.string())), table.num_rows))
    return table


def parse_package(path: Path, emil_id: str, doc_id: str | None, blob_sha256: str,
                  members: dict[str, str], operating_date=None) -> dict[str, tuple[pa.Table, list[str]]]:
    """Every table of one package, as ``{table: (rows with identity columns, the member hashes behind them)}``.

    ``members`` maps member path to member sha256. A document that is not a zip (the GTL workbook) is parsed whole.
    """
    module = PARSERS[emil_id]
    package = {"emil_id": emil_id, "doc_id": doc_id, "blob_sha256": blob_sha256}
    parts: dict[str, list[pa.Table]] = {}
    sources: dict[str, list[str]] = {}
    for member, data, member_sha256 in _parsed_members(path, module, members, operating_date, blob_sha256):
        identity = {**package, "member_sha256": member_sha256, "member_path": member.path, **member.identity}
        for table_name, rows in module.parse_member(member, data).items():
            parts.setdefault(table_name, []).append(_identity_columns(rows, identity))
            sources.setdefault(table_name, []).append(member_sha256 or "")
    return {table_name: (pa.concat_tables(chunks, promote_options="default"), sources[table_name]) for table_name, chunks in parts.items()}


def _parsed_members(path: Path, module, members: dict[str, str], operating_date, blob_sha256: str):
    """(member, bytes, member sha256) for every member a parser reads: the zip's files, or the whole document."""
    if module is gtl:
        data = path.read_bytes()
        member = gtl.classify_document("<document>", data, operating_date)
        if member.is_parsed:
            yield member, data, blob_sha256
        return
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            member = module.classify_member(name)
            if member is not None and member.is_parsed:
                yield member, package.read(name), members.get(name)


def build_package(path: Path, emil_id: str, doc_id: str | None, blob_sha256: str,
                  members: dict[str, str], tmp_dir: Path, operating_date=None) -> PackageResult:
    """One package parsed and each table written to a temporary Parquet file, or the error; runs in a worker."""
    started = time.perf_counter()
    try:
        tables = parse_package(path, emil_id, doc_id, blob_sha256, members, operating_date)
        written = []
        for table_name, (rows, sources) in tables.items():
            handle, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".parquet")
            os.close(handle)
            pq.write_table(rows, tmp, compression=COMPRESSION)
            written.append(Written(table_name, Path(tmp), rows.num_rows, Path(tmp).stat().st_size, tuple(sorted(set(sources)))))
    except (Exception, PanicException) as error:  # a polars panic is not an Exception; reported per package; other packages continue
        return PackageResult(blob_sha256, (), time.perf_counter() - started, f"{type(error).__name__}: {error}")
    return PackageResult(blob_sha256, tuple(written), time.perf_counter() - started)


def build(session, product: str | int, *, workers: int | None = None, limit: int | None = None) -> list[dict]:
    """One record per archived package of a product, ``status`` built, skipped or failed; a skipped package's tables are already in place."""
    spec: Product = get_product(product)
    parser = parser_id(spec.emil_id)
    packages = session.catalog.packages(spec.emil_id)
    if limit is not None:
        packages = packages[:limit]
    # A package is built when this parser wrote every table the catalog has for it and each file is in place.
    built = {blob: rows for (blob,), rows in session.catalog.artifacts(LAYER).partition_by("blob_sha256", as_dict=True).items()}
    todo, results = [], []
    for package in packages:
        rows = built.get(package["sha256"])
        current = rows is not None and all(key == artifact_key(parser, package["sha256"], table) and (path == NONE or (session.data_dir / path).is_file())
                                           for key, table, path in rows.select("artifact_key", "table_name", "path").rows())
        if current:
            results.append({"emil_id": spec.emil_id, "doc_id": package["doc_id"], "blob_sha256": package["sha256"],
                            "status": "skipped", "tables": rows.height, "rows": None, "seconds": 0.0, "error": None})
            continue
        todo.append(package)
    if not todo:
        return results

    run_id = session.start_run(f"build_raw {spec.emil_id}")
    tmp_dir = session.data_dir / LAYER / ".partial"
    tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobs = {p["sha256"]: (session.data_dir / p["path"], spec.emil_id, p["doc_id"], p["sha256"], p["members"], tmp_dir, p["operating_date"]) for p in todo}
    by_sha = {p["sha256"]: p for p in todo}

    def finish(result: PackageResult) -> None:
        package = by_sha[result.blob_sha256]
        record = {"emil_id": spec.emil_id, "doc_id": package["doc_id"], "blob_sha256": result.blob_sha256,
                  "status": "failed" if result.error else "built", "tables": len(result.artifacts),
                  "rows": sum(a.rows for a in result.artifacts), "seconds": round(result.seconds, 1), "error": result.error}
        if not result.error:
            placed = []
            if not result.artifacts:  # nothing to read in this document: remember that, or it is parsed again every run
                placed.append((artifact_key(parser, result.blob_sha256, NONE), Written(NONE, Path(NONE), 0, 0, ()), Path(NONE)))
            for artifact in result.artifacts:
                rel = artifact_path(LAYER, artifact.table, spec.emil_id, result.blob_sha256)
                dest = session.data_dir / rel
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(artifact.tmp_path, 0o600)
                os.replace(artifact.tmp_path, dest)
                placed.append((artifact_key(parser, result.blob_sha256, artifact.table), artifact, rel))
            session.record_artifacts(run_id, LAYER, spec.emil_id, result.blob_sha256, parser, placed)
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
    session.finish_run(run_id, failed=sum(1 for r in results if r["status"] == "failed"))
    return results
