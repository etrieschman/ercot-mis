"""What both clients share: the record of a posted document, the download chunk, the retry rule for a stalled call, and ERCOT's posting times."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from ..clock import TZ

T = TypeVar("T")

CHUNK = 1 << 20

# Seconds to wait before the second and third attempts at a stalled call.
DELAYS: tuple[float, ...] = (5.0, 20.0)


@dataclass(frozen=True)
class RemoteDoc:
    """One posted document as a listing describes it, from EWS or the Public API archive."""

    report_type_id: int | None
    doc_id: str | None
    file_name: str
    report_group: str
    operating_date: str
    posted_at: datetime | None
    size_bytes: int
    format: str
    url: str


def retrying(call: Callable[[], T], transient: tuple[type[BaseException], ...]) -> T:
    """The result of ``call``, retried after each ``transient`` error (a stalled download, a 5xx reply) with the ``DELAYS`` between attempts."""
    for delay in (*DELAYS, None):
        try:
            return call()
        except transient:
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def posted_at(text: str | None) -> datetime | None:
    """ERCOT's posting time as a datetime: ISO text, read as ERCOT local time when it carries no offset; None when empty or unreadable."""
    if not text:
        return None
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=TZ)
