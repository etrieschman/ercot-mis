"""The assumptions register and the code agree: every choice the code makes names a row."""

import re
from dataclasses import fields
from pathlib import Path

from ercot_mis.core import match
from ercot_mis.out import network

REGISTER = Path(__file__).resolve().parents[1] / "docs" / "assumptions.md"
TYPES = {"stated", "measured", "forced", "option"}
STATUSES = {"verified", "open", "external"}


def rows() -> list[dict[str, str]]:
    out = []
    for line in REGISTER.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if re.fullmatch(r"[A-Z]+-\d\d", cells[0]):
            assert len(cells) == 7, f"{cells[0]}: expected 7 columns, found {len(cells)}"
            out.append(dict(zip(("id", "type", "assumption", "where", "ercot", "check", "status"), cells)))
    return out


def test_rows_are_well_formed():
    found = rows()
    ids = [r["id"] for r in found]
    assert found and len(ids) == len(set(ids))
    for r in found:
        assert r["type"] in TYPES, r["id"]
        assert r["status"].split(" ")[0] in STATUSES, r["id"]
        assert r["where"] and r["check"], r["id"]


def test_every_option_names_a_row():
    by_id = {r["id"]: r for r in rows()}
    cited = network.REGISTER_ROWS["option"]
    assert set(cited) == {f.name for f in fields(network.Options)}
    assert set(cited.values()) <= set(by_id)


def test_every_drop_reason_and_match_method_names_a_row():
    ids = {r["id"] for r in rows()}
    for module in (network, match):
        for table, cited in module.REGISTER_ROWS.items():
            assert set(cited.values()) <= ids, table


def test_an_unregistered_method_is_refused():
    import polars as pl
    import pytest

    with pytest.raises(ValueError, match="no row in docs/assumptions.md"):
        match._registered(pl.DataFrame({"match_method": ["exact", "guess"]}), "match_branch")
