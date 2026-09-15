import hashlib
import io
import stat
import zipfile

import pytest

from ercot_mis.store import archive


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


def test_store_is_content_addressed_read_only_and_deduplicated(tmp_path):
    data = b"network model bytes"
    first = archive.store(tmp_path, "NP7-800-M", [data[:5], data[5:]], ".zip")
    sha = hashlib.sha256(data).hexdigest()
    assert (first.sha256, first.size_bytes, first.created) == (sha, len(data), True)
    assert first.path.as_posix() == f"archive/NP7-800-M/{sha}.zip"
    stored = tmp_path / first.path
    assert stored.read_bytes() == data
    assert not stored.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)

    again = archive.store(tmp_path, "NP7-800-M", [data], ".zip")
    assert again.created is False and again.path == first.path
    assert list((tmp_path / "archive" / ".partial").iterdir()) == []


def test_interrupted_store_leaves_nothing(tmp_path):
    def chunks():
        yield b"partial"
        raise ConnectionError("dropped")

    with pytest.raises(ConnectionError):
        archive.store(tmp_path, "NP7-800-M", chunks(), ".zip")
    assert list((tmp_path / "archive" / ".partial").iterdir()) == []
    assert not (tmp_path / "archive" / "NP7-800-M").exists()


def test_index_members_hashes_files_without_extracting(tmp_path):
    path = tmp_path / "doc.zip"
    path.write_bytes(_zip({"JAN/model.csv": b"a,b\n1,2\n", "JAN/": b"", "notes.txt": b"x"}))
    members = {m.member_path: m for m in archive.index_members(path)}
    assert set(members) == {"JAN/model.csv", "notes.txt"}
    assert members["JAN/model.csv"].member_sha256 == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
    assert members["JAN/model.csv"].size_bytes == 8

    plain = tmp_path / "doc.csv"
    plain.write_text("not a zip")
    assert archive.index_members(plain) == []
