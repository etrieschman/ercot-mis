import io
import zipfile

import polars as pl
import pytest

from ercot_mis.archive import documents


def zipped(members: dict[str, bytes]) -> bytes:
    """A zip holding ``members`` (path to bytes): a synthetic ERCOT package."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


def bus_records(numbers, names, kv=138.0) -> pl.DataFrame:
    """``psse_bus`` rows: the numbers and names, at one voltage, all type 1."""
    return pl.DataFrame({"i": numbers, "name": names, "basekv": [kv] * len(numbers), "ide": [1] * len(numbers)})


@pytest.fixture(autouse=True)
def no_retry_delays(monkeypatch):
    """Retries still happen in tests, but without waiting."""
    monkeypatch.setattr(documents, "DELAYS", (0.0, 0.0))
