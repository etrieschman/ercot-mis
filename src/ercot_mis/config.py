"""Where data lives, and who we are to ERCOT.

Neither the DUNS nor the API user ID is a secret the way a password is, but both
identify the market participant, and the certificate key is an unencrypted
private key. All of it comes from the environment (or a gitignored ``.env``) and
none of it is ever written into the repository.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DATA_ENV = "ERCOT_MIS_DATA"

# Meaningful for a source checkout (the intended install: uv add --editable).
REPO_ROOT = Path(__file__).resolve().parents[2]

# Relative to the home folder.
_SYNCED = ("Library/Mobile Documents", "Library/CloudStorage", "Dropbox", "Google Drive", "OneDrive")
_MAYBE_SYNCED = ("Desktop", "Documents")  # synced when iCloud "Desktop & Documents" is on


class ConfigError(RuntimeError):
    """Configuration is missing or unusable; the message says how to fix it."""


@dataclass(frozen=True)
class Identity:
    """Credentials for EWS. The identifiers are hidden from repr so they stay out of logs."""

    duns: str = field(repr=False)
    api_user: str = field(repr=False)
    cert: Path
    key: Path


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` reader: blank lines, comments and ``export`` are allowed."""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


KEYCHAIN_SERVICE = "ercot-mis"

# Values that may live in the macOS Keychain instead of the environment or .env:
#   security add-generic-password -s ercot-mis -a ERCOT_PUBLIC_API_PASSWORD -w
SECRET_VARIABLES = (
    "ERCOT_PUBLIC_API_USERNAME",
    "ERCOT_PUBLIC_API_PASSWORD",
    "ERCOT_PUBLIC_API_SUBSCRIPTION_KEY",
)


def keychain_secret(name: str, service: str = KEYCHAIN_SERVICE) -> str | None:
    """A generic password from the macOS Keychain (account ``name``), or None."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", name, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def load_secret(name: str, env: Mapping[str, str] | None = None, dotenv: Path | None = None) -> str | None:
    """A secret by name: environment, then ``.env``, then the Keychain. Never logged."""
    dotenv = REPO_ROOT / ".env" if dotenv is None else dotenv
    values = {**read_dotenv(dotenv), **(os.environ if env is None else env)}
    value = values.get(name, "").strip()
    if value and not value.startswith("<"):
        return value
    return keychain_secret(name)


def secure_permissions(data_dir: Path) -> None:
    """Make the data folder, its top-level entries and the archive's product folders owner-only.

    ``Path.mkdir(mode=...)`` applies the mode to the last component only, and launchd
    creates the log file with its own mode, so this runs on every ``open()``. Archived
    documents themselves are already read-only for the owner and are left alone.
    """
    targets = [data_dir]
    if data_dir.is_dir():
        for entry in data_dir.iterdir():
            targets.append(entry)
            if entry.is_dir():
                # Product folders under archive/; log and probe files elsewhere. Archived
                # documents (two levels down) are already 0400.
                targets += [p for p in entry.iterdir() if entry.name != "archive" or p.is_dir()]
    for path in targets:
        try:
            mode = path.stat().st_mode
            wanted = 0o700 if stat.S_ISDIR(mode) else 0o600
            if stat.S_IMODE(mode) != wanted:
                os.chmod(path, wanted)
        except OSError:
            continue


def resolve_data_dir(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Pick the data folder: explicit path, then ``$ERCOT_MIS_DATA``, then ``data/`` here."""
    env = os.environ if env is None else env
    if path is not None:
        chosen = Path(path)
    elif env.get(DATA_ENV):
        chosen = Path(env[DATA_ENV])
    elif (REPO_ROOT / "pyproject.toml").is_file():
        chosen = REPO_ROOT / "data"
    else:
        raise ConfigError(f"Set {DATA_ENV} to the folder where ercot-mis should keep data.")
    chosen = chosen.expanduser().resolve()
    warn_if_synced(chosen)
    return chosen


def warn_if_synced(path: Path, home: Path | None = None) -> None:
    """Warn when ``path`` sits in a folder that syncs to a cloud service."""
    home = Path.home() if home is None else home
    try:
        rel = path.relative_to(home)
    except ValueError:
        return
    posix = rel.as_posix()
    for synced in _SYNCED:
        if posix == synced or posix.startswith(synced + "/"):
            warnings.warn(
                f"{path} is inside ~/{synced}, which syncs to a cloud service. "
                f"Secure and ECEII data must stay local: set {DATA_ENV} to an unsynced folder.",
                stacklevel=3,
            )
            return
    if rel.parts and rel.parts[0] in _MAYBE_SYNCED:
        warnings.warn(
            f"{path} is inside ~/{rel.parts[0]}, which syncs if iCloud Desktop & Documents is on. "
            f"If it does, set {DATA_ENV} to an unsynced folder.",
            stacklevel=3,
        )


def load_identity(env: Mapping[str, str] | None = None, dotenv: Path | None = None) -> Identity:
    """Read DUNS, API user and certificate paths; real environment variables beat ``.env``."""
    dotenv = REPO_ROOT / ".env" if dotenv is None else dotenv
    values = {**read_dotenv(dotenv), **(os.environ if env is None else env)}

    duns = values.get("ERCOT_DUNS", "").strip()
    user = values.get("ERCOT_API_USER", "").strip()
    if not duns or not user:
        raise ConfigError(
            "Set ERCOT_DUNS and ERCOT_API_USER in the environment or in .env (see .env.example)."
        )
    if not user.startswith("API_"):
        raise ConfigError(
            "ERCOT_API_USER must be an API certificate user ID starting with 'API_'; "
            "EWS rejects personal certificates."
        )

    cert = Path(values.get("ERCOT_CERT", "~/.ercot/api.crt")).expanduser()
    key = Path(values.get("ERCOT_KEY", "~/.ercot/api.key")).expanduser()
    missing = [str(p) for p in (cert, key) if not p.is_file()]
    if missing:
        raise ConfigError(
            f"Missing certificate files: {', '.join(missing)}. See 'Credentials' in the README."
        )
    return Identity(duns=duns, api_user=user, cert=cert, key=key)
