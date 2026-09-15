import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_confidential", ROOT / "tools" / "check_confidential.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

# Built at runtime so this file never contains the patterns it tests for.
DUNS_LIKE = "1" * 13
PADDED = "0" * 3 + "7" * 13
API_LIKE = "API_" + "ABC12345"


def _check(files, known=()):
    return guard.problems(files, lambda rel: files[rel].encode() if files[rel] is not None else None, [k.encode() for k in known])


def test_clean_code_passes():
    assert _check({"src/x.py": "size = 43214180\nsha = 'a1b2c3'\n"}) == []
    assert _check({".env.example": "ERCOT_PUBLIC_API_USERNAME=<email>\nERCOT_API_USER=API_YOURID\n"}) == []


def test_flags_identifier_patterns_with_line_numbers_not_values():
    found = _check({"notes.md": f"ok\nduns {DUNS_LIKE}\nfile man.{PADDED}.zip\nuser {API_LIKE}\n"})
    assert "notes.md:2: 13- or 16-digit number (looks like a DUNS)" in found
    assert "notes.md:4: looks like an ERCOT API user ID" in found
    assert not any(DUNS_LIKE in f or API_LIKE in f for f in found)


def test_flags_real_identifiers_everywhere_even_in_uv_lock():
    real = "9" * 9
    found = _check({"uv.lock": f"x\n{real}\n", "src/y.py": DUNS_LIKE}, known=[real])
    assert found == [
        "uv.lock:2: contains a value from your .env (DUNS, API user or Public API credential)",
        "src/y.py:1: 13- or 16-digit number (looks like a DUNS)",
    ]


@pytest.mark.parametrize("path", ["data.RAW", "tests/api.key", "docs/model.xlsx", "pkg.zip", "docs/ECEII_notes.md"])
def test_flags_data_and_credential_files(path):
    assert _check({path: None})


def test_allows_synthetic_fixtures():
    assert _check({"tests/fixtures/synthetic/tiny_package.zip": "fake"}) == []


def test_identifiers_include_duns_spellings_api_user_and_public_api_secrets(tmp_path, monkeypatch):
    for name in ("ERCOT_DUNS", "ERCOT_API_USER", *guard.SECRET_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    secret = "s3cret-" + "x" * 8
    (tmp_path / ".env").write_text(
        f"ERCOT_DUNS={DUNS_LIKE}\nERCOT_API_USER={API_LIKE}\n"
        f"ERCOT_PUBLIC_API_PASSWORD={secret}\nERCOT_PUBLIC_API_USERNAME=<your email>\n"
    )
    assert guard.identifiers(tmp_path) == [
        DUNS_LIKE.encode(), ("000" + DUNS_LIKE).encode(), API_LIKE.encode(), secret.encode(),
    ]


def test_every_tracked_file_is_clean():
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if listed.returncode != 0 or not listed.stdout.strip():
        pytest.skip("not a git checkout with tracked files")
    paths = [p for p in listed.stdout.split("\n") if p]
    read = lambda rel: (ROOT / rel).read_bytes() if (ROOT / rel).is_file() else None
    assert guard.problems(paths, read, guard.identifiers(ROOT)) == []
