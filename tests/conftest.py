import pytest

from ercot_mis.archive import ews


@pytest.fixture(autouse=True)
def no_retry_delays(monkeypatch):
    """Retries still happen in tests, but without waiting."""
    monkeypatch.setattr(ews, "DELAYS", (0.0, 0.0))
