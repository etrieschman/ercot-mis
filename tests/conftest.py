import pytest

from ercot_mis.archive import documents


@pytest.fixture(autouse=True)
def no_retry_delays(monkeypatch):
    """Retries still happen in tests, but without waiting."""
    monkeypatch.setattr(documents, "DELAYS", (0.0, 0.0))
