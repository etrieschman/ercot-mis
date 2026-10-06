import importlib.util
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("daily_pull", ROOT / "scripts" / "daily_pull.py")
daily_pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(daily_pull)

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


def test_at_risk_counts_only_what_ercot_still_offers_and_we_lack():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    run_started = now - timedelta(minutes=5)

    class Catalog:
        def documents(self, emil_id):
            if emil_id != "NP4-500-SG":
                return pl.DataFrame(schema={"is_archived": pl.Boolean, "last_listed_at": pl.Datetime("us", "UTC"), "posted_at": pl.Datetime("us", "UTC")})
            return pl.DataFrame({
                "is_archived": [True, False, False, False],
                # archived; missing and listed today; missing but rolled off (not listed this run); missing without a posting time
                "last_listed_at": [now, now, now - timedelta(days=40), now],
                "posted_at": [now - timedelta(days=3), now - timedelta(days=29), now - timedelta(days=60), None],
            })

    class Session:
        def catalog(self):
            return Catalog()

    risk = daily_pull.at_risk(Session(), run_started)
    assert risk["unarchived_ews_documents"] == 1
    assert 1.5 < risk["days_until_oldest_rolls_off"] <= 2.0  # a 31-day window, posted 29 days ago
