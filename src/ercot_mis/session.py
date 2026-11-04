"""The session: one data folder, the clients that fill it, and every layer read from it.

``em.open()`` returns a ``Session``. ``list``, ``fetch`` and ``ingest`` fill the archive;
``build_raw`` and ``build_core`` write the layers; ``raw`` and ``core`` read them;
``network``, ``injections``, ``match_*`` and ``diff_*`` compute from them. Each method
names the module that does the work.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import polars as pl

from . import viewer
from .archive import ews, fetch
from .core import diff, match
from .core.build import build as build_core_tables
from .out import injection, network as out
from .raw import build as raw_build
from .archive.catalog import Catalog, ReadOnlyCatalog, writing
from .config import Identity, load_identity
from .archive.ews import PRIVATE_COLUMNS, EwsClient, Probe
from .archive.fetch import as_date
from .archive.products import Product, get_product
from .archive.public_api import PublicApiClient

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

class Session:
    """A data folder and the clients that fill it.

    The session holds no catalog connection: a read opens one for the call, and a write holds one only
    while it records a listing, a blob or an artifact. A notebook left open never blocks the daily pull.
    """

    def __init__(self, data_dir: Path, identity: Identity | None = None):
        self.data_dir = Path(data_dir)
        self._identity = identity
        self._ews: EwsClient | None = None
        self._public_api: PublicApiClient | None = None
        self._pairs: dict[tuple, tuple[tuple, pl.DataFrame]] = {}  # match_* and diff_* tables computed in this session

    def __repr__(self) -> str:
        return f"Session({str(self.data_dir)!r})"

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """Nothing to close; ``with em.open() as s:`` is allowed."""

    @property
    def catalog(self) -> ReadOnlyCatalog:
        """The catalog, read-only, one connection per call."""
        return ReadOnlyCatalog(self.data_dir)

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        """The rows of an ad hoc read-only SQL query on the catalog."""
        return self.catalog.query(sql, params)

    def latest_doc(self, emil_id: str, doc_ids: Iterable[str]) -> str:
        """The one of ``doc_ids`` ERCOT posted last (TOP-11)."""
        return self.catalog.latest_doc(emil_id, doc_ids)

    @contextmanager
    def _writer(self) -> Iterator[Catalog]:
        with writing(self.data_dir) as writer:
            yield writer

    @property
    def ews(self) -> EwsClient:
        """The EWS client, created on first use; reading local data never needs credentials."""
        if self._ews is None:
            self._ews = EwsClient(self._identity or load_identity())
        return self._ews

    @property
    def public_api(self) -> PublicApiClient:
        """The Public API client, created on first use from the secrets."""
        if self._public_api is None:
            self._public_api = PublicApiClient.from_secrets()
        return self._public_api

    # ------------------------------------------------------------------ the archive: ercot_mis.archive.fetch

    def _client(self, spec: Product):
        return self.ews if spec.source == "ews" else self.public_api

    def list(self, product: str | int, since: date | datetime | str | None = None, until: date | datetime | str | None = None) -> pl.DataFrame:
        """The documents ERCOT lists for a product, recorded in the catalog, each with ``is_archived`` and ``sha256``.

        ``since`` and ``until`` bound the posting time (a date is midnight ERCOT time). File names and URLs stay in the catalog.
        """
        spec = get_product(product)
        return fetch.list_documents(self._client(spec), spec, self.data_dir, since, until).drop(PRIVATE_COLUMNS)

    def probe(self, product: str | int, *, archive_years: float = 7, now: datetime | None = None) -> Probe:
        """Two EWS listings of a product and whether any document predates the display window (``ews.probe``)."""
        spec = get_product(product)
        return ews.probe(self.ews if spec.source == "ews" else None, spec, archive_years=archive_years, now=now)

    def fetch(self, product: str | int, since: date | datetime | str | None = None, until: date | datetime | str | None = None, *,
              operating_dates: Iterable[date | str] | None = None, max_gb: float | None = None) -> pl.DataFrame:
        """One row per listed document not yet archived: fetched, or failed with the error (``fetch.fetch``).

        ``operating_dates`` keeps only those listing days; ``max_gb`` refuses a larger fetch before downloading.
        """
        spec = get_product(product)
        return fetch.fetch(self._client(spec), spec, self.data_dir, since, until, operating_dates=operating_dates, max_gb=max_gb)

    def ingest(self, path: str | Path, product: str | int | None = None) -> pl.DataFrame:
        """One row per ERCOT document adopted from a zip or a folder's top-level zips (``fetch.ingest``)."""
        return fetch.ingest(self.data_dir, path, product)

    # ----------------------------------------------------------------- building

    def build_raw(self, product: str | int, *, workers: int | None = None, limit: int | None = None) -> pl.DataFrame:
        """One row per archived package of a product: built into raw Parquet, skipped as already built, or failed (``raw.build``)."""
        return pl.DataFrame(raw_build.build(self, product, workers=workers, limit=limit), schema=BUILD_SCHEMA)

    def build_core(self, *, limit: int | None = None) -> pl.DataFrame:
        """One row per package: its core tables built, skipped as already built, or failed (``core.build``); ``core/snapshot.parquet`` is rewritten each time."""
        return pl.DataFrame(build_core_tables(self, limit=limit), schema=BUILD_SCHEMA)

    # ----------------------------------------------------------------- one model

    def snapshot(self, snapshot_id: str) -> dict:
        """The ``core.snapshot`` row of one model, as a dict."""
        rows = pl.read_parquet(self.data_dir / "core" / "snapshot.parquet").filter(pl.col("snapshot_id") == snapshot_id)
        if rows.is_empty():
            raise KeyError(f"{snapshot_id!r} is not in core.snapshot; run build_core()")
        return rows.row(0, named=True)

    def newest(self) -> tuple[str, str]:
        """The newest monthly CRR model and hour 12 of the newest DAM day, latest revisions; the pair the checks and the viewer default to."""
        snaps = self.core("snapshot").collect()
        crr = snaps.filter(pl.col("model_kind") == "crr_monthly").sort("month", "revision")
        dam = snaps.filter((pl.col("model_kind") == "dam") & (pl.col("hour") == 12)).sort("operating_date", "revision")
        if crr.is_empty() or dam.is_empty():
            raise ValueError("no monthly CRR model or no DAM day in core.snapshot; run build_core() first or pass snapshot ids")
        return crr["snapshot_id"][-1], dam["snapshot_id"][-1]

    def hour_ids(self, day: date) -> list[str]:
        """The latest revision of every DAM hour of one operating day."""
        snaps = self.core("snapshot").filter((pl.col("model_kind") == "dam") & (pl.col("operating_date") == day)).collect()
        return snaps.sort("hour", "revision").unique(subset=["hour"], keep="last").sort("hour")["snapshot_id"].to_list()

    def core_rows(self, table: str, snapshot_id: str) -> pl.DataFrame:
        """One model's rows of a core table, read from its package's file."""
        snap = self.snapshot(snapshot_id)
        path = self.data_dir / raw_build.artifact_path("core", table, snap["emil_id"], snap["blob_sha256"])
        if not path.is_file():
            return self.core(table).filter(pl.col("snapshot_id") == snapshot_id).collect()
        return pl.scan_parquet(path).filter(pl.col("snapshot_id") == snapshot_id).collect().with_columns(pl.lit(snap["emil_id"]).alias("emil_id"))

    def raw_rows(self, table: str, snapshot_id: str) -> pl.DataFrame:
        """One model's rows of a raw table: its package's file, narrowed to the model's DAM hour or CRR month."""
        snap = self.snapshot(snapshot_id)
        path = self.data_dir / raw_build.artifact_path("raw", table, snap["emil_id"], snap["blob_sha256"])
        rows = pl.scan_parquet(path) if path.is_file() else self.raw(table).filter(pl.col("blob_sha256") == snap["blob_sha256"]).drop("emil_id")
        columns = rows.collect_schema().names()
        if snap["hour"] is not None and "hour" in columns:
            rows = rows.filter(pl.col("hour") == snap["hour"])
        if snap["month"] is not None and "month" in columns:
            rows = rows.filter(pl.col("month") == snap["month"])
        return rows.collect().with_columns(pl.lit(snap["emil_id"]).alias("emil_id"))

    # ----------------------------------------------------------------- one CRR model against one DAM hour

    def match_branches(self, crr_snapshot_id: str, dam_snapshot_id: str) -> pl.DataFrame:
        """``core.match_branch``: every CRR branch with its DAM branch and ``match_method``, unmatched rows on both sides included."""
        def compute() -> pl.DataFrame:
            return match.match_branches(self.core_rows("branch", crr_snapshot_id), self.raw_rows("crr_mapping_lines", crr_snapshot_id),
                                        self.raw_rows("crr_mapping_autos", crr_snapshot_id), self.core_rows("branch", dam_snapshot_id))

        return self._pair("match_branch", match.VERSION, crr_snapshot_id, dam_snapshot_id, compute)

    def match_buses(self, crr_snapshot_id: str, dam_snapshot_id: str) -> pl.DataFrame:
        """``core.match_bus``: every CRR bus with its DAM bus, matched through settlement points first, then the ends of matched branches."""
        def compute() -> pl.DataFrame:
            return match.match_buses(self.core_rows("bus", crr_snapshot_id), self.core_rows("bus", dam_snapshot_id),
                                     self.core_rows("branch", crr_snapshot_id), self.core_rows("branch", dam_snapshot_id),
                                     self.match_branches(crr_snapshot_id, dam_snapshot_id))

        return self._pair("match_bus", match.VERSION, crr_snapshot_id, dam_snapshot_id, compute)

    def match_contingencies(self, crr_snapshot_id: str, dam_snapshot_id: str) -> pl.DataFrame:
        """``core.match_contingency``: every CRR contingency with its DAM contingency, matched by name, then by the translated branch set."""
        def compute() -> pl.DataFrame:
            return match.match_contingencies(self.core_rows("contingency_outage", crr_snapshot_id), self.core_rows("contingency_outage", dam_snapshot_id),
                                             self.match_branches(crr_snapshot_id, dam_snapshot_id))

        return self._pair("match_contingency", match.VERSION, crr_snapshot_id, dam_snapshot_id, compute)

    def diff_branches(self, crr_snapshot_id: str, dam_snapshot_id: str, time_of_use: str = "PeakWD") -> pl.DataFrame:
        """``core.diff_branch``: each matched pair's kind, reactance, voltages, service and ratings side by side, with ``same_*`` verdicts."""
        def compute() -> pl.DataFrame:
            crr, dam = crr_snapshot_id, dam_snapshot_id
            return diff.diff_branches(self.match_branches(crr, dam), self.core_rows("branch", crr), self.core_rows("bus", crr), self.core_rows("branch_rating", crr),
                                      self.core_rows("branch", dam), self.core_rows("bus", dam), self.core_rows("branch_rating", dam), time_of_use)

        return self._pair("diff_branch", diff.VERSION, crr_snapshot_id, dam_snapshot_id, compute, suffix=f"__{time_of_use}")

    def diff_settlement_points(self, crr_snapshot_id: str, dam_snapshot_id: str) -> pl.DataFrame:
        """``core.diff_settlement_point``: per settlement point, the buses each model puts it on, compared through ``core.match_bus``."""
        def compute() -> pl.DataFrame:
            crr, dam = crr_snapshot_id, dam_snapshot_id
            return diff.diff_settlement_points(self.core_rows("settlement_point", crr), self.core_rows("settlement_point_bus", crr),
                                               self.core_rows("settlement_point", dam), self.core_rows("settlement_point_bus", dam), self.match_buses(crr, dam))

        return self._pair("diff_settlement_point", diff.VERSION, crr_snapshot_id, dam_snapshot_id, compute)

    def diff_loads(self, crr_snapshot_id: str, dam_snapshot_id: str) -> pl.DataFrame:
        """``core.diff_load``: per bus carrying a load in either model, both models' loads there, compared through ``core.match_bus``."""
        def compute() -> pl.DataFrame:
            return diff.diff_loads(self.core_rows("load", crr_snapshot_id), self.core_rows("load", dam_snapshot_id),
                                   self.match_buses(crr_snapshot_id, dam_snapshot_id))

        return self._pair("diff_load", diff.VERSION, crr_snapshot_id, dam_snapshot_id, compute)

    def _pair(self, table: str, version: int, crr_snapshot_id: str, dam_snapshot_id: str, compute, suffix: str = "") -> pl.DataFrame:
        """A table about one CRR model and one DAM hour, computed once per session and again when either model's core files or ``version`` change; nothing is written to disk."""
        packages = [self.snapshot(sid) for sid in (crr_snapshot_id, dam_snapshot_id)]
        files = sorted(f for snap in packages for f in (self.data_dir / "core").glob(f"*/emil_id={snap['emil_id']}/{snap['blob_sha256'][:16]}.parquet"))
        stamp = (version, tuple((str(f.relative_to(self.data_dir)), f.stat().st_mtime_ns, f.stat().st_size) for f in files))
        key = (table, crr_snapshot_id, dam_snapshot_id, suffix)
        cached = self._pairs.get(key)
        if cached is None or cached[0] != stamp:
            ids = ("crr_snapshot_id", "dam_snapshot_id")
            result = (compute().with_columns(pl.lit(crr_snapshot_id).alias(ids[0]), pl.lit(dam_snapshot_id).alias(ids[1]))
                      .select(*ids, pl.exclude(*ids)))
            cached = self._pairs[key] = (stamp, result)
        return cached[1]

    def viewer(self, left: str | None = None, right: str | None = None, *, reference_dam: str | None = None) -> Path:
        """The path of the single-file network viewer for one snapshot or a pair (default: the newest pair), under ``reports/viewer/``.

        The page holds CEII; open it with ``?nohash`` when its address could leave the machine.
        """
        if left is None:
            left, right = self.newest()
        return viewer.build(self, left, right, reference_dam=reference_dam)

    def network(self, snapshot_id: str, options=None):
        """One snapshot's ``Network`` (``out.network``), assembled from its core tables on every call.

        ``options`` is an ``out.network.Options``: tie contraction and the CRR time-of-use block; every other rule is ERCOT's practice.
        """
        return out.build_network(snapshot_id, out.core_tables(self, snapshot_id), options or out.Options())

    # ----------------------------------------------------------------- reading

    def injections(self, day: date) -> pl.DataFrame:
        """The DAM's net injection per settlement point and hour for one operating day (``out.injection``), from the 60-day disclosure."""
        return injection.injections(self, as_date(day))

    def raw(self, table: str) -> pl.LazyFrame:
        """A lazy scan over every file of a raw table; ``emil_id`` comes from the path."""
        return self._scan("raw", table)

    def core(self, table: str) -> pl.LazyFrame:
        """A lazy scan over a core table: ``snapshot`` (one file) or a per-package table."""
        return self._scan("core", table)

    def _scan(self, layer: str, table: str) -> pl.LazyFrame:
        folder = self.data_dir / layer / table
        single = folder.with_suffix(".parquet")
        if single.is_file():
            return pl.scan_parquet(str(single))
        # CRR and DAM packages carry different identity columns; the union of one file's schema per product lets one scan span both.
        schema: dict = {}
        for product in sorted(folder.glob("emil_id=*")):
            for file in sorted(product.glob("*.parquet"))[:1]:
                for name, dtype in pl.read_parquet_schema(file).items():
                    schema.setdefault(name, dtype)
        if not schema:
            raise FileNotFoundError(f"no {layer}.{table} artifacts under {folder}; run the build first")
        return pl.scan_parquet(str(folder / "**" / "*.parquet"), hive_partitioning=True, schema=schema, missing_columns="insert")

    # ----------------------------------------------------------------- recording builds (used by raw.build and core.build)

    def start_run(self, command: str) -> str:
        """The id of a new ``run`` row in the catalog."""
        with self._writer() as writer:
            return writer.start_run(command)

    def record_artifacts(self, run_id: str, layer: str, emil_id: str, blob_sha256: str, identity: str, placed: list) -> None:
        """Register a build's files: ``placed`` is ``[(artifact key, Written, path relative to the data folder)]``."""
        with self._writer() as writer:
            writer.add_artifacts(run_id, layer, emil_id, blob_sha256, identity, placed)

    def finish_run(self, run_id: str, failed: int) -> None:
        with self._writer() as writer:
            writer.finish_run(run_id, failed)
