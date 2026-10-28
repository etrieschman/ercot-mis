"""Layer 4: the layers tested against what ERCOT published.

Each check reads the archive's own ERCOT reports (prices, binding constraints, awards,
distribution factors) and compares them with what our network and tables say. A check
is a report, never a pass or fail: it writes JSON under ``data/reports/<check>/``.

- ``binding``: the DAM's binding constraints of one hour, resolved onto our network
- ``prices``: published prices rebuilt from shadow prices and our shift factors
- ``flows``: the DAM's awards pushed through our network, against ERCOT's flows
- ``injections``: whether the awards balance every hour
- ``ldf``: ERCOT's load distribution factors against the DAM load file
- ``network``: the DC solve on assembled networks, ties kept and contracted
- ``register``: a measurement for each row of ``docs/assumptions.md``
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_report(session, check: str, name: str, result: dict) -> Path:
    """Write ``result`` to ``data/reports/<check>/<name>.json`` with the time it was measured."""
    folder = session.data_dir / "reports" / check
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / f"{name.replace(':', '-')}.json"
    path.write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), **result}, indent=1, default=str))
    path.chmod(0o600)
    return path
