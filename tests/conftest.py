import pytest

from ercot_mis import retry


@pytest.fixture(autouse=True)
def no_retry_delays(monkeypatch):
    """Retries still happen in tests, but without waiting."""
    monkeypatch.setattr(retry, "DELAYS", (0.0, 0.0))
