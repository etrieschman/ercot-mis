#!/usr/bin/env python3
"""Refuse to commit anything that looks like ERCOT data or participant identifiers.

The repository is public and holds code only. This runs as the git pre-commit
hook (``git config core.hooksPath .githooks``) and over every tracked file in the
test suite. Stdlib only, so the hook works before an environment is synced.

    python3 tools/check_confidential.py            # every tracked file
    python3 tools/check_confidential.py --staged   # what is staged for commit

Findings name the file and line, never the matched text, so the check itself
cannot echo an identifier into a terminal or CI log.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MAX_BYTES = 1_000_000
SYNTHETIC_FIXTURES = "tests/fixtures/synthetic/"
BLOCKED_SUFFIXES = {".raw", ".pfx", ".p12", ".key", ".crt", ".pem", ".zip", ".xlsx"}

# A DUNS+4 is 13 digits and ERCOT file names zero-pad it to 16.
DIGIT_RUN = re.compile(rb"(?<![0-9A-Za-z])(\d{13}|\d{16})(?![0-9A-Za-z])")
# ERCOT API user IDs are API_ plus letters and digits (e.g. a date and a name). Requiring a
# digit and a word boundary keeps variable names like ERCOT_PUBLIC_API_USERNAME out.
API_USER = re.compile(rb"(?<![A-Za-z0-9_])API_(?=[A-Z0-9]*\d)[A-Z0-9]{8,}")
# Machine-generated from PyPI; its hashes produce false digit-run matches.
PATTERN_EXEMPT = {"uv.lock"}


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (duplicated from ercot_mis.config to stay stdlib-only)."""
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


# Secrets and identifiers whose exact values must never appear in a tracked file.
SECRET_VARIABLES = (
    "ERCOT_PUBLIC_API_USERNAME",
    "ERCOT_PUBLIC_API_PASSWORD",
    "ERCOT_PUBLIC_API_SUBSCRIPTION_KEY",
)


def identifiers(root: Path = ROOT) -> list[bytes]:
    """Exact values to block: DUNS (both spellings), API user, and Public API credentials."""
    values = {**read_dotenv(root / ".env"), **os.environ}
    duns = values.get("ERCOT_DUNS", "").strip()
    user = values.get("ERCOT_API_USER", "").strip()
    found = []
    if duns.isdigit():
        found += [duns.encode(), duns.zfill(16).encode()]
    if user.startswith("API_") and len(user) > 6:
        found.append(user.encode())
    for name in SECRET_VARIABLES:
        value = values.get(name, "").strip()
        if len(value) >= 6 and not value.startswith("<"):  # skip .env.example-style placeholders
            found.append(value.encode())
    return found


def _line(data: bytes, index: int) -> int:
    return data.count(b"\n", 0, index) + 1


def problems(
    paths: Iterable[str],
    read: Callable[[str], bytes | None],
    known: Iterable[bytes] = (),
) -> list[str]:
    """Every reason these paths must not be committed."""
    known = list(known)
    found = []
    for rel in paths:
        suffix = Path(rel).suffix.lower()
        if "eceii" in rel.lower():
            found.append(f"{rel}: file name mentions ECEII")
        if suffix in BLOCKED_SUFFIXES and not rel.startswith(SYNTHETIC_FIXTURES):
            found.append(f"{rel}: {suffix} files belong in data/, not the repository")

        data = read(rel)
        if data is None:
            continue
        if len(data) > MAX_BYTES:
            found.append(f"{rel}: {len(data):,} bytes; data files belong in data/")
        for ident in known:
            index = data.find(ident)
            if index >= 0:
                found.append(f"{rel}:{_line(data, index)}: contains a value from your .env (DUNS, API user or Public API credential)")
        if rel in PATTERN_EXEMPT:
            continue
        if match := DIGIT_RUN.search(data):
            found.append(f"{rel}:{_line(data, match.start())}: 13- or 16-digit number (looks like a DUNS)")
        if match := API_USER.search(data):
            found.append(f"{rel}:{_line(data, match.start())}: looks like an ERCOT API user ID")
    return found


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main(argv: list[str]) -> int:
    staged = "--staged" in argv
    if staged:
        paths = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").split("\n")

        def read(rel: str) -> bytes | None:
            result = subprocess.run(["git", "show", f":{rel}"], cwd=ROOT, capture_output=True)
            return result.stdout if result.returncode == 0 else None
    else:
        paths = _git("ls-files").split("\n")

        def read(rel: str) -> bytes | None:
            path = ROOT / rel
            return path.read_bytes() if path.is_file() else None

    found = problems([p for p in paths if p], read, identifiers())
    for line in found:
        print(line, file=sys.stderr)
    if found:
        print("\nBlocked: ercot-mis is public and holds code only.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
