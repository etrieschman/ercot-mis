"""``core.snapshot``: one row per network model, with the IDs the core layer is keyed by.

``dam:2026-10-14:he07:r1``, ``crr:monthly:2026-10:r1``,
``crr:annual:2029.1st6:seq6:2029-01:r2``. The revision ``r<n>`` orders packages that
describe the same logical model by posting time: a DAM operating day, a CRR month, an
annual term and sequence (``_Upd`` packages are revisions).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from ..raw import crr, dam

CRR_PRODUCTS = ("NP7-801-M", "NP7-800-M")
DAM_PRODUCT = "NP4-500-SG"


@dataclass(frozen=True)
class Logical:
    """What a package describes, from its member names."""

    kind: str  # "dam", "monthly", "annual"
    key: tuple  # groups packages that are revisions of one another
    months: tuple[date, ...]  # CRR months inside (annual: six); DAM: ()
    day: date | None  # DAM operating day


def logical(emil_id: str, member_paths: list[str]) -> Logical | None:
    if emil_id == DAM_PRODUCT:
        days = {m.operating_date for n in member_paths if (m := dam.classify_member(n)) and m.operating_date}
        return Logical("dam", (min(days),), (), min(days)) if days else None
    members = [m for n in member_paths if (m := crr.classify_member(n)) and m.month]
    if not members:
        return None
    months = tuple(sorted({m.month for m in members}))
    if any(m.auction == "annual" for m in members):
        first = next(m for m in members if m.auction == "annual")
        return Logical("annual", (first.term, first.sequence), months, None)
    return Logical("monthly", (months[0],), months, None)


def snapshots(session) -> pl.DataFrame:
    """Every snapshot the archive holds, with revision numbers; one row per model."""
    rows = []
    for emil_id in (*CRR_PRODUCTS, DAM_PRODUCT):
        packages = session.catalog.packages(emil_id)
        described = [(p, logical(emil_id, list(p["members"]))) for p in packages]
        described = [(p, l) for p, l in described if l is not None]
        by_key: dict[tuple, list] = {}
        for p, l in described:
            by_key.setdefault((l.kind, l.key), []).append((p, l))
        for (kind, key), group in by_key.items():
            ordered = sorted(group, key=lambda pl_: (pl_[0]["posted_at"] is None, pl_[0]["posted_at"] or 0, pl_[0]["sha256"]))
            for revision, (p, l) in enumerate(ordered, start=1):
                base = {"emil_id": emil_id, "doc_id": p["doc_id"], "blob_sha256": p["sha256"], "posted_at": p["posted_at"],
                        "model_kind": kind, "revision": revision}
                if kind == "dam":
                    hours = sorted({m.hour for n in p["members"] if (m := dam.classify_member(n)) and m.kind == "network_model" and m.hour})
                    for hour in hours:
                        rows.append({**base, "snapshot_id": f"dam:{l.day}:he{hour:02d}:r{revision}", "operating_date": l.day,
                                     "hour": hour, "month": None, "term": None, "sequence": None})
                else:
                    for month in l.months:
                        term, sequence = (key if kind == "annual" else (None, None))
                        prefix = f"crr:annual:{term}:seq{sequence}" if kind == "annual" else "crr:monthly"
                        rows.append({**base, "snapshot_id": f"{prefix}:{month:%Y-%m}:r{revision}", "operating_date": None,
                                     "hour": None, "month": month, "term": term, "sequence": sequence})
    schema = {"snapshot_id": pl.String, "emil_id": pl.String, "doc_id": pl.String, "blob_sha256": pl.String,
              "posted_at": pl.Datetime("us", "UTC"), "model_kind": pl.String, "revision": pl.Int64,
              "operating_date": pl.Date, "hour": pl.Int64, "month": pl.Date, "term": pl.String, "sequence": pl.Int64}
    return pl.DataFrame(rows, schema=schema).sort("snapshot_id") if rows else pl.DataFrame(schema=schema)
