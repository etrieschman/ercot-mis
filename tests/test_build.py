import io
import zipfile

import polars as pl
import pytest

from ercot_mis import Session
from ercot_mis.raw import build
from ercot_mis.raw import dam as dam_parser

from test_psse import MARKED, to_bare

CTG = b"Contingency,DeviceName,DeviceType,Action\r\nCTG_1,1 ALPHA 2 BRAVO 1,LINE,OPN\r\n"
DAM_LINES = (
    ", ".join(c.header for c in dam_parser.LINES)
    + "\n1, 1, 2, 1, IN-SERVICE, No, Yes, ALPHA, 138, BRAVO, 138, LINE_A, 0.001, 0.01, 0.02, 100, 110, 120\n"
).encode()


def _headers(columns) -> bytes:
    return (", ".join(c.header for c in columns) + "\n").encode()


def _workbook() -> bytes:
    from openpyxl import Workbook

    from ercot_mis.raw import crr as crr_parser

    book = Workbook()
    lines = book.active
    lines.title = "Lines"
    lines.append([c.header for c in crr_parser.MAPPING_LINES])
    autos = book.create_sheet("Autos")
    autos.append([c.header for c in crr_parser.MAPPING_AUTOS])
    autos.append(["XF_A", "OPS_XF_A", "PT", 1, "ALPHA", 2, "BRAVO", "T1"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buffer.getvalue()


def _monthly(tmp_path, suffix=b""):
    package = tmp_path / "man.00011205.x.20260901.pkg.zip"
    package.write_bytes(_zip({
        "2026.SEP.Monthly.Auction.NetworkModel_PeakWD.raw": MARKED.encode() + suffix,
        "2026.SEP.Monthly.Auction.Contingencies.CSV": CTG,
        "2026.SEP.Monthly.Auction.Contingencies.XML": b"<x/>",  # archived, not parsed
        "2026.SEP.Monthly.Auction.SourcesAndSinks.CSV": b"Name,PriceNode,BusName,ParticipationFactor\nSP_A,SP_A,1 ALPHA 1,1\n",
        "2026.SEP.Monthly.Auction.MappingDocument.xlsx": _workbook(),
    }))
    return package


def _dam(tmp_path):
    package = tmp_path / "man.00013070.x.20260915.pkg.zip"
    bare = to_bare(MARKED).encode()
    package.write_bytes(_zip({
        "DAM09152026_001.RAW": bare, "DAM09152026_002.RAW": bare,
        "DAM09152026_Ln_001.csv": DAM_LINES, "DAM09152026_Ln_002.csv": DAM_LINES.replace(b"\n1, ", b"\n2, "),
        "README_DAM09152026.txt": b"notes",
        **{f"DAM09152026_{kind}_{hour}.csv": _headers(columns) for hour in ("001", "002") for kind, columns in
           (("Xf", dam_parser.TRANSFORMERS), ("Gn", dam_parser.GENERATORS), ("Ld", dam_parser.LOADS), ("Sp", dam_parser.SETTLEMENT_POINTS))},
    }))
    return package


def test_build_raw_writes_identity_columns_and_provenance(tmp_path):
    with Session(tmp_path / "data") as mis:
        mis.ingest(_monthly(tmp_path))
        result = mis.build_raw("NP7-800-M")
        assert result["status"].to_list() == ["built"] and result["tables"][0] == 15  # 11 psse + ctg + sources + 2 workbook sheets

        bus = mis.raw("psse_bus").collect()
        assert bus.height == 2
        assert bus["emil_id"].to_list() == ["NP7-800-M"] * 2
        assert bus["auction"][0] == "monthly" and str(bus["month"][0]) == "2026-09-01" and bus["time_of_use"][0] == "PeakWD"
        assert bus["member_sha256"][0] and bus["blob_sha256"][0] == result["blob_sha256"][0]
        ctg = mis.raw("crr_contingencies").collect()
        assert ctg["contingency"].to_list() == ["CTG_1"] and ctg["time_of_use"][0] is None

        artifacts = mis.catalog.artifacts("raw")
        assert artifacts.height == 15 and set(artifacts["table_name"]) >= {"psse_bus", "crr_contingencies"}
        lineage = mis.catalog.con.execute("SELECT count(DISTINCT member_sha256) FROM lineage").fetchone()[0]
        assert lineage == 4  # RAW, two CSVs and the workbook; the XML fed nothing
        runs = mis.catalog.con.execute("SELECT command, failed FROM run").fetchall()
        assert runs == [("build_raw NP7-800-M", 0)]
        for path in artifacts["path"]:
            assert oct((tmp_path / "data" / path).stat().st_mode & 0o777) == "0o600"


def test_build_raw_skips_built_packages_and_rebuilds_on_parser_change(tmp_path, monkeypatch):
    with Session(tmp_path / "data") as mis:
        mis.ingest(_monthly(tmp_path))
        first = mis.build_raw("NP7-800-M")
        again = mis.build_raw("NP7-800-M")
        assert first["status"][0] == "built" and again["status"][0] == "skipped"
        assert mis.catalog.artifacts("raw").height == 15

        monkeypatch.setattr(build, "parser_id", lambda emil_id: "changed")
        rebuilt = mis.build_raw("NP7-800-M")
        assert rebuilt["status"][0] == "built"
        assert mis.catalog.artifacts("raw").height == 15  # replaced, not duplicated
        assert set(mis.catalog.artifacts("raw")["parser_id"]) == {"changed"}


def test_build_raw_dam_keeps_the_files_hour_and_adds_it_to_raw_tables(tmp_path):
    with Session(tmp_path / "data") as mis:
        mis.ingest(_dam(tmp_path))
        result = mis.build_raw("NP4-500-SG", workers=1)
        assert result["status"][0] == "built"
        bus = mis.raw("psse_bus").collect()
        assert sorted(bus["hour"].to_list()) == [1, 1, 2, 2] and str(bus["operating_date"][0]) == "2026-09-15"
        lines = mis.raw("dam_lines").collect()
        assert lines["hour"].to_list() == [1, 2] and "member_path" in lines.columns
        assert lines.columns.count("hour") == 1


def test_build_raw_reports_a_broken_package_without_stopping(tmp_path):
    with Session(tmp_path / "data") as mis:
        mis.ingest(_monthly(tmp_path))
        broken = tmp_path / "man.00011205.x.20260801.broken.zip"
        broken.write_bytes(_zip({"2026.AUG.Monthly.Auction.Contingencies.CSV": b"Wrong,Header\n1,2\n"}))
        mis.ingest(broken)
        result = mis.build_raw("NP7-800-M", workers=1)
        assert sorted(result["status"].to_list()) == ["built", "failed"]
        assert "header does not match" in result.filter(pl.col("status") == "failed")["error"][0]


def test_parser_id_names_the_versions_that_matter():
    assert build.parser_id("NP7-800-M") != build.parser_id("NP4-500-SG")
    assert build.parser_id("NP4-500-SG") == "ercot-mis=0.0.1|table=1|psse=1|dam=1"
    with pytest.raises(ValueError, match="no raw-layer parser"):
        build.parser_id("SYS-608-CD")


def test_build_core_snapshots_and_nodes(tmp_path):
    from ercot_mis.core.snapshot import snapshots

    with Session(tmp_path / "data") as mis:
        mis.ingest(_monthly(tmp_path))
        mis.ingest(_dam(tmp_path))
        mis.build_raw("NP7-800-M", workers=1)
        mis.build_raw("NP4-500-SG", workers=1)
        snaps = snapshots(mis)
        assert sorted(snaps["snapshot_id"]) == ["crr:monthly:2026-09:r1", "dam:2026-09-15:he01:r1", "dam:2026-09-15:he02:r1"]

        result = mis.build_core()
        assert result["status"].to_list() == ["built", "built"], result["error"].to_list()
        nodes = mis.core("node").collect()
        assert set(nodes["snapshot_id"]) == set(snaps["snapshot_id"])
        dam_nodes = nodes.filter(pl.col("snapshot_id").str.starts_with("dam"))
        assert dam_nodes.filter(pl.col("hour") == 1)["node_key"].to_list() == dam_nodes.filter(pl.col("hour") == 2)["node_key"].to_list() if "hour" in dam_nodes.columns else True
        h1 = set(dam_nodes.filter(pl.col("snapshot_id").str.contains("he01"))["node_key"])
        h2 = set(dam_nodes.filter(pl.col("snapshot_id").str.contains("he02"))["node_key"])
        assert h1 == h2 and len(h1) == 2
        assert mis.build_core()["status"].to_list() == ["skipped", "skipped"]
        assert mis.core("snapshot").collect().height == 3


def test_snapshot_revisions_order_packages_by_posting_time(tmp_path):
    from datetime import datetime, timezone

    from ercot_mis.core.snapshot import snapshots
    from ercot_mis.sources.ews import RemoteDoc

    with Session(tmp_path / "data") as mis:
        (tmp_path / "later").mkdir()
        first, second = _monthly(tmp_path), _monthly(tmp_path / "later", b"\r\n")
        for path in (first, second):
            mis.ingest(path)
        docs = [RemoteDoc(11205, str(i), path.name, "g", "2026-09-01", datetime(2026, 8, 1 + i, tzinfo=timezone.utc), path.stat().st_size, "zip", f"u{i}")
                for i, path in enumerate((first, second))]
        with mis._writer() as writer:
            writer.record_listing(__import__("ercot_mis").get_product("NP7-800-M"), docs, datetime.now(timezone.utc))
            for i, path in enumerate((first, second)):
                import hashlib
                writer.con.execute("UPDATE archive_source SET doc_id = ? WHERE sha256 = ?", [str(i), hashlib.sha256(path.read_bytes()).hexdigest()])
        snaps = snapshots(mis)
        assert snaps["snapshot_id"].to_list() == ["crr:monthly:2026-09:r1", "crr:monthly:2026-09:r2"]
        assert snaps["revision"].to_list() == [1, 2] and snaps["posted_at"][0] < snaps["posted_at"][1]
