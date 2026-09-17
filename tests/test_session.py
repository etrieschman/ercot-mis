from datetime import datetime, timedelta, timezone

import pytest

from ercot_mis import Session, get_product
from ercot_mis.sources.ews import EwsError, RemoteDoc

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _doc(doc_id, days_ago, size=10, group="CRR Network Model (Monthly)"):
    return RemoteDoc(
        report_type_id=11205,
        doc_id=doc_id,
        file_name=f"doc_{doc_id}.zip",
        report_group=group,
        operating_date=(NOW - timedelta(days=days_ago)).date().isoformat(),
        posted_at=NOW - timedelta(days=days_ago),
        size_bytes=size,
        format="zip",
        url=f"https://example.invalid/?doclookupId={doc_id}",
    )


class FakeEws:
    def __init__(self, unbounded, older=None, older_error=None):
        self.unbounded, self.older, self.older_error = unbounded, older or [], older_error
        self.calls = []

    def get_reports(self, report_type_id, start=None, end=None):
        self.calls.append((report_type_id, start, end))
        if start is None:
            return list(self.unbounded)
        if self.older_error:
            raise EwsError(self.older_error)
        return list(self.older)


def _mis(tmp_path, fake):
    mis = Session(tmp_path)
    mis._ews = fake
    return mis


def test_probe_merges_listings_and_measures_depth(tmp_path):
    fake = FakeEws(unbounded=[_doc("a", 10), _doc("b", 200)], older=[_doc("b", 200), _doc("c", 900, size=5)])
    probe = _mis(tmp_path, fake).probe("np7-800-m", now=NOW)

    (_, start, end), = [c for c in fake.calls if c[1] is not None]
    assert end == NOW - timedelta(days=365)
    assert start < end

    s = probe.summary
    assert (s["n_docs"], s["n_docs_unbounded_listing"], s["n_docs_before_display_window"]) == (3, 2, 1)
    assert s["total_bytes"] == 25
    assert s["earliest_posted"] == NOW - timedelta(days=900)
    assert s["report_groups"] == {"CRR Network Model (Monthly)": 3}
    assert probe.documents["doc_id"].to_list() == ["c", "b", "a"]


def test_probe_records_a_refused_older_window(tmp_path):
    probe = _mis(tmp_path, FakeEws([], older_error="ReplyCode=ERROR")).probe(11205, now=NOW)
    assert probe.summary["older_window_error"] == "ReplyCode=ERROR"
    assert probe.summary["n_docs"] == 0 and probe.documents.height == 0
    assert probe.documents.schema["posted_at"].time_zone == "UTC"


def test_probe_refuses_public_api_products(tmp_path):
    with pytest.raises(NotImplementedError, match="Public API"):
        _mis(tmp_path, FakeEws([])).probe("NP4-190-CD")


def test_product_lookup():
    assert get_product(12354).emil_id == "SYS-608-CD"
    assert get_product("12354").take == "track"
    with pytest.raises(KeyError, match="Unknown product"):
        get_product("NP0-000")
