"""The catalog: what ERCOT has listed, what is archived, and how it arrived.

One DuckDB file in the data folder. Rows are inserted or refreshed, never deleted,
so the catalog is also the history of what ERCOT offered and when. That matters
because EWS forgets everything older than a product's display window.
"""

from __future__ import annotations

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
)


def client_version() -> str:
    try:
        return version("ercot-mis")
    except PackageNotFoundError:
        return "unknown"


class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path))
        self.con.execute("SET TimeZone = 'UTC'")
        for statement in SCHEMA:
            self.con.execute(statement)

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
