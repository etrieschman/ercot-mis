import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from ercot_mis import BudgetError, Mis
from ercot_mis.sources.ews import EwsError, RemoteDoc

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def _zip(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


def _doc(doc_id, payload, days_ago=1, size=None, operating_date=None, name=None):
    return RemoteDoc(
        report_type_id=11205,
        doc_id=doc_id,
        file_name=name or f"doc_{doc_id}.zip",
        report_group="CRR Network Model (Monthly)",
        operating_date=operating_date or (NOW - timedelta(days=days_ago)).date().isoformat(),
        posted_at=NOW - timedelta(days=days_ago),
        size_bytes=len(payload) if size is None else size,
        format="zip",
        url=f"https://example.invalid/?doclookupId={doc_id}",
    )


class FakeSource:
    def __init__(self, docs_and_payloads, fail=()):
        self.docs = [doc for doc, _ in docs_and_payloads]
        self.payloads = {doc.url: payload for doc, payload in docs_and_payloads}
        self.fail = set(fail)
        self.downloads = []

    def list_documents(self, product, start=None, end=None):
        return list(self.docs)

    def download(self, url):
        self.downloads.append(url)
        if url in self.fail:
            raise EwsError("HTTP 500 downloading a document")
        data = self.payloads[url]
        yield data[:4]
        yield data[4:]


@pytest.fixture
def payloads():
    return {"a": _zip(**{"JAN/model.csv": b"a"}), "b": _zip(**{"FEB/model.csv": b"bb"}), "c": _zip(x=b"c")}


def _mis(tmp_path, source):
    mis = Mis(tmp_path)
    mis._ews = source
    return mis


def test_list_records_documents_and_refreshes_them(tmp_path, payloads):
    source = FakeSource([(_doc("a", payloads["a"]), payloads["a"])])
    with _mis(tmp_path, source) as mis:
        first = mis.list("NP7-800-M")
        second = mis.list("NP7-800-M")
    assert first.height == 1 and not first["is_archived"][0]
    assert second["first_listed_at"][0] == first["first_listed_at"][0]
    assert second["last_listed_at"][0] >= first["last_listed_at"][0]


def test_fetch_archives_indexes_and_resumes(tmp_path, payloads):
    docs = [(_doc(k, payloads[k], days_ago=i + 1), payloads[k]) for i, k in enumerate("abc")]
    source = FakeSource(docs, fail={docs[1][0].url})
    with _mis(tmp_path, source) as mis:
        result = mis.fetch("NP7-800-M")
        assert dict(zip(result["doc_id"], result["status"])) == {"a": "fetched", "b": "failed", "c": "fetched"}
        assert "file_name" not in result.columns
        assert mis.catalog.con.execute("SELECT count(*) FROM archive_member").fetchone()[0] == 2
        for sha in result.filter(pl.col("status") == "fetched")["sha256"]:
            assert (tmp_path / "archive" / "NP7-800-M" / f"{sha}.zip").is_file()

        source.fail.clear()
        source.downloads.clear()
        retry = mis.fetch("NP7-800-M")
        assert retry["doc_id"].to_list() == ["b"] and retry["status"].to_list() == ["fetched"]
        assert mis.fetch("NP7-800-M").height == 0
        assert mis.list("NP7-800-M")["is_archived"].all()


def test_size_mismatch_keeps_nothing(tmp_path, payloads):
    source = FakeSource([(_doc("a", payloads["a"], size=999), payloads["a"])])
    with _mis(tmp_path, source) as mis:
        result = mis.fetch("NP7-800-M")
        assert result["status"].to_list() == ["failed"]
        assert "listing says 999" in result["error"][0]
        assert not list((tmp_path / "archive" / "NP7-800-M").iterdir())
        assert mis.catalog.con.execute("SELECT count(*) FROM archive_blob").fetchone()[0] == 0


def test_budget_refuses_before_downloading(tmp_path, payloads):
    source = FakeSource([(_doc("a", payloads["a"], size=2_000_000_000), payloads["a"])])
    with _mis(tmp_path, source) as mis, pytest.raises(BudgetError, match="exceeds max_gb=1"):
        mis.fetch("NP7-800-M", max_gb=1)
    assert source.downloads == []


def test_operating_dates_select_days(tmp_path, payloads):
    docs = [(_doc(k, payloads[k], operating_date=f"2026-09-0{i + 1}"), payloads[k]) for i, k in enumerate("abc")]
    with _mis(tmp_path, FakeSource(docs)) as mis:
        result = mis.fetch("NP7-800-M", operating_dates=[date(2026, 9, 1), "2026-09-03"])
    assert sorted(result["doc_id"]) == ["a", "c"]


def test_tracked_products_are_listed_not_fetched(tmp_path):
    with _mis(tmp_path, FakeSource([])) as mis, pytest.raises(ValueError, match="tracked, not pulled"):
        mis.fetch("SYS-608-CD")


def test_ingest_links_local_files_to_listings_and_deduplicates(tmp_path, payloads):
    name = "man.00011205.example.20260817.doc_a.zip"
    source = FakeSource([(_doc("a", payloads["a"], name=name), payloads["a"])])
    local = tmp_path / "downloads"
    (local / "extracted").mkdir(parents=True)
    (local / name).write_bytes(payloads["a"])
    (local / "extracted" / "inner.zip").write_bytes(payloads["b"])  # inside an extracted copy: ignored
    (local / "readme.txt").write_text("not a document")

    with _mis(tmp_path / "data", source) as mis:
        mis.list("NP7-800-M")
        first = mis.ingest(local)
        again = mis.ingest(local)
        assert first.select("emil_id", "doc_id", "is_new_bytes").rows() == [("NP7-800-M", "a", True)]
        assert again["is_new_bytes"].to_list() == [False]
        assert mis.catalog.con.execute("SELECT count(*) FROM archive_source").fetchone()[0] == 1
        assert mis.fetch("NP7-800-M").height == 0
    assert source.downloads == []


def test_ingest_needs_a_product_for_unrecognized_names(tmp_path, payloads):
    path = tmp_path / "renamed.zip"
    path.write_bytes(payloads["a"])
    with _mis(tmp_path / "data", FakeSource([])) as mis:
        with pytest.raises(ValueError, match="pass product="):
            mis.ingest(path)
        assert mis.ingest(path, product="NP7-800-M")["doc_id"].to_list() == [None]


def test_listing_hides_file_names_and_urls(tmp_path, payloads):
    source = FakeSource([(_doc("a", payloads["a"]), payloads["a"])])
    with _mis(tmp_path, source) as mis:
        listed = mis.list("NP7-800-M")
        assert "file_name" not in listed.columns and "url" not in listed.columns
        # The catalog keeps them: fetch needs the URL and the suffix.
        assert mis.catalog.con.execute("SELECT count(file_name) FROM remote_doc").fetchone()[0] == 1


def test_transient_download_errors_are_retried_within_a_run(tmp_path, payloads):
    doc = _doc("a", payloads["a"])

    class Flaky(FakeSource):
        def download(self, url):
            self.downloads.append(url)
            if len(self.downloads) < 3:
                raise ConnectionError("read timed out")
            yield self.payloads[url]

    source = Flaky([(doc, payloads["a"])])
    with _mis(tmp_path, source) as mis:
        result = mis.fetch("NP7-800-M")
    assert result["status"].to_list() == ["fetched"] and len(source.downloads) == 3


def test_catalog_is_read_only_between_writes(tmp_path, payloads):
    import duckdb

    with _mis(tmp_path, FakeSource([(_doc("a", payloads["a"]), payloads["a"])])) as mis:
        mis.list("NP7-800-M")
        assert mis.catalog.read_only
        with pytest.raises(duckdb.Error):
            mis.catalog.con.execute("DELETE FROM remote_doc")
        assert oct(mis.catalog.path.stat().st_mode & 0o777) == "0o600"
