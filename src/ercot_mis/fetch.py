"""How documents arrive: a listing recorded in the catalog, a document streamed into the store, a local file adopted.

Each function takes a client and the data folder and holds the catalog's writer only while it
records a listing, a blob or an arrival, never across a download. ``Session`` calls them with
its clients.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from datetime import date, datetime, time, timezone
from pathlib import Path

import polars as pl
import requests

from . import store
from .catalog import writing
from .clock import TZ
from .ews import CHUNK, EwsError, retrying
from .products import Product, get_product

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
INGEST_SCHEMA = {
    "emil_id": pl.String,
    "doc_id": pl.String,
    "sha256": pl.String,
    "size_bytes": pl.Int64,
    "is_new_bytes": pl.Boolean,
}

# ERCOT names a download "man.<8-digit report type>.<participant>.<timestamp>.<name>".
ERCOT_NAME = re.compile(r"^man\.(\d{8})\.")

# Errors a second attempt may fix: a stalled connection, a short read, a 5xx reply.
TRANSIENT = (requests.RequestException, EwsError, ConnectionError, TimeoutError)


class BudgetError(RuntimeError):
    """The fetch would download more than ``max_gb``; nothing was downloaded."""


class DownloadError(RuntimeError):
    """A download arrived shorter or longer than its listing says; nothing was kept."""


def list_documents(client, spec: Product, data_dir: Path, since=None, until=None) -> pl.DataFrame:
    """The documents ERCOT lists for ``spec`` now, recorded in the catalog and returned as the catalog's rows, private columns included."""
    docs = client.list_documents(spec, bound(since), bound(until))
    with writing(data_dir) as writer:
        writer.record_listing(spec, docs, listed_at=datetime.now(timezone.utc))
        return writer.documents(spec.emil_id, [d.doc_id for d in docs if d.doc_id])


def fetch(client, spec: Product, data_dir: Path, since: date | datetime | str | None = None, until: date | datetime | str | None = None, *,
          operating_dates: Iterable[date | str] | None = None, max_gb: float | None = None) -> pl.DataFrame:
    """One row per listed document not yet archived: fetched, or failed with the error; a later call resumes.

    ``operating_dates`` keeps only those listing days (for NP4-500-SG the listing day is the posting day; the
    models inside are for the day after). ``max_gb`` refuses a larger fetch before downloading.
    """
    if not spec.is_fetched:
        raise ValueError(f"{spec.emil_id} is listed, not fetched: list() records its documents. Set is_fetched=True in products.py to download it.")
    wanted = list_documents(client, spec, data_dir, since, until).filter(~pl.col("is_archived"))
    if operating_dates is not None:
        days = sorted({as_date(d).isoformat() for d in operating_dates})
        wanted = wanted.filter(pl.col("operating_date").is_in(days))
    total = int(wanted["size_bytes"].sum())
    if max_gb is not None and total > max_gb * 1e9:
        raise BudgetError(
            f"{spec.emil_id}: {wanted.height} documents, {total / 1e9:.2f} GB exceeds max_gb={max_gb}. "
            "Narrow the window or raise the budget."
        )

    results = []
    for row in wanted.sort("posted_at").iter_rows(named=True):
        outcome = {k: row[k] for k in ("emil_id", "doc_id", "report_group", "operating_date", "posted_at", "size_bytes")}
        suffix = Path(row["file_name"] or "").suffix or (f".{row['format']}" if row["format"] else "")
        try:
            blob = retrying(
                lambda: archive(data_dir, spec, client.download(row["url"]), suffix, expected_size=row["size_bytes"]),
                TRANSIENT + (DownloadError,),
            )
            with writing(data_dir) as writer:
                writer.add_source(blob.sha256, spec.emil_id, row["doc_id"], "fetch", row["file_name"])
            results.append({**outcome, "sha256": blob.sha256, "status": "fetched", "error": None})
        except Exception as error:  # reported; the next call retries it
            results.append({**outcome, "sha256": None, "status": "failed", "error": f"{type(error).__name__}: {error}"})
    return pl.DataFrame(results, schema=FETCH_SCHEMA)


def ingest(data_dir: Path, path: str | Path, product: str | int | None = None) -> pl.DataFrame:
    """One row per ERCOT document adopted from ``path`` (a zip, or a folder's top-level zips).

    The product comes from ``product`` or from ERCOT's file name; a file is linked to its listing when the catalog has one by that name.
    """
    path = Path(path).expanduser()
    files = [path] if path.is_file() else sorted(
        p for p in path.iterdir() if p.is_file() and zipfile.is_zipfile(p)
    )
    rows = []
    for file in files:
        spec = get_product(product) if product is not None else _product_from_name(file.name)
        with file.open("rb") as handle:
            blob = archive(data_dir, spec, iter(lambda: handle.read(CHUNK), b""), file.suffix)
        with writing(data_dir) as writer:
            doc_id = writer.doc_id_for_name(spec.emil_id, file.name)
            writer.add_source(blob.sha256, spec.emil_id, doc_id, "ingest", file.name)
        rows.append({"emil_id": spec.emil_id, "doc_id": doc_id, "sha256": blob.sha256,
                     "size_bytes": blob.size_bytes, "is_new_bytes": blob.created})
    return pl.DataFrame(rows, schema=INGEST_SCHEMA)


def archive(data_dir: Path, spec: Product, chunks: Iterable[bytes], suffix: str, expected_size: int | None = None) -> store.StoredBlob:
    """The blob stored for ``chunks``, indexed in the catalog; a download of the wrong size is discarded and raises ``DownloadError``."""
    blob = store.put(data_dir, spec.emil_id, chunks, suffix)
    if expected_size and blob.size_bytes != expected_size:
        if blob.created:
            store.discard(data_dir, blob)
        raise DownloadError(f"received {blob.size_bytes:,} bytes but the listing says {expected_size:,}")
    with writing(data_dir) as writer:
        if not writer.has_blob(blob.sha256):
            writer.add_blob(blob, spec, store.index_members(data_dir / blob.path))
    return blob


def bound(value: date | datetime | str | None) -> datetime | None:
    """A posting-time bound as a datetime: a date is midnight ERCOT time, a naive datetime is ERCOT time."""
    if value is None:
        return None
    if isinstance(value, str):
        value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    return datetime.combine(value, time(), TZ)


def as_date(value: date | str) -> date:
    """The date of an ISO date string, a date or a datetime."""
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def _product_from_name(name: str) -> Product:
    match = ERCOT_NAME.match(name)
    if match is None:
        raise ValueError("Can't tell the product from a file name like this; pass product= (e.g. 'NP7-801-M').")
    return get_product(int(match.group(1)))
