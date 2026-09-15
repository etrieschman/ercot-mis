"""The session object every ercot-mis workflow goes through."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from .config import Identity, load_identity
from .products import Product, get_product
from .sources.ews import EwsClient, EwsError, RemoteDoc

DOCUMENT_SCHEMA = {
    "report_type_id": pl.Int64,
    "doc_id": pl.String,
    "file_name": pl.String,
    "report_group": pl.String,
    "operating_date": pl.String,
    "posted_at": pl.Datetime("us", "UTC"),
    "size_bytes": pl.Int64,
    "format": pl.String,
    "url": pl.String,
}


@dataclass(frozen=True)
class Probe:
    """What EWS holds for one product.

    ``summary`` is safe to print: counts, dates, bytes and report groups.
    ``documents`` holds the full listing, whose file names embed the participant
    DUNS, so keep it inside the data folder.
    """

    summary: dict
    documents: pl.DataFrame


class Mis:
    """A local ercot-mis data folder plus the clients that fill it."""

    def __init__(self, data_dir: Path, identity: Identity | None = None):
        self.data_dir = Path(data_dir)
        self._identity = identity
        self._ews: EwsClient | None = None

    def __repr__(self) -> str:
        return f"Mis({str(self.data_dir)!r})"

    @property
    def ews(self) -> EwsClient:
        """The EWS client, created on first use so reading local data never needs credentials."""
        if self._ews is None:
            self._ews = EwsClient(self._identity or load_identity())
        return self._ews

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
            raise NotImplementedError(
                f"{spec.emil_id} comes from the Public API, whose client arrives with the archive store."
            )
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


def _dedupe(docs: list[RemoteDoc]) -> list[RemoteDoc]:
    seen: dict[str, RemoteDoc] = {}
    for doc in docs:
        seen.setdefault(doc.doc_id or doc.url or f"{doc.file_name}|{doc.posted_at}", doc)
    return sorted(seen.values(), key=lambda d: (d.posted_at is None, d.posted_at or datetime.min.replace(tzinfo=timezone.utc)))


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
        {**asdict(d), "posted_at": d.posted_at.astimezone(timezone.utc) if d.posted_at else None}
        for d in docs
    ]
    return pl.DataFrame(rows, schema=DOCUMENT_SCHEMA)
