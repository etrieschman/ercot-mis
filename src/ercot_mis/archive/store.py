"""The archive: ERCOT's documents as posted, stored once each under their hash and never edited.

A document lives at ``archive/<EMIL id>/<sha256><suffix>`` in the data folder. Identical bytes
are stored once, a revision is a separate file, and ERCOT's file name (which embeds the
participant's DUNS) is never a path. A download lands in a partial folder and moves into place
when complete, read-only. Zips stay zipped; their members are indexed by hash.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .ews import CHUNK



@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    path: Path  # relative to the data folder
    size_bytes: int
    created: bool  # False when the bytes were already archived


@dataclass(frozen=True)
class Member:
    member_path: str
    member_sha256: str
    size_bytes: int


def blob_path(emil_id: str, sha256: str, suffix: str) -> Path:
    return Path("archive") / emil_id / f"{sha256}{suffix}"


def put(data_dir: Path, emil_id: str, chunks: Iterable[bytes], suffix: str) -> StoredBlob:
    """The stored blob for ``chunks``; nothing appears at its final path until every chunk is written."""
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
    """Remove a blob this run created and then rejected (a short download)."""
    (data_dir / blob.path).unlink(missing_ok=True)


def index_members(path: Path) -> list[Member]:
    """Every file inside a zip with its hash, read without extracting; a document that is not a zip has none."""
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
