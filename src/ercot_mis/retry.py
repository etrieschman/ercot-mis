"""Retry transient failures with fixed back-off delays.

ERCOT's servers stall now and then (a read timeout on a 30 MB download is the
usual symptom). One retry inside the run costs seconds; leaving the document
for the next day's pull costs a day of the display window.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

# Seconds to wait before the second and third attempts. Tests shorten this.
DELAYS: tuple[float, ...] = (5.0, 20.0)


def retrying(
    call: Callable[[], T],
    transient: tuple[type[BaseException], ...],
    delays: Sequence[float] | None = None,
) -> T:
    """Run ``call``; on a ``transient`` error wait and try again, up to ``len(delays)`` times."""
    delays = DELAYS if delays is None else delays
    for attempt, delay in enumerate((*delays, None)):
        try:
            return call()
        except transient:
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")
