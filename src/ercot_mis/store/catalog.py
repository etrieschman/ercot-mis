"""The catalog: what ERCOT has listed, what is archived, and how it arrived.

One DuckDB file in the data folder. Rows are inserted or refreshed, never deleted,
so the catalog is also the history of what ERCOT offered and when. That matters
because EWS forgets everything older than a product's display window.

DuckDB lets one process write a file while nobody else has it open. Readers open
``read_only=True`` and writers keep their connection only as long as the write, so
a scheduled pull and a notebook seldom collide; when they do, opening waits and
retries for a short while before failing.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import polars as pl

if TYPE_CHECKING:
    from ..products import Product
    from ..sources.ews import RemoteDoc
    from .archive import Member, StoredBlob

SCHEMA = (
    # Every document a source has listed, refreshed each time it is listed again.
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
    # One row per arrival: the same bytes can be ingested once and fetched later.
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
    # One row per written file; the key changes whenever the parser or the input does.
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


def _connect(path: Path, read_only: bool) -> duckdb.DuckDBPyConnection:
    """Open the file, waiting out another process's lock for a while."""
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


class Catalog:
    def __init__(self, path: Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if read_only and (not self.path.is_file() or self._missing_tables()):
            Catalog(self.path).close()  # create the file, or tables a newer version added
        self.con = _connect(self.path, read_only)
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

    def close(self) -> None:
        self.con.close()

    def record_listing(self, product: Product, docs: Iterable[RemoteDoc], listed_at: datetime) -> None:
        """Insert newly listed documents and refresh the ones seen before."""
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
        """Catalogued documents for a product, with whether (and as which bytes) each is archived."""
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
        """Record archived bytes and their zip members in one transaction."""
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
        """Record how archived bytes arrived; an identical arrival is recorded once."""
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
        """The listed document a local file name belongs to, if exactly one matches.

        Matches ERCOT's name as listed, with or without ``.zip``, and the sanitized form
        the old notebook script saved files under.
        """
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
        """Archived packages of a product, newest posting first, each with its member hashes."""
        rows = self.con.execute(
            """SELECT b.sha256, b.path, any_value(s.doc_id) AS doc_id, max(d.posted_at) AS posted_at,
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
        return [{"sha256": sha, "path": path, "doc_id": doc_id, "posted_at": posted,
                 "members": dict(zip(paths or [], hashes or []))} for sha, path, doc_id, posted, paths, hashes in rows]

    def artifact_keys(self, layer: str) -> set[str]:
        return {k for (k,) in self.con.execute("SELECT artifact_key FROM artifact WHERE layer = ?", [layer]).fetchall()}

    def artifact_tables(self, layer: str, blob_sha256: str) -> list[str]:
        return [t for (t,) in self.con.execute(
            "SELECT DISTINCT table_name FROM artifact WHERE layer = ? AND blob_sha256 = ?", [layer, blob_sha256]).fetchall()]

    def artifact_paths(self, keys: Iterable[str]) -> list[str]:
        keys = list(keys)
        if not keys:
            return []
        return [p for (p,) in self.con.execute(
            "SELECT path FROM artifact WHERE list_contains($keys, artifact_key)", {"keys": keys}).fetchall()]

    def artifacts(self, layer: str | None = None) -> pl.DataFrame:
        query = "SELECT * FROM artifact" + ("" if layer is None else " WHERE layer = $layer") + " ORDER BY written_at"
        return self.con.execute(query, {"layer": layer} if layer else {}).pl()

    def start_run(self, command: str) -> str:
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.urandom(3).hex()}"
        self.con.execute("INSERT INTO run VALUES (?, ?, ?, ?, NULL, NULL)",
                         [run_id, command, client_version(), datetime.now(timezone.utc)])
        return run_id

    def finish_run(self, run_id: str, failed: int) -> None:
        self.con.execute("UPDATE run SET finished_at = ?, failed = ? WHERE run_id = ?",
                         [datetime.now(timezone.utc), failed, run_id])

    def add_artifacts(self, run_id: str, layer: str, emil_id: str, blob_sha256: str, parser: str, placed: list) -> None:
        """Register written files and their member lineage; replaces earlier rows for the same key."""
        self.con.begin()
        try:
            for key, artifact, rel in placed:
                self.con.execute("DELETE FROM lineage WHERE artifact_key = ?", [key])
                self.con.execute("DELETE FROM artifact WHERE artifact_key = ?", [key])
                # A parser change gives a new key for the same (blob, table); drop the stale row too.
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
