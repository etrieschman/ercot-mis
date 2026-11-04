"""ercot-mis: fetch, archive and standardize ERCOT MIS data locally; every table records the ERCOT document it came from."""

from pathlib import Path

from .config import ConfigError, Identity, load_identity, load_secret, resolve_data_dir, secure_permissions
from .ews import EwsError, Probe, RemoteDoc
from .fetch import BudgetError, DownloadError
from .products import PRODUCTS, Product, get_product
from .session import Session

__version__ = "0.0.1"

__all__ = [
    "PRODUCTS",
    "BudgetError",
    "ConfigError",
    "DownloadError",
    "EwsError",
    "Identity",
    "Session",
    "Probe",
    "Product",
    "RemoteDoc",
    "get_product",
    "load_identity",
    "load_secret",
    "open",
    "resolve_data_dir",
]


def open(data_dir: str | Path | None = None) -> Session:  # em.open() is the API; it shadows the builtin on purpose
    """A ``Session`` on the data folder: ``data_dir``, else ``$ERCOT_MIS_DATA``, else ``data/`` in this repository; created owner-only if missing."""
    path = resolve_data_dir(data_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_permissions(path)
    return Session(path)
