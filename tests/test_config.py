import warnings
from pathlib import Path

import pytest

from ercot_mis import config


def test_data_dir_precedence(tmp_path):
    explicit, from_env = tmp_path / "explicit", tmp_path / "env"
    env = {config.DATA_ENV: str(from_env)}
    assert config.resolve_data_dir(explicit, env=env) == explicit.resolve()
    assert config.resolve_data_dir(env=env) == from_env.resolve()
    assert config.resolve_data_dir(env={}) == (config.REPO_ROOT / "data").resolve()


@pytest.mark.parametrize("synced", ["Library/Mobile Documents/com~apple~CloudDocs/x", "Library/CloudStorage/Dropbox/x"])
def test_warns_inside_synced_folders(synced):
    home = Path("/Users/someone")
    with pytest.warns(UserWarning, match="syncs to a cloud service"):
        config.warn_if_synced(home / synced, home=home)


def test_quiet_outside_synced_folders():
    home = Path("/Users/someone")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config.warn_if_synced(home / "dev" / "ercot-mis" / "data", home=home)
        config.warn_if_synced(Path("/Volumes/external/data"), home=home)


def _certs(tmp_path):
    cert, key = tmp_path / "api.crt", tmp_path / "api.key"
    cert.write_text("cert")
    key.write_text("key")
    return {"ERCOT_CERT": str(cert), "ERCOT_KEY": str(key)}


def test_identity_env_beats_dotenv_and_hides_identifiers(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comment\nexport ERCOT_DUNS='111'\nERCOT_API_USER=API_FILE\n")
    identity = config.load_identity(env={"ERCOT_DUNS": "222", **_certs(tmp_path)}, dotenv=dotenv)
    assert (identity.duns, identity.api_user) == ("222", "API_FILE")
    assert "222" not in repr(identity) and "API_FILE" not in repr(identity)


def test_identity_errors_say_how_to_fix(tmp_path):
    missing = tmp_path / "missing.env"
    with pytest.raises(config.ConfigError, match="ERCOT_DUNS and ERCOT_API_USER"):
        config.load_identity(env={}, dotenv=missing)
    with pytest.raises(config.ConfigError, match="starting with 'API_'"):
        config.load_identity(env={"ERCOT_DUNS": "1", "ERCOT_API_USER": "PERSONAL"}, dotenv=missing)
    with pytest.raises(config.ConfigError, match="Missing certificate files"):
        config.load_identity(
            env={"ERCOT_DUNS": "1", "ERCOT_API_USER": "API_X", "ERCOT_CERT": str(tmp_path / "nope.crt")},
            dotenv=missing,
        )
