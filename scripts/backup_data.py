"""Back up what cannot be fetched again: the archive, the catalog and the hand-kept files.

    uv run python scripts/backup_data.py /Volumes/<drive>/ercot-mis
    uv run python scripts/backup_data.py <othermac>.local:backups/ercot-mis

EWS keeps nothing past a product's display window, so the archive on this machine is
the only copy of every older network model. This copies, without ever deleting
anything at the destination:

- ``archive/``: new files only. Names are content hashes and files never change.
- ``catalog.duckdb``: a consistent copy, taken while a read-only connection holds off
  the pull's writer for the second it takes. The copy it replaces is kept as
  ``catalog.duckdb.previous``.
- ``overrides/``, ``reports/`` and ``logs/``.

Not ``raw/``, ``core/`` or ``out/``: the next build makes them again from the archive,
and a stale Parquet file left in a backup would be read twice after a restore.
Credentials (``~/.ercot/``, ``.env``, the Keychain) are not copied either; see the
README's new-machine section.

Most of the archive is ECEII. The backup refuses a destination inside a cloud-synced
folder and, on this machine, one on a volume that is not encrypted. A destination on
another machine (``host:path``) cannot be checked: its disk must have FileVault on.

A missing destination (a drive not plugged in) is not a failure: the run says so and
exits 0, and a notification reminds you once the last good backup is older than
``STALE_DAYS``. The outcome goes to ``data/logs/last_backup.json``. Schedule it with
launchd: see scripts/launchd/ercot-mis.backup.plist.

To restore, copy the backup into an empty data folder and run the daily pull, which
rebuilds raw and core.
"""

import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daily_pull import notify  # noqa: E402

import ercot_mis as em  # noqa: E402
from ercot_mis.archive.catalog import Catalog  # noqa: E402
from ercot_mis.config import warn_if_synced  # noqa: E402

# The folders copied besides the archive; each is optional.
SMALL_FOLDERS = ("overrides", "reports", "logs")

# A missing destination for longer than this since the last good backup raises a notification.
STALE_DAYS = 7


class BackupRefused(RuntimeError):
    """The destination is not a safe place for Secure or ECEII data."""


def is_remote(destination: str) -> bool:
    """rsync's ``host:path``: a colon before the first slash."""
    return ":" in destination.split("/", 1)[0]


def is_encrypted(path: Path) -> bool | None:
    """Whether the volume holding ``path`` is encrypted; None where it cannot be told (not macOS)."""
    if sys.platform != "darwin":
        return None
    mount = subprocess.run(["df", "-P", str(path)], capture_output=True, text=True, check=True).stdout.splitlines()[-1].split()[-1]
    info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", mount], capture_output=True, check=True).stdout)
    return bool(info.get("FileVault") or info.get("Encryption"))


def check_local(destination: Path) -> bool:
    """Refuse a synced or unencrypted local destination; False when it is not there (a drive unplugged)."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_if_synced(destination)
    if caught:
        raise BackupRefused(str(caught[0].message))
    if not destination.parent.is_dir():
        return False
    destination.mkdir(mode=0o700, exist_ok=True)
    encrypted = is_encrypted(destination)
    if encrypted is False:
        raise BackupRefused(f"{destination} is on a volume that is not encrypted; ECEII must not leave this machine unencrypted.")
    if encrypted is None:
        print("  could not check the destination's encryption on this platform", flush=True)
    return True


def rsync(*args: str) -> None:
    subprocess.run(["rsync", *args], check=True)


def backup(data_dir: Path, destination: str) -> None:
    """Copy the archive, a consistent catalog and the small folders to ``destination``."""
    target = destination.rstrip("/")
    if (data_dir / "archive").is_dir():
        rsync("-a", "--ignore-existing", f"{data_dir / 'archive'}/", f"{target}/archive/")
    catalog_path = data_dir / "catalog.duckdb"
    if catalog_path.is_file():
        with tempfile.TemporaryDirectory() as staging:
            copy = Path(staging) / "catalog.duckdb"
            catalog = Catalog(catalog_path, read_only=True)  # waits out a writer, then holds the next one off
            try:
                shutil.copy2(catalog_path, copy)
            finally:
                catalog.close()
            copy.chmod(0o600)
            rsync("-a", "--backup", "--suffix=.previous", str(copy), f"{target}/catalog.duckdb")
    for folder in SMALL_FOLDERS:
        if (data_dir / folder).is_dir():
            rsync("-a", f"{data_dir / folder}/", f"{target}/{folder}/")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    destination = argv[0]
    started = datetime.now(timezone.utc)
    began = time.monotonic()
    print(f"backup {started:%Y-%m-%d %H:%M} UTC", flush=True)
    with em.open() as mis:
        data_dir = mis.data_dir
    status_path = data_dir / "logs" / "last_backup.json"
    previous = json.loads(status_path.read_text()) if status_path.is_file() else {}
    status = {"started_utc": started.isoformat(), "destination": destination, "last_success_utc": previous.get("last_success_utc")}
    try:
        present = True if is_remote(destination) else check_local(Path(destination).expanduser())
        if present:
            backup(data_dir, destination if is_remote(destination) else str(Path(destination).expanduser()))
            status.update(outcome="copied", last_success_utc=datetime.now(timezone.utc).isoformat())
            print(f"  copied to the destination, {time.monotonic() - began:.0f}s", flush=True)
        else:
            status["outcome"] = "destination missing"
            print("  destination missing (drive not plugged in?); nothing copied", flush=True)
    except (BackupRefused, subprocess.CalledProcessError, OSError) as error:
        status["outcome"] = f"failed: {type(error).__name__}: {error}"
        print(f"  FAILED {type(error).__name__}: {error}", flush=True)
    finally:
        status_path.parent.mkdir(mode=0o700, exist_ok=True)
        status_path.write_text(json.dumps(status))
    last = status["last_success_utc"]
    age = None if last is None else (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 86400
    if status["outcome"].startswith("failed"):
        notify("ercot-mis: backup failed", status["outcome"][:200])
        return 1
    if status["outcome"] == "destination missing" and (age is None or age > STALE_DAYS):
        notify("ercot-mis: no recent backup", "never backed up" if age is None else f"last good backup {age:.0f} days ago")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
