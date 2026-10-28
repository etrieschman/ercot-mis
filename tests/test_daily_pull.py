import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_pull", ROOT / "scripts" / "daily_pull.py")
daily_pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_pull)

import ercot_mis as em
from ercot_mis.session import BUILD_SCHEMA


def _result(*statuses, rows=10, seconds=1.0):
    return pl.DataFrame(
        [{"emil_id": "X", "doc_id": str(i), "blob_sha256": "b" * 64, "status": s, "tables": 1,
          "rows": rows if s == "built" else None, "seconds": seconds, "error": "boom" if s == "failed" else None}
         for i, s in enumerate(statuses)],
        schema=BUILD_SCHEMA,
    )


class FakeSession:
    def __init__(self, raw=None, core=None):
        self.raw_calls, self._raw, self._core = [], raw, core

    def build_raw(self, emil_id):
        self.raw_calls.append(emil_id)
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw

    def build_core(self):
        if isinstance(self._core, Exception):
            raise self._core
        return self._core


def test_report_counts_failed_packages_and_prints_their_errors(capsys):
    assert daily_pull._report("build_raw X", _result("built", "skipped", "failed", "failed")) == 2
    out = capsys.readouterr().out
    assert "built 1 (10 rows, 1s), skipped 1, failed 2" in out
    assert out.count("boom") == 2


def test_build_layers_runs_every_parsed_product_then_core():
    session = FakeSession(raw=_result("skipped"), core=_result("built"))
    assert daily_pull.build_layers(session) == 0
    assert session.raw_calls == list(daily_pull.parsed_products())


def test_build_layers_counts_an_exception_as_one_failure_and_keeps_going(capsys):
    session = FakeSession(raw=RuntimeError("catalog locked"), core=_result("built", "failed"))
    assert daily_pull.build_layers(session) == len(daily_pull.parsed_products()) + 1
    assert "catalog locked" in capsys.readouterr().out


_DOCS_SCHEMA = {"is_archived": pl.Boolean, "last_listed_at": pl.Datetime("us", "UTC"), "posted_at": pl.Datetime("us", "UTC")}
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


class Catalog:
    def __init__(self, dam=None):
        self.dam = dam

    def documents(self, emil_id):
        if emil_id != "NP4-500-SG" or self.dam is None:
            return pl.DataFrame(schema=_DOCS_SCHEMA)
        return pl.DataFrame(self.dam, schema=_DOCS_SCHEMA)


class RiskSession:
    def __init__(self, dam=None, data_dir=None):
        self.catalog, self.data_dir = Catalog(dam), data_dir


def test_at_risk_counts_only_what_ercot_still_offers_and_we_lack():
    session = RiskSession({
        "is_archived": [True, False, False, False],
        # archived; missing inside the window; missing but rolled off; missing without a posting time
        "last_listed_at": [NOW] * 4,
        "posted_at": [NOW - timedelta(days=3), NOW - timedelta(days=29), NOW - timedelta(days=60), None],
    })
    risk = daily_pull.at_risk(session, {"NP4-500-SG": NOW}, NOW)
    assert risk["unarchived_ews_documents"] == 1
    assert risk["days_until_loss"] == 2.0  # a 31-day window, posted 29 days ago
    assert risk["nearest_product"] == "NP4-500-SG"


def test_at_risk_counts_down_from_the_last_clean_fetch_when_pulls_are_missed():
    captured = {"NP4-500-SG": NOW - timedelta(days=26), "NP7-801-M": NOW - timedelta(days=26)}
    risk = daily_pull.at_risk(RiskSession(), captured, NOW)
    assert risk["unarchived_ews_documents"] == 0
    assert risk["days_until_loss"] == 5.0 and risk["nearest_product"] == "NP4-500-SG"  # 31 - 26; CRR has 365
    assert daily_pull.is_urgent(risk)
    assert not daily_pull.is_urgent(daily_pull.at_risk(RiskSession(), {"NP4-500-SG": NOW - timedelta(days=1)}, NOW))
    assert not daily_pull.is_urgent(daily_pull.at_risk(RiskSession(), {}, NOW))  # nothing known, nothing claimed


def test_captured_round_trips_and_seeds_from_the_newest_listing(tmp_path):
    session = RiskSession({"is_archived": [True, True], "last_listed_at": [NOW - timedelta(days=2), NOW - timedelta(days=1)],
                           "posted_at": [NOW - timedelta(days=5), NOW - timedelta(days=4)]}, data_dir=tmp_path)
    assert daily_pull.load_captured(session) == {"NP4-500-SG": NOW - timedelta(days=1)}
    daily_pull.save_captured(session, {"NP4-500-SG": NOW})
    assert daily_pull.load_captured(session) == {"NP4-500-SG": NOW}


def test_fetch_products_records_a_clean_fetch_only(capsys):
    def fetched(*statuses):
        return pl.DataFrame({"doc_id": [str(i) for i in range(len(statuses))], "status": list(statuses),
                             "size_bytes": [1_000_000] * len(statuses), "error": ["boom" if s == "failed" else None for s in statuses]})

    class FetchSession:
        def fetch(self, emil_id, since, operating_dates=None, max_gb=None):
            if emil_id == "NP7-800-M":
                raise ConnectionError("network down")
            return fetched("fetched", "failed") if emil_id == "NP7-801-M" else fetched("fetched")

    specs = [em.PRODUCTS[e] for e in ("NP4-500-SG", "NP7-801-M", "NP7-800-M")]
    captured = {"NP7-801-M": NOW, "NP7-800-M": NOW}
    assert daily_pull.fetch_products(FetchSession(), specs, captured) == 2
    assert captured["NP4-500-SG"] > NOW and captured["NP7-801-M"] == NOW and captured["NP7-800-M"] == NOW
    assert "network down" in capsys.readouterr().out
