import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from ercot_mis.archive.catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("backup_data", ROOT / "scripts" / "backup_data.py")
backup_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_data)

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")


def _data(tmp_path):
    data = tmp_path / "data"
    (data / "archive" / "X-1").mkdir(parents=True)
    (data / "archive" / "X-1" / ("a" * 64 + ".zip")).write_bytes(b"zip")
    for folder in ("raw/t", "core/t", "logs"):
        (data / folder).mkdir(parents=True)
    (data / "raw" / "t" / "f.parquet").write_bytes(b"p")
    (data / "logs" / "daily_pull.log").write_text("line\n")
    Catalog(data / "catalog.duckdb").close()
    return data


def test_backup_copies_what_cannot_be_fetched_again_and_never_deletes(tmp_path):
    data, dest = _data(tmp_path), tmp_path / "dest"
    (dest / "archive" / "X-0").mkdir(parents=True)
    (dest / "archive" / "X-0" / "old.zip").write_bytes(b"old")
    backup_data.backup(data, str(dest))
    assert (dest / "archive" / "X-1" / ("a" * 64 + ".zip")).read_bytes() == b"zip"
    assert (dest / "archive" / "X-0" / "old.zip").exists()  # gone here, kept there
    assert (dest / "logs" / "daily_pull.log").exists()
    assert not (dest / "raw").exists() and not (dest / "core").exists()
    Catalog(dest / "catalog.duckdb", read_only=True).close()  # a usable catalog
    os.utime(data / "catalog.duckdb", (2_000_000_000, 2_000_000_000))  # the catalog changed since
    backup_data.backup(data, str(dest))
    assert (dest / "catalog.duckdb.previous").exists()


def test_check_local_refuses_synced_and_unencrypted_and_reports_a_missing_drive(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_data, "is_encrypted", lambda path: True)
    assert backup_data.check_local(tmp_path / "dest") is True
    assert backup_data.check_local(tmp_path / "unplugged" / "dest") is False
    with pytest.raises(backup_data.BackupRefused):
        backup_data.check_local(Path.home() / "Library" / "Mobile Documents" / "ercot-mis")
    monkeypatch.setattr(backup_data, "is_encrypted", lambda path: False)
    with pytest.raises(backup_data.BackupRefused):
        backup_data.check_local(tmp_path / "dest")


def test_is_remote_reads_rsync_host_syntax():
    assert backup_data.is_remote("othermac.local:backups/ercot-mis")
    assert not backup_data.is_remote("/Volumes/Drive/ercot-mis")
    assert not backup_data.is_remote("/Volumes/a:b/ercot-mis")
