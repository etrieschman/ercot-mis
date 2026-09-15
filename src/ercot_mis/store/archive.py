"""The archive: original ERCOT documents, content-addressed and never edited.

A document lives at ``archive/<EMIL ID>/<sha256><suffix>`` inside the data folder.
The path is derived from the bytes, so identical bytes are stored once, a revised
document (``_Upd``) is a separate file, and ERCOT's original file name (which
embeds the participant DUNS) never becomes a path. Files are written to a partial
folder and moved into place only once complete, then made read-only.

Zips are never unzipped to disk; their members are indexed by hash instead.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    path: Path  # relative to the data folder
    size_bytes: int
    created: bool  # False when identical bytes were already archived


@dataclass(frozen=True)
class Member:
    member_path: str
    member_sha256: str
    size_bytes: int


def blob_path(emil_id: str, sha256: str, suffix: str) -> Path:
    return Path("archive") / emil_id / f"{sha256}{suffix}"


def store(data_dir: Path, emil_id: str, chunks: Iterable[bytes], suffix: str) -> StoredBlob:
    """Stream bytes into the archive; nothing appears at the final path until complete."""
    partial = data_dir / "archive" / ".partial"
    partial.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=partial)
    try:
        digest, size = hashlib.sha256(), 0
        with os.fdopen(handle, "wb") as out:
            for chunk in chunks:
                digest.update(chunk)
                out.write(chunk)
                size += len(chunk)
        sha256 = digest.hexdigest()
        rel = blob_path(emil_id, sha256, suffix)
        dest = data_dir / rel
        if dest.exists():
            os.unlink(tmp)
            return StoredBlob(sha256, rel, size, created=False)
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(tmp, 0o400)
        os.replace(tmp, dest)
        return StoredBlob(sha256, rel, size, created=True)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def discard(data_dir: Path, blob: StoredBlob) -> None:
    """Remove a blob this run just created and then rejected. Never used on accepted blobs."""
    (data_dir / blob.path).unlink(missing_ok=True)


def index_members(path: Path) -> list[Member]:
    """Hash every file inside a zip without extracting it; non-zips have no members."""
    if not zipfile.is_zipfile(path):
        return []
    members = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            digest = hashlib.sha256()
            with archive.open(info) as member:
                for chunk in iter(lambda: member.read(CHUNK), b""):
                    digest.update(chunk)
            members.append(Member(info.filename, digest.hexdigest(), info.file_size))
    return members
