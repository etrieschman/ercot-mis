"""ercot-mis: fetch, cache and standardize ERCOT MIS data locally, with provenance."""

from pathlib import Path

from .config import ConfigError, Identity, load_identity, load_secret, resolve_data_dir, secure_permissions
from .mis import BudgetError, DownloadError, Mis, Probe
from .products import PRODUCTS, Product, get_product
from .sources.ews import EwsError, RemoteDoc

__version__ = "0.0.1"

__all__ = [
    "PRODUCTS",
    "BudgetError",
    "ConfigError",
    "DownloadError",
    "EwsError",
    "Identity",
    "Mis",
    "Probe",
    "Product",
    "RemoteDoc",
    "get_product",
    "load_identity",
    "load_secret",
    "open",
    "resolve_data_dir",
]


def open(data_dir: str | Path | None = None) -> Mis:  # noqa: A001 - em.open() is the API
    """Open the local data folder, creating it with owner-only permissions if needed.

    Resolution order: ``data_dir``, then ``$ERCOT_MIS_DATA``, then ``data/`` in this
    repository. Warns if the folder is inside a cloud-synced location.
    """
    path = resolve_data_dir(data_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_permissions(path)
    return Mis(path)
