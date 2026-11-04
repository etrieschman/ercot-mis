"""The catalog: what ERCOT listed, what is archived, how it arrived, and every file a build wrote.

One DuckDB file in the data folder. Rows are inserted or refreshed, never deleted, so the
catalog is also the record of what ERCOT offered and when. DuckDB lets one process hold the
file at a time, so readers open it read-only per call and writers hold it only for the write;
an open that meets a lock waits and retries (CLAUDE.md, "Provenance").
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import polars as pl

if TYPE_CHECKING:
    from .ews import RemoteDoc
    from .products import Product
    from .store import Member, StoredBlob

SCHEMA = (
    # Every document a source has listed, refreshed each time it is listed.
    """CREATE TABLE IF NOT EXISTS remote_doc (
        emil_id VARCHAR NOT NULL,
        doc_id VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        report_type_id INTEGER,
        file_name VARCHAR,
        report_group VARCHAR,
        operating_date VARCHAR,
        posted_at TIMESTAMPTZ,
        size_bytes BIGINT,
        format VARCHAR,
        url VARCHAR,
        first_listed_at TIMESTAMPTZ NOT NULL,
        last_listed_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (emil_id, doc_id)
    )""",
    # Distinct archived bytes.
    """CREATE TABLE IF NOT EXISTS archive_blob (
        sha256 VARCHAR PRIMARY KEY,
        emil_id VARCHAR NOT NULL,
        path VARCHAR NOT NULL,
        size_bytes BIGINT NOT NULL,
        classification VARCHAR NOT NULL,
        stored_at TIMESTAMPTZ NOT NULL,
        client_version VARCHAR NOT NULL
    )""",
    # Files inside each archived zip, hashed without extracting.
    """CREATE TABLE IF NOT EXISTS archive_member (
        blob_sha256 VARCHAR NOT NULL,
        member_path VARCHAR NOT NULL,
        member_sha256 VARCHAR NOT NULL,
        size_bytes BIGINT NOT NULL,
        PRIMARY KEY (blob_sha256, member_path)
    )""",
    # One row per arrival; the same bytes can arrive by ingest and later by fetch.
    """CREATE TABLE IF NOT EXISTS archive_source (
        sha256 VARCHAR NOT NULL,
        emil_id VARCHAR NOT NULL,
        doc_id VARCHAR,
        method VARCHAR NOT NULL,
        original_name VARCHAR,
        stored_at TIMESTAMPTZ NOT NULL
    )""",
    # -- provenance ------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS run (
        run_id VARCHAR PRIMARY KEY,
        command VARCHAR NOT NULL,
        client_version VARCHAR NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        failed INTEGER
    )""",
    # One row per written file; the key changes when the parser or the input changes.
    """CREATE TABLE IF NOT EXISTS artifact (
        artifact_key VARCHAR PRIMARY KEY,
        layer VARCHAR NOT NULL,
        table_name VARCHAR NOT NULL,
        emil_id VARCHAR NOT NULL,
        blob_sha256 VARCHAR NOT NULL,
        parser_id VARCHAR NOT NULL,
        path VARCHAR NOT NULL,
        rows BIGINT NOT NULL,
        size_bytes BIGINT NOT NULL,
        run_id VARCHAR NOT NULL,
        written_at TIMESTAMPTZ NOT NULL
    )""",
    # Which zip members fed each artifact.
    """CREATE TABLE IF NOT EXISTS lineage (
        artifact_key VARCHAR NOT NULL,
        member_sha256 VARCHAR NOT NULL,
        PRIMARY KEY (artifact_key, member_sha256)
    )""",
)


TABLES = {re.search(r"EXISTS (\w+)", statement).group(1) for statement in SCHEMA}


def posting_order(posted_at: datetime | None, tiebreak: str) -> tuple:
    """The sort key of a posting: ERCOT's posting time, oldest first, an undated one first of all (TOP-11)."""
    return (posted_at is not None, posted_at or datetime.min.replace(tzinfo=timezone.utc), len(tiebreak), tiebreak)


def _connect(path: Path, read_only: bool) -> duckdb.DuckDBPyConnection:
    """A connection to the file, after waiting out another process's lock for up to about a minute."""
    for wait in (*LOCK_WAITS, None):
        try:
            return duckdb.connect(str(path), read_only=read_only)
        except duckdb.IOException as error:
            if wait is None or "lock" not in str(error).lower():
                raise
            time.sleep(wait)
    raise AssertionError("unreachable")


def client_version() -> str:
    try:
        return version("ercot-mis")
    except PackageNotFoundError:
        return "unknown"


# Seconds between attempts to open a locked catalog, about a minute in total.
LOCK_WAITS = (1, 2, 5, 10, 20, 30)

CATALOG_FILE = "catalog.duckdb"

# Catalog files this process has checked for every table; a read-only open checks a file once.
_CHECKED: set[Path] = set()


class Catalog:
    def __init__(self, path: Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if read_only and self.path not in _CHECKED and (not self.path.is_file() or self._missing_tables()):
            Catalog(self.path).close()  # create the file, or the tables a newer version added
        self.con = _connect(self.path, read_only)
        _CHECKED.add(self.path)
        self.con.execute("SET TimeZone = 'UTC'")
        if not read_only:
            for statement in SCHEMA:
                self.con.execute(statement)
            os.chmod(self.path, 0o600)

    def _missing_tables(self) -> bool:
        con = _connect(self.path, read_only=True)
        try:
            present = {t for (t,) in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        finally:
            con.close()
        return not TABLES <= present

    def query(self, sql: str, params: list | None = None) -> list[tuple]:
        """The rows of an ad hoc SQL query."""
        return self.con.execute(sql, params or []).fetchall()

    def latest_doc(self, emil_id: str, doc_ids: Iterable[str]) -> str:
        """The one of ``doc_ids`` ERCOT posted last (TOP-11)."""
        doc_ids = list(doc_ids)
        posted = dict(self.documents(emil_id, doc_ids).select("doc_id", "posted_at").rows())
        return max(doc_ids, key=lambda d: posting_order(posted.get(d), d))

    def close(self) -> None:
        self.con.close()

    def record_listing(self, product: Product, docs: Iterable[RemoteDoc], listed_at: datetime) -> None:
        """Insert the documents listed for the first time and refresh the rest."""
        unique = {d.doc_id: d for d in docs if d.doc_id}
        if not unique:
            return
        self.con.executemany(
            """INSERT INTO remote_doc (emil_id, doc_id, source, report_type_id, file_name, report_group,
                   operating_date, posted_at, size_bytes, format, url, first_listed_at, last_listed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (emil_id, doc_id) DO UPDATE SET
                   file_name = excluded.file_name,
                   report_group = excluded.report_group,
                   operating_date = excluded.operating_date,
                   posted_at = excluded.posted_at,
                   size_bytes = excluded.size_bytes,
                   format = excluded.format,
                   url = excluded.url,
                   last_listed_at = excluded.last_listed_at""",
            [
                (product.emil_id, d.doc_id, product.source, d.report_type_id, d.file_name, d.report_group,
                 d.operating_date or None, d.posted_at, d.size_bytes, d.format, d.url, listed_at, listed_at)
                for d in unique.values()
            ],
        )

    def documents(self, emil_id: str, doc_ids: Iterable[str] | None = None) -> pl.DataFrame:
        """A product's listed documents, each with ``is_archived`` and the ``sha256`` of its bytes."""
        filters, params = ["d.emil_id = $emil_id"], {"emil_id": emil_id}
        if doc_ids is not None:
            ids = list(doc_ids)
            if ids:
                filters.append("list_contains($doc_ids, d.doc_id)")
                params["doc_ids"] = ids
            else:
                filters.append("false")
        return self.con.execute(
            f"""SELECT d.emil_id, d.doc_id, d.report_type_id, d.file_name, d.report_group,
                       d.operating_date, d.posted_at, d.size_bytes, d.format, d.url,
                       d.first_listed_at, d.last_listed_at,
                       s.sha256, s.sha256 IS NOT NULL AS is_archived
                FROM remote_doc AS d
                LEFT JOIN (
                    SELECT emil_id, doc_id, min(sha256) AS sha256
                    FROM archive_source
                    WHERE doc_id IS NOT NULL
                    GROUP BY emil_id, doc_id
                ) AS s ON s.emil_id = d.emil_id AND s.doc_id = d.doc_id
                WHERE {" AND ".join(filters)}
                ORDER BY d.posted_at, d.doc_id""",
            params,
        ).pl()

    def has_blob(self, sha256: str) -> bool:
        return self.con.execute("SELECT 1 FROM archive_blob WHERE sha256 = ?", [sha256]).fetchone() is not None

    def add_blob(self, blob: StoredBlob, product: Product, members: list[Member]) -> None:
        """Record a blob and its zip members in one transaction."""
        self.con.begin()
        try:
            self.con.execute(
                "INSERT INTO archive_blob VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                [blob.sha256, product.emil_id, blob.path.as_posix(), blob.size_bytes,
                 product.classification, datetime.now(timezone.utc), client_version()],
            )
            if members:
                self.con.executemany(
                    "INSERT INTO archive_member VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    [(blob.sha256, m.member_path, m.member_sha256, m.size_bytes) for m in members],
                )
            self.con.commit()
        except BaseException:
            self.con.rollback()
            raise

    def add_source(
        self,
        sha256: str,
        emil_id: str,
        doc_id: str | None,
        method: str,
        original_name: str | None,
    ) -> None:
        """Record how a blob arrived (fetch or ingest, and from which listing); an identical arrival is recorded once."""
        self.con.execute(
            """INSERT INTO archive_source
               SELECT $sha256, $emil_id, $doc_id, $method, $original_name, $stored_at
               WHERE NOT EXISTS (
                   SELECT 1 FROM archive_source
                   WHERE sha256 = $sha256 AND emil_id = $emil_id AND method = $method
                     AND doc_id IS NOT DISTINCT FROM $doc_id
                     AND original_name IS NOT DISTINCT FROM $original_name
               )""",
            {"sha256": sha256, "emil_id": emil_id, "doc_id": doc_id, "method": method,
             "original_name": original_name, "stored_at": datetime.now(timezone.utc)},
        )

    def doc_id_for_name(self, emil_id: str, name: str) -> str | None:
        """The one listed document a local file name belongs to, or None; the name may lack ``.zip`` or have its punctuation replaced."""
        stem = name[:-4] if name.lower().endswith(".zip") else name
        rows = self.con.execute(
            """SELECT DISTINCT doc_id FROM remote_doc
               WHERE emil_id = $emil_id
                 AND (file_name IN ($name, $stem)
                      OR regexp_replace(file_name, '[^A-Za-z0-9._-]', '_', 'g') IN ($name, $stem))""",
            {"emil_id": emil_id, "name": name, "stem": stem},
        ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    # ------------------------------------------------------------- provenance

    def packages(self, emil_id: str) -> list[dict]:
        """A product's archived packages, newest posting first, each with its document id, posting time, operating date and member hashes."""
        rows = self.con.execute(
            """SELECT b.sha256, b.path, any_value(s.doc_id) AS doc_id, max(d.posted_at) AS posted_at,
                      try_cast(any_value(d.operating_date) AS DATE) AS operating_date,
                      list(m.member_path ORDER BY m.member_path) AS member_paths,
                      list(m.member_sha256 ORDER BY m.member_path) AS member_hashes
               FROM archive_blob b
               JOIN archive_source s USING (sha256)
               LEFT JOIN remote_doc d ON d.emil_id = s.emil_id AND d.doc_id = s.doc_id
               LEFT JOIN archive_member m ON m.blob_sha256 = b.sha256
               WHERE b.emil_id = ?
               GROUP BY b.sha256, b.path
               ORDER BY posted_at DESC NULLS LAST, b.sha256""",
            [emil_id],
        ).fetchall()
        return [{"sha256": sha, "path": path, "doc_id": doc_id, "posted_at": posted, "operating_date": operating_date,
                 "members": {p: h for p, h in zip(paths or [], hashes or []) if p is not None}}
                for sha, path, doc_id, posted, operating_date, paths, hashes in rows]

    def artifact_keys(self, layer: str) -> set[str]:
        return {k for (k,) in self.con.execute("SELECT artifact_key FROM artifact WHERE layer = ?", [layer]).fetchall()}

    def artifacts(self, layer: str) -> pl.DataFrame:
        """Every file a build wrote in a layer, oldest first."""
        return self.con.execute("SELECT * FROM artifact WHERE layer = $layer ORDER BY written_at", {"layer": layer}).pl()

    def start_run(self, command: str) -> str:
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.urandom(3).hex()}"
        self.con.execute("INSERT INTO run VALUES (?, ?, ?, ?, NULL, NULL)",
                         [run_id, command, client_version(), datetime.now(timezone.utc)])
        return run_id

    def finish_run(self, run_id: str, failed: int) -> None:
        self.con.execute("UPDATE run SET finished_at = ?, failed = ? WHERE run_id = ?",
                         [datetime.now(timezone.utc), failed, run_id])

    def add_artifacts(self, run_id: str, layer: str, emil_id: str, blob_sha256: str, parser: str, placed: list) -> None:
        """Register a build's files and the zip members behind each; an earlier row for the same file is replaced."""
        self.con.begin()
        try:
            for key, artifact, rel in placed:
                self.con.execute("DELETE FROM lineage WHERE artifact_key = ?", [key])
                self.con.execute("DELETE FROM artifact WHERE artifact_key = ?", [key])
                # A parser change gives the same (blob, table) a new key; the row under the old key goes.
                self.con.execute("DELETE FROM artifact WHERE layer = ? AND blob_sha256 = ? AND table_name = ?",
                                 [layer, blob_sha256, artifact.table])
                self.con.execute("INSERT INTO artifact VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 [key, layer, artifact.table, emil_id, blob_sha256, parser, rel.as_posix(),
                                  artifact.rows, artifact.size_bytes, run_id, datetime.now(timezone.utc)])
                if artifact.member_sha256s:
                    self.con.executemany("INSERT INTO lineage VALUES (?, ?) ON CONFLICT DO NOTHING",
                                         [(key, m) for m in artifact.member_sha256s if m])
            self.con.commit()
        except BaseException:
            self.con.rollback()
            raise


@contextmanager
def writing(data_dir: Path) -> Iterator[Catalog]:
    """The writable catalog, open only inside the block."""
    writer = Catalog(Path(data_dir) / CATALOG_FILE)
    try:
        yield writer
    finally:
        writer.close()


class ReadOnlyCatalog:
    """Every ``Catalog`` method, each call on its own read-only connection that closes before returning.

    A notebook that kept a connection open would block the daily pull's writes.
    """

    read_only = True

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / CATALOG_FILE

    def __getattr__(self, name: str):
        if name.startswith("_") or not callable(getattr(Catalog, name, None)):
            raise AttributeError(name)

        def call(*args, **kwargs):
            catalog = Catalog(self.path, read_only=True)
            try:
                return getattr(catalog, name)(*args, **kwargs)
            finally:
                catalog.close()

        return call
