"""The session: one open data folder, the clients that fill it, and the layers built from it.

``em.open()`` returns a ``Session``. Its methods follow the data flow: ``list`` and
``fetch``/``ingest`` fill the archive, ``build_raw`` and ``build_core`` write the
layers, ``raw(table)`` and ``core(table)`` read them.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Protocol

import polars as pl
import requests

from .config import Identity, load_identity
from .products import Product, get_product
from .sources.retry import retrying
from .sources.ews import ERCOT_TZ, EwsClient, EwsError, RemoteDoc
from .archive import store
from .archive.catalog import Catalog

# Columns that embed the participant DUNS (ERCOT's file names) or point at the
# participant's download servlet. They stay in the catalog and never leave it.
PRIVATE_COLUMNS = ("file_name", "url")

DOCUMENT_SCHEMA = {
    "report_type_id": pl.Int64,
    "doc_id": pl.String,
    "report_group": pl.String,
    "operating_date": pl.String,
    "posted_at": pl.Datetime("us", "UTC"),
    "size_bytes": pl.Int64,
    "format": pl.String,
}

FETCH_SCHEMA = {
    "emil_id": pl.String,
    "doc_id": pl.String,
    "report_group": pl.String,
    "operating_date": pl.String,
    "posted_at": pl.Datetime("us", "UTC"),
    "size_bytes": pl.Int64,
    "sha256": pl.String,
    "status": pl.String,
    "error": pl.String,
}
BUILD_SCHEMA = {
    "emil_id": pl.String,
    "doc_id": pl.String,
    "blob_sha256": pl.String,
    "status": pl.String,
    "tables": pl.Int64,
    "rows": pl.Int64,
    "seconds": pl.Float64,
    "error": pl.String,
}
INGEST_SCHEMA = {
    "emil_id": pl.String,
    "doc_id": pl.String,
    "sha256": pl.String,
    "size_bytes": pl.Int64,
    "is_new_bytes": pl.Boolean,
}

# ERCOT names its downloads "man.<8-digit report type>.<participant>.<timestamp>.<name>".
_ERCOT_NAME = re.compile(r"^man\.(\d{8})\.")

# Errors a second attempt can fix: a stalled connection, a short read, a 5xx reply.
_TRANSIENT = (requests.RequestException, EwsError, ConnectionError, TimeoutError)


class Source(Protocol):
    def list_documents(self, product: Product, start: datetime | None, end: datetime | None) -> list[RemoteDoc]: ...
    def download(self, url: str) -> Iterator[bytes]: ...


class BudgetError(RuntimeError):
    """A fetch would download more than ``max_gb`` allows; nothing was downloaded."""


class DownloadError(RuntimeError):
    """A download arrived incomplete or different from its listing; nothing was kept."""


@dataclass(frozen=True)
class Probe:
    """What EWS holds for one product.

    Both parts are safe to print: ``summary`` holds counts, dates, bytes and report
    groups; ``documents`` is the listing without ERCOT's file names and URLs.
    """

    summary: dict
    documents: pl.DataFrame


class Session:
    """A local ercot-mis data folder plus the clients that fill it.

    The catalog is a DuckDB file that allows one writer at a time. ``Mis`` reads it
    through a read-only connection and takes a writable one only for the moments it
    records a listing or an archived document, never across a download, so a daily
    pull and a notebook rarely collide. Use as a context manager, or call ``close()``,
    to release the read-only connection.
    """

    def __init__(self, data_dir: Path, identity: Identity | None = None):
        self.data_dir = Path(data_dir)
        self._identity = identity
        self._ews: EwsClient | None = None
        self._catalog: Catalog | None = None

    def __repr__(self) -> str:
        return f"Session({str(self.data_dir)!r})"

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None

    @property
    def catalog(self) -> Catalog:
        """A read-only view of the catalog. Close the session to let another process write."""
        if self._catalog is None:
            self._catalog = Catalog(self._catalog_path, read_only=True)
        return self._catalog

    @property
    def _catalog_path(self) -> Path:
        return self.data_dir / "catalog.duckdb"

    @contextmanager
    def _writer(self) -> Iterator[Catalog]:
        """The writable catalog, held only for the duration of the block."""
        self.close()
        writer = Catalog(self._catalog_path)
        try:
            yield writer
        finally:
            writer.close()

    @property
    def ews(self) -> EwsClient:
        """The EWS client, created on first use so reading local data never needs credentials."""
        if self._ews is None:
            self._ews = EwsClient(self._identity or load_identity())
        return self._ews

    def _source(self, spec: Product) -> Source:
        if spec.source == "ews":
            return self.ews
        raise NotImplementedError(f"{spec.emil_id} comes from the Public API, whose client is not built yet.")

    # ------------------------------------------------------------------ listing

    def list(self, product: str | int, since: date | datetime | str | None = None,
             until: date | datetime | str | None = None) -> pl.DataFrame:
        """List what ERCOT currently offers for a product, and record it in the catalog.

        ``since`` and ``until`` bound the posting time; a date means midnight ERCOT time.
        Returns the listed documents with ``is_archived`` and ``sha256``, without ERCOT's
        file names (they embed the participant DUNS). Works for tracked products too:
        listing is how they are tracked.
        """
        return self._list(get_product(product), since, until).drop(PRIVATE_COLUMNS)

    def _list(self, spec: Product, since, until) -> pl.DataFrame:
        docs = self._source(spec).list_documents(spec, _bound(since), _bound(until))
        with self._writer() as writer:
            writer.record_listing(spec, docs, listed_at=datetime.now(timezone.utc))
            return writer.documents(spec.emil_id, [d.doc_id for d in docs if d.doc_id])

    def probe(self, product: str | int, *, archive_years: float = 7, now: datetime | None = None) -> Probe:
        """List everything EWS offers for a product, and whether it reaches past the display window.

        Makes two listing calls and downloads nothing. The first omits the time window,
        which EWS documents as "everything available". The second asks explicitly for
        documents posted before the display window, since an unbounded listing may be
        capped at the window. ``summary["n_docs_before_display_window"] > 0`` is the
        answer to "does the archive go deeper than MIS shows?".
        """
        spec = get_product(product)
        if spec.source != "ews" or spec.report_type_id is None:
            raise NotImplementedError(f"{spec.emil_id} comes from the Public API, whose client is not built yet.")
        now = datetime.now(timezone.utc) if now is None else now
        cutoff = now - timedelta(days=spec.display_days or 0)

        unbounded = self.ews.get_reports(spec.report_type_id)
        older_error = None
        try:
            older = self.ews.get_reports(
                spec.report_type_id,
                start=now - timedelta(days=round(365.25 * archive_years)),
                end=cutoff,
            )
        except EwsError as error:
            older, older_error = [], str(error)

        docs = _dedupe(unbounded + older)
        return Probe(summary=_summarize(spec, docs, len(unbounded), cutoff, now, older_error), documents=_frame(docs))

    # --------------------------------------------------------------- archiving

    def fetch(
        self,
        product: str | int,
        since: date | datetime | str | None = None,
        until: date | datetime | str | None = None,
        *,
        operating_dates: Iterable[date | str] | None = None,
        max_gb: float | None = None,
    ) -> pl.DataFrame:
        """Download every listed document that is not archived yet.

        Each document is committed on its own, so running again resumes an interrupted
        fetch. A transient failure (stalled connection, short read, 5xx) is retried
        within the run; a document that still fails is reported (``status == "failed"``)
        without stopping the others, and is retried on the next run. ``operating_dates``
        restricts the fetch to those days; ``max_gb`` refuses, before downloading
        anything, a fetch larger than the budget.
        """
        spec = get_product(product)
        if spec.take != "pull":
            raise ValueError(
                f"{spec.emil_id} is tracked, not pulled: list() records its documents. "
                "Set take='pull' in products.py to download it."
            )
        wanted = self._list(spec, since, until).filter(~pl.col("is_archived"))
        if operating_dates is not None:
            days = sorted({_as_date(d).isoformat() for d in operating_dates})
            wanted = wanted.filter(pl.col("operating_date").is_in(days))
        total = int(wanted["size_bytes"].sum())
        if max_gb is not None and total > max_gb * 1e9:
            raise BudgetError(
                f"{spec.emil_id}: {wanted.height} documents, {total / 1e9:.2f} GB exceeds max_gb={max_gb}. "
                "Narrow the window or raise the budget."
            )

        source = self._source(spec)
        results = []
        for row in wanted.sort("posted_at").iter_rows(named=True):
            outcome = {k: row[k] for k in ("emil_id", "doc_id", "report_group", "operating_date", "posted_at", "size_bytes")}
            suffix = _suffix(row["file_name"], row["format"])
            try:
                blob = retrying(
                    lambda: self._archive(spec, source.download(row["url"]), suffix, expected_size=row["size_bytes"]),
                    _TRANSIENT + (DownloadError,),
                )
                with self._writer() as writer:
                    writer.add_source(blob.sha256, spec.emil_id, row["doc_id"], "fetch", row["file_name"])
                results.append({**outcome, "sha256": blob.sha256, "status": "fetched", "error": None})
            except Exception as error:  # reported, and retried on the next run
                results.append({**outcome, "sha256": None, "status": "failed", "error": f"{type(error).__name__}: {error}"})
        return pl.DataFrame(results, schema=FETCH_SCHEMA)

    def ingest(self, path: str | Path, product: str | int | None = None) -> pl.DataFrame:
        """Adopt ERCOT documents downloaded outside ercot-mis, such as by the old notebook script.

        ``path`` is a file or a folder. For a folder only its top-level zip files are taken,
        since extracted copies are not documents. The product comes from ``product`` or from
        ERCOT's file name (``man.<report type>.…``). Each file is linked to its listing by
        name when the catalog has one, so call ``list()`` first while ERCOT still offers it.
        """
        path = Path(path).expanduser()
        files = [path] if path.is_file() else sorted(
            p for p in path.iterdir() if p.is_file() and zipfile.is_zipfile(p)
        )
        rows = []
        for file in files:
            spec = get_product(product) if product is not None else _product_from_name(file.name)
            with file.open("rb") as handle:
                blob = self._archive(spec, iter(lambda: handle.read(store.CHUNK), b""), file.suffix)
            with self._writer() as writer:
                doc_id = writer.doc_id_for_name(spec.emil_id, file.name)
                writer.add_source(blob.sha256, spec.emil_id, doc_id, "ingest", file.name)
            rows.append({"emil_id": spec.emil_id, "doc_id": doc_id, "sha256": blob.sha256,
                         "size_bytes": blob.size_bytes, "is_new_bytes": blob.created})
        return pl.DataFrame(rows, schema=INGEST_SCHEMA)

    # ----------------------------------------------------------------- building

    def build_raw(self, product: str | int, *, workers: int | None = None, limit: int | None = None) -> pl.DataFrame:
        """Parse every archived package of a product into raw Parquet, skipping what is built.

        One file per package and table under ``raw/<table>/emil_id=<EMIL>/``, every row
        carrying its source identity. See ``ercot_mis.raw.build``. Returns one row per package.
        """
        from .raw.build import build

        return pl.DataFrame(build(self, product, workers=workers, limit=limit), schema=BUILD_SCHEMA)

    def build_core(self, *, limit: int | None = None) -> pl.DataFrame:
        """Write ``core.snapshot`` and ``core.node`` from the raw layer. See ``ercot_mis.core.build``."""
        from .core.build import build

        return pl.DataFrame(build(self, limit=limit), schema=BUILD_SCHEMA)

    def match_branches(self, crr_snapshot_id: str, dam_snapshot_id: str) -> pl.DataFrame:
        """``core.match_branch`` for one CRR model and one DAM hour, computed once and cached.

        See ``ercot_mis.core.match``. Rows: every CRR branch with its DAM branch and the
        ``match_method`` that found it, plus DAM branches nothing matched.
        """
        from .core.match import COLUMNS, VERSION, match_branches

        key = f"{crr_snapshot_id}__{dam_snapshot_id}".replace(":", "-")
        path = self.data_dir / "core" / "match_branch" / f"v{VERSION}" / f"{key}.parquet"
        if path.is_file():
            return pl.read_parquet(path)
        crr = self.core("branch").filter(pl.col("snapshot_id") == crr_snapshot_id).collect()
        dam = self.core("branch").filter(pl.col("snapshot_id") == dam_snapshot_id).collect()
        if crr.is_empty() or dam.is_empty():
            raise KeyError(f"no core.branch rows for {crr_snapshot_id!r} and {dam_snapshot_id!r}; run build_core()")
        blob = self.core("snapshot").filter(pl.col("snapshot_id") == crr_snapshot_id).select("blob_sha256", "month").collect().row(0)
        lines = self.raw("crr_mapping_lines").filter((pl.col("blob_sha256") == blob[0]) & (pl.col("month") == blob[1])).collect()
        autos = self.raw("crr_mapping_autos").filter((pl.col("blob_sha256") == blob[0]) & (pl.col("month") == blob[1])).collect()
        result = match_branches(crr, lines, autos, dam).with_columns(
            pl.lit(crr_snapshot_id).alias("crr_snapshot_id"), pl.lit(dam_snapshot_id).alias("dam_snapshot_id")
        ).select("crr_snapshot_id", "dam_snapshot_id", *COLUMNS)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        result.write_parquet(path, compression="zstd")
        path.chmod(0o600)
        return result

    # ----------------------------------------------------------------- reading

    def raw(self, table: str) -> pl.LazyFrame:
        """A lazy scan over every raw artifact of a table (``emil_id`` comes from the path)."""
        return self._scan("raw", table)

    def core(self, table: str) -> pl.LazyFrame:
        """A lazy scan over a core table: ``snapshot`` (one file) or ``node`` (one file per package)."""
        return self._scan("core", table)

    def _scan(self, layer: str, table: str) -> pl.LazyFrame:
        folder = self.data_dir / layer / table
        single = folder.with_suffix(".parquet")
        if single.is_file():
            return pl.scan_parquet(str(single))
        return pl.scan_parquet(str(folder / "**" / "*.parquet"), hive_partitioning=True)

    def _start_run(self, command: str) -> str:
        with self._writer() as writer:
            return writer.start_run(command)

    def _finish_run(self, run_id: str, failed: int) -> None:
        with self._writer() as writer:
            writer.finish_run(run_id, failed)

    def _add_artifacts(self, *args) -> None:
        with self._writer() as writer:
            writer.add_artifacts(*args)

    def _archive(self, spec: Product, chunks: Iterable[bytes], suffix: str,
                 expected_size: int | None = None) -> store.StoredBlob:
        """Stream bytes into the archive and index them; the catalog is locked only for the index."""
        blob = store.put(self.data_dir, spec.emil_id, chunks, suffix)
        if expected_size and blob.size_bytes != expected_size:
            if blob.created:
                store.discard(self.data_dir, blob)
            raise DownloadError(f"received {blob.size_bytes:,} bytes but the listing says {expected_size:,}")
        with self._writer() as writer:
            if not writer.has_blob(blob.sha256):
                writer.add_blob(blob, spec, store.index_members(self.data_dir / blob.path))
        return blob


# ---------------------------------------------------------------------- helpers


def _bound(value: date | datetime | str | None) -> datetime | None:
    """A posting-time bound: dates are midnight ERCOT time, naive datetimes are ERCOT time."""
    if value is None:
        return None
    if isinstance(value, str):
        value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ERCOT_TZ)
    return datetime.combine(value, time(), ERCOT_TZ)


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def _suffix(file_name: str | None, format: str | None) -> str:
    suffix = Path(file_name or "").suffix
    return suffix if suffix else (f".{format}" if format else "")


def _product_from_name(name: str) -> Product:
    match = _ERCOT_NAME.match(name)
    if match is None:
        raise ValueError(f"Can't tell the product from a file name like this; pass product= (e.g. 'NP7-801-M').")
    return get_product(int(match.group(1)))


def _dedupe(docs: list[RemoteDoc]) -> list[RemoteDoc]:
    seen: dict[str, RemoteDoc] = {}
    for doc in docs:
        seen.setdefault(doc.doc_id or doc.url or f"{doc.file_name}|{doc.posted_at}", doc)
    earliest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(seen.values(), key=lambda d: (d.posted_at is None, d.posted_at or earliest))


def _summarize(
    spec: Product,
    docs: list[RemoteDoc],
    n_unbounded: int,
    cutoff: datetime,
    now: datetime,
    older_error: str | None,
) -> dict:
    posted = [d.posted_at for d in docs if d.posted_at is not None]
    operating = sorted(d.operating_date for d in docs if d.operating_date)
    groups: dict[str, int] = {}
    for doc in docs:
        groups[doc.report_group] = groups.get(doc.report_group, 0) + 1
    return {
        "emil_id": spec.emil_id,
        "name": spec.name,
        "report_type_id": spec.report_type_id,
        "display_days": spec.display_days,
        "n_docs": len(docs),
        "n_docs_unbounded_listing": n_unbounded,
        "n_docs_before_display_window": sum(1 for t in posted if t < cutoff),
        "earliest_posted": min(posted, default=None),
        "latest_posted": max(posted, default=None),
        "earliest_operating_date": operating[0] if operating else None,
        "latest_operating_date": operating[-1] if operating else None,
        "total_bytes": sum(d.size_bytes for d in docs),
        "report_groups": groups,
        "older_window_error": older_error,
        "probed_at": now,
    }


def _frame(docs: list[RemoteDoc]) -> pl.DataFrame:
    rows = [
        {**{k: v for k, v in asdict(d).items() if k not in PRIVATE_COLUMNS},
         "posted_at": d.posted_at.astimezone(timezone.utc) if d.posted_at else None}
        for d in docs
    ]
    return pl.DataFrame(rows, schema=DOCUMENT_SCHEMA)
