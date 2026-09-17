"""Measure how the keys in CRR and DAM packages line up, on real archived data.

Reads the archive and writes one JSON report per run to ``data/reports/identity/``
(``<crr month>_<dam day>_he<hour>.json``). Prints and records counts and *masked* name
patterns (letters -> A, digits -> 9), never names or values. The dataset notes describe
what these measurements mean; the numbers live here, dated, and are re-measured when a
new CRR month or DAM day arrives.

Checks, for one monthly CRR package and one DAM hour:
  * CRR: which files name lines by the RAW comment, which name transformers by the
    Autos workbook, source/sink weights, workbook coverage, ratings vs RAW rate A;
  * DAM: CSV <-> RAW keys, monitored flags, settlement point and hub buses;
  * CRR <-> DAM: bus numbers, bus names, branch names (exact / punctuation-free /
    prefix), contingency names, settlement points;
  * stability: DAM bus numbering across hours and days, CRR across months, and
    whether equipment names give a stable (station, kV) identity.

    uv run python scripts/measure_identity.py                       # latest month and day
    uv run python scripts/measure_identity.py --month 2026-09 --day 2026-09-15 --hour 12
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import zipfile
from datetime import date, datetime, timezone

import polars as pl

import ercot_mis as em
from ercot_mis.core import node
from ercot_mis.raw import crr, dam

# ------------------------------------------------------------------ helpers


def mask(value) -> str:
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", str(value)))


def norm(value) -> str | None:
    """Upper case, collapsed whitespace."""
    return re.sub(r"\s+", " ", str(value).strip().upper()) if value is not None else None


def norm2(value) -> str | None:
    """Letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", norm(value)) if value is not None else None


def patterns(values, k: int = 3) -> list[tuple[str, int]]:
    return collections.Counter(mask(v) for v in values if v is not None).most_common(k)


def names(frame: pl.DataFrame, column: str, f=norm) -> set[str]:
    return {f(v) for v in frame[column].to_list() if v is not None}


def keys(frame: pl.DataFrame, a: str, b: str, c: str) -> set[tuple]:
    """(from, to, circuit) triples with the circuit normalized."""
    return {(x, y, norm(z) or "") for x, y, z in zip(frame[a].to_list(), frame[b].to_list(), frame[c].to_list())
            if x is not None and y is not None}


def either(k: set[tuple]) -> set[tuple]:
    return k | {(b, a, c) for a, b, c in k}


REPORT: dict[str, dict] = {}
_SECTION = [""]


def section(title: str) -> None:
    _SECTION[0] = title
    print(f"\n== {title}")


def show(title: str, **counts) -> None:
    REPORT.setdefault(_SECTION[0], {})[title] = {k: (list(v) if isinstance(v, (set, tuple)) else v) for k, v in counts.items()}
    print(f"  {title}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


def numeric(column: str) -> pl.Expr:
    return pl.col(column).cast(pl.Int64, strict=False)


# ------------------------------------------------------------------ loading


def package_paths(mis: em.Session, emil_id: str) -> list:
    rows = mis.catalog.con.execute(
        """SELECT b.path FROM archive_blob b JOIN archive_source s USING (sha256)
           LEFT JOIN remote_doc d ON d.emil_id = s.emil_id AND d.doc_id = s.doc_id
           WHERE b.emil_id = ? GROUP BY b.path ORDER BY max(d.posted_at) DESC NULLS LAST""",
        [emil_id],
    ).fetchall()
    return [mis.data_dir / path for (path,) in rows]


def crr_package(mis: em.Session, month: date | None):
    for path in package_paths(mis, "NP7-800-M"):
        with zipfile.ZipFile(path) as z:
            months = {m.month for n in z.namelist() if (m := crr.classify_member(n)) and m.month}
        if month is None or month in months:
            return path, (month or max(months))
    raise SystemExit(f"no archived monthly CRR package for {month}")


def dam_package(mis: em.Session, day: date | None):
    for path in package_paths(mis, "NP4-500-SG"):
        with zipfile.ZipFile(path) as z:
            days = {m.operating_date for n in z.namelist() if (m := dam.classify_member(n)) and m.operating_date}
        if day is None or day in days:
            return path, (day or max(days))
    raise SystemExit(f"no archived DAM package for {day}")


def load_crr(path, kinds) -> dict[str, pl.DataFrame]:
    tables = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            member = crr.classify_member(name)
            if member and member.is_parsed and member.kind in kinds:
                for table, data in crr.parse_member(member, z.read(name)).items():
                    tables[table] = pl.from_arrow(data)
    return tables


def load_dam(path, hour: int, kinds) -> dict[str, pl.DataFrame]:
    tables = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            member = dam.classify_member(name)
            if member and member.is_parsed and member.kind in kinds and (member.hour is None or member.hour == hour):
                for table, data in dam.parse_member(member, z.read(name)).items():
                    tables[table] = pl.from_arrow(data)
    return tables


def bus_identity(tables) -> dict[int, tuple[str | None, float]]:
    bus = tables["psse_bus"]
    return dict(zip(bus["i"].to_list(), zip([norm(x) for x in bus["name"].to_list()], bus["basekv"].to_list())))


# ------------------------------------------------------------------ sections


def crr_internal(C: dict[str, pl.DataFrame]) -> None:
    section("CRR internal")
    bus, br, xf = C["psse_bus"], C["psse_branch"], C["psse_transformer"]
    show("bus", n=bus.height, distinct_name=bus["name"].n_unique(), distinct_name_kv=bus.select("name", "basekv").n_unique(),
         distinct_comment=bus["comment"].n_unique(), isolated_ide4=int((bus["ide"] == 4).sum()),
         name_patterns=patterns(bus["name"].to_list()), comment_patterns=patterns(bus["comment"].to_list()))
    show("branch", n=br.height, in_service=int((br["st"] == 1).sum()), x_le_1e4=int((br["x"].abs() <= 1e-4).sum()),
         comment_distinct=br["comment"].n_unique(), comment_patterns=patterns(br["comment"].to_list()))
    show("transformer", n=xf.height, in_service=int((xf["stat"] == 1).sum()), comment_distinct=xf["comment"].n_unique())
    line_names, xf_names, bus_names, bus_comments = names(br, "comment"), names(xf, "comment"), names(bus, "name"), names(bus, "comment")

    autos, lines = C["crr_mapping_autos"], C["crr_mapping_lines"]
    auto_names = names(autos, "crr_name")

    ctg = C["crr_contingencies"]
    show("contingencies", n_names=ctg["contingency"].n_unique(), rows=ctg.height, name_patterns=patterns(ctg["contingency"].to_list()),
         type_patterns=patterns(ctg["device_type"].to_list()), action_patterns=patterns(ctg["action"].to_list()))
    for (device_type,), sub in ctg.group_by("device_type"):
        devices = names(sub, "device_name")
        show(f"  devices type={mask(device_type)}", distinct=len(devices), in_raw_line_comment=len(devices & line_names),
             in_autos_workbook=len(devices & auto_names), in_bus=len(devices & (bus_names | bus_comments)),
             unmatched=len(devices - line_names - auto_names - bus_names - bus_comments))
    mon = C["crr_monitored_lines_and_transformers"]
    show("monitored", rows=mon.height, distinct_devices=mon["device_name"].n_unique(), tou_patterns=patterns(mon["time_of_use"].to_list()))
    for (device_type,), sub in mon.group_by("device_type"):
        devices = names(sub, "device_name")
        show(f"  devices type={mask(device_type)}", distinct=len(devices), in_raw_line_comment=len(devices & line_names),
             in_autos_workbook=len(devices & auto_names), unmatched=len(devices - line_names - auto_names))
    tou = mon.pivot(on="time_of_use", index="device_name", values="base_case_rating")
    tou_columns = [c for c in tou.columns if c != "device_name"]
    show("time-of-use ratings", devices=tou.height, blocks=len(tou_columns),
         devices_with_identical_ratings=sum(1 for row in tou.select(tou_columns).rows() if len(set(row)) == 1))
    peak = mon.filter(pl.col("time_of_use").str.to_uppercase() == "PEAKWD")
    joined = peak.join(br.select(pl.col("comment").alias("device_name"), "ratea", "rateb", "ratec", "x"), on="device_name")
    ratio = joined["base_case_rating"] / joined["ratea"]
    emergency = joined["emergency_rating"] / joined["ratea"]
    show("ratings vs RAW (PeakWD lines)", joined=joined.height,
         base_over_ratea_quantiles=[round(float(ratio.quantile(q)), 3) for q in (0, 0.05, 0.5, 0.95, 1)],
         emergency_over_ratea_quantiles=[round(float(emergency.quantile(q)), 3) for q in (0, 0.05, 0.5, 0.95, 1)],
         monitored_with_x_le_1e4=int((joined["x"].abs() <= 1e-4).sum()))
    show("coverage", branches_in_service=int((br["st"] == 1).sum()),
         branches_monitored_peakwd=len(names(br.filter(pl.col("st") == 1), "comment") & names(peak, "device_name")),
         xf_in_service=int((xf["stat"] == 1).sum()),
         xf_monitored_peakwd=len(names(peak, "device_name") & auto_names))

    gtc = C["crr_non_thermal_constraints"]
    members = names(gtc, "device_name")
    show("gtc", n_gtcs=gtc["name"].n_unique(), member_rows=gtc.height, members_in_raw_line_comment=len(members & line_names),
         members_in_autos_workbook=len(members & auto_names), unmatched=len(members - line_names - auto_names),
         type_patterns=patterns(gtc["device_type"].to_list()), direction_patterns=patterns(gtc["flow_direction"].to_list()))

    ss = C["crr_sources_and_sinks"]
    sums = ss.group_by("name").agg(pl.col("participation_factor").sum().alias("s"), pl.len().alias("k"))
    show("sources_sinks", n_names=ss["name"].n_unique(), rows=ss.height, bus_in_raw_comment=len(names(ss, "bus_name") & bus_comments),
         bus_in_raw_name=len(names(ss, "bus_name") & bus_names), bus_unmatched=len(names(ss, "bus_name") - bus_names - bus_comments),
         names_summing_to_1=int(((sums["s"] - 1).abs() < 1e-3).sum()), names_summing_above_1=int((sums["s"] > 1.001).sum()),
         name_patterns=patterns(ss["name"].to_list()))

    with_numbers = lines.with_columns(numeric("from_number").alias("f"), numeric("to_number").alias("t"))
    ops_dupes = lines.group_by("operations_name").len().filter(pl.col("len") > 1)
    show("mapping_lines", rows=lines.height, tag_in_raw_line_comment=len(names(lines, "crr_tag") & line_names),
         tag_unmatched=len(names(lines, "crr_tag") - line_names), ops_distinct=lines["operations_name"].n_unique(),
         ops_shared_by_many_tags=ops_dupes.height, max_tags_per_ops=int(ops_dupes["len"].max()) if ops_dupes.height else 0,
         from_to_numeric=int((with_numbers["f"].is_not_null() & with_numbers["t"].is_not_null()).sum()),
         key_in_raw=len(either(keys(with_numbers.filter(pl.col("f").is_not_null() & pl.col("t").is_not_null()), "f", "t", "circuit_id")) & keys(br, "i", "j", "ckt")),
         raw_branches_without_tag=br.height - len(line_names & names(lines, "crr_tag")),
         tag_patterns=patterns(lines["crr_tag"].to_list()), ops_patterns=patterns(lines["operations_name"].to_list()))
    with_numbers = autos.with_columns(numeric("from_number").alias("f"), numeric("to_number").alias("t"))
    show("mapping_autos", rows=autos.height, name_in_raw_xf_comment=len(auto_names & xf_names),
         key_in_raw=len(either(keys(with_numbers.filter(pl.col("f").is_not_null() & pl.col("t").is_not_null()), "f", "t", "id")) & keys(xf, "i", "j", "ckt")),
         name_patterns=patterns(autos["crr_name"].to_list()), ops_patterns=patterns(autos["operations_name"].to_list()))


def dam_internal(D: dict[str, pl.DataFrame]) -> None:
    section("DAM internal (one hour)")
    bus, br, xf = D["psse_bus"], D["psse_branch"], D["psse_transformer"]
    groups = bus.group_by("name", "basekv").len()
    show("bus", n=bus.height, distinct_name=bus["name"].n_unique(), distinct_name_kv=groups.height,
         buses_sharing_name_kv=int(groups.filter(pl.col("len") > 1)["len"].sum()), max_per_name_kv=int(groups["len"].max()),
         isolated_ide4=int((bus["ide"] == 4).sum()), slack_ide3=int((bus["ide"] == 3).sum()), name_patterns=patterns(bus["name"].to_list()))
    ln, dx, dc, sp, hb, gn, ld = (D[k] for k in ("dam_lines", "dam_transformers", "dam_contingencies", "dam_settlement_points",
                                                  "dam_hub_buses", "dam_generators", "dam_loads"))
    ln_keys = keys(ln, "psse_from_bus_number", "psse_to_bus_number", "psse_ckt_id")
    xf_keys = keys(dx, "psse_from_bus_number", "psse_to_bus_number", "psse_ckt_id")
    bus_numbers = set(bus["i"].to_list())
    bus_name = dict(zip(bus["i"].to_list(), [norm(x) for x in bus["name"].to_list()]))
    show("lines", csv_rows=ln.height, raw_rows=br.height, key_match=len(ln_keys & keys(br, "i", "j", "ckt")),
         raw_out_of_service=int((br["st"] != 1).sum()), raw_ratea_zero=int((br["ratea"] == 0).sum()), raw_x_le_1e4=int((br["x"].abs() <= 1e-4).sum()),
         branch_name_distinct=ln["branch_name"].n_unique(), name_patterns=patterns(ln["branch_name"].to_list()),
         from_station_eq_raw_bus_name=sum(1 for b, s in zip(ln["psse_from_bus_number"].to_list(), ln["from_station_name_psse_bus_name"].to_list()) if bus_name.get(b) == norm(s)))
    show("transformers", csv_rows=dx.height, raw_rows=xf.height, key_match=len(xf_keys & keys(xf, "i", "j", "ckt")),
         raw_out_of_service=int((xf["stat"] != 1).sum()), name_patterns=patterns(dx["branch_name"].to_list()))
    for label, frame in (("lines", ln), ("transformers", dx)):
        combos = frame.group_by("monitored", "monitored_and_secured").len()
        show(f"monitored flags {label}", combos=[(mask(a), mask(b), n) for a, b, n in combos.rows()])
    show("contingencies", n_names=dc["contingency_name"].n_unique(), rows=dc.height, type_patterns=patterns(dc["equipment_type"].to_list(), 5),
         operation_patterns=patterns(dc["contingency_operation"].to_list()), name_patterns=patterns(dc["contingency_name"].to_list()))
    gen_ids = {(b, norm(i) or "") for b, i in zip(D["psse_generator"]["i"].to_list(), D["psse_generator"]["id"].to_list())}
    load_ids = {(b, norm(i) or "") for b, i in zip(D["psse_load"]["i"].to_list(), D["psse_load"]["id"].to_list())}
    for (equipment_type,), sub in dc.group_by("equipment_type"):
        k = keys(sub, "psse_from_bus_number", "psse_to_bus_number", "psse_ckt_id")
        ids = {(b, norm(i) or "") for b, i in zip(sub["psse_gen_or_load_or_sp_bus_number"].to_list(), sub["psse_gen_or_load_id"].to_list()) if b is not None}
        show(f"  rows type={mask(equipment_type)}", rows=sub.height, branch_keys=len(k), in_lines_or_xf=len(either(k) & (ln_keys | xf_keys)),
             bus_ids=len(ids), in_raw_generators=len(ids & gen_ids), in_raw_loads=len(ids & load_ids), bus_in_raw=len({b for b, _ in ids} & bus_numbers))
    show("settlement_points", rows=sp.height, type_patterns=patterns(sp["settlement_point_type"].to_list(), 6),
         bus_null=sp["psse_bus_number"].null_count(), bus_in_raw=len(set(sp["psse_bus_number"].drop_nulls().to_list()) & bus_numbers))
    show("hubs", rows=hb.height, hubs=hb["hub_name"].n_unique(), bus_in_raw=len(set(hb["psse_bus_number"].drop_nulls().to_list()) & bus_numbers))
    show("generators", rows=gn.height, gen_bus_id_in_raw=len({(b, norm(i) or "") for b, i in zip(gn["psse_bus_number"].to_list(), gn["psse_gen_id"].to_list())} & gen_ids),
         resource_node_bus_null=gn["resource_node_psse_bus_number"].null_count(),
         resource_node_sp_in_sp_file=len(names(gn, "resource_node_settlement_point_name") & names(sp, "settlement_point_name")))
    show("loads", rows=ld.height, ldf_mw_sum=round(float(ld["raw_mw_ldf"].sum()), 1), load_zones=ld["load_zone_name"].n_unique())
    spc = D["dam_settlement_point_contingencies"]
    show("sp_contingencies", rows=spc.height, names_not_in_ctg=len(names(spc, "contingency_name") - names(dc, "contingency_name")))


def cross(C: dict[str, pl.DataFrame], D: dict[str, pl.DataFrame]) -> None:
    section("CRR vs DAM")
    c_bus, d_bus = bus_identity(C), bus_identity(D)
    shared = set(c_bus) & set(d_bus)
    show("bus numbers", crr=len(c_bus), dam=len(d_bus), shared=len(shared),
         shared_same_name=sum(1 for b in shared if c_bus[b][0] == d_bus[b][0]),
         shared_same_name_kv=sum(1 for b in shared if c_bus[b] == d_bus[b]))
    c_names, d_names = {v[0] for v in c_bus.values()}, {v[0] for v in d_bus.values()}
    show("bus names", crr=len(c_names), dam=len(d_names), shared=len(c_names & d_names))
    ln, dx = D["dam_lines"], D["dam_transformers"]
    dam_names = names(ln, "branch_name", norm2)
    ops = names(C["crr_mapping_lines"], "operations_name", norm2)
    exact = ops & dam_names
    rest = ops - dam_names
    show("workbook Operations_Name vs DAM Branch Name", ops=len(ops), exact=len(names(C["crr_mapping_lines"], "operations_name") & names(ln, "branch_name")),
         letters_digits_only=len(exact), prefix_of_a_dam_name=sum(1 for o in rest if any(d.startswith(o) for d in dam_names)),
         unmatched=len(rest), dam_names_embedding_a_station=sum(
             1 for n, f, t in zip(ln["branch_name"].to_list(), ln["from_station_name_psse_bus_name"].to_list(), ln["to_station_name_psse_bus_name"].to_list())
             if norm2(f) in norm2(n) or norm2(t) in norm2(n)))
    auto_ops = names(C["crr_mapping_autos"], "operations_name")
    show("workbook Autos Operations_Name vs DAM transformer Branch Name", ops=len(auto_ops), exact=len(auto_ops & names(dx, "branch_name")))
    show("branch keys (from, to, ckt)", crr=len(keys(C["psse_branch"], "i", "j", "ckt")), dam=len(keys(D["psse_branch"], "i", "j", "ckt")),
         shared=len(either(keys(C["psse_branch"], "i", "j", "ckt")) & keys(D["psse_branch"], "i", "j", "ckt")))
    c_ctg, d_ctg = names(C["crr_contingencies"], "contingency"), names(D["dam_contingencies"], "contingency_name")
    show("contingency names", crr=len(c_ctg), dam=len(d_ctg), shared=len(c_ctg & d_ctg))
    ss, sp = C["crr_sources_and_sinks"], D["dam_settlement_points"]
    show("settlement points", crr_source_sink_names=len(names(ss, "name")), dam_sps=len(names(sp, "settlement_point_name")),
         crr_names_in_dam=len(names(ss, "name") & names(sp, "settlement_point_name")))
    show("monitored", crr_peakwd_devices=C["crr_monitored_lines_and_transformers"].filter(pl.col("time_of_use").str.to_uppercase() == "PEAKWD").height,
         dam_lines=ln.height, dam_transformers=dx.height)
    show("gtc", crr_gtcs=C["crr_non_thermal_constraints"]["name"].n_unique(), dam_gtc_tables=[t for t in D if "constraint" in t])


def stability(mis: em.Session, dam_path, day: date, hour: int, D, C, crr_month: date) -> None:
    section("Stability of identity")

    def compare(label: str, a: dict, b: dict) -> None:
        shared = set(a) & set(b)
        unique_a = {v: k for k, v in a.items() if list(a.values()).count(v) == 1}
        unique_b = {v: k for k, v in b.items() if list(b.values()).count(v) == 1}
        both = set(unique_a) & set(unique_b)
        show(label, buses_a=len(a), buses_b=len(b), shared_numbers=len(shared), same_name_kv=sum(1 for k in shared if a[k] == b[k]),
             uniquely_named_in_both=len(both), keeping_their_number=sum(1 for v in both if unique_a[v] == unique_b[v]))

    def equipment(tables) -> dict[str, dict]:
        bus = bus_identity(tables)
        out = {}
        for label, table, name_col, bus_col in (("generator", "dam_generators", "generator_name", "psse_bus_number"),
                                                ("load", "dam_loads", "load_name", "psse_bus_number"),
                                                ("settlement_point", "dam_settlement_points", "settlement_point_name", "psse_bus_number")):
            frame = tables[table]
            out[label] = {norm(n): bus.get(b) for n, b in zip(frame[name_col].to_list(), frame[bus_col].to_list()) if b is not None}
        ln = tables["dam_lines"]
        out["branch"] = {norm(n): (norm(f), norm(t), fk, tk) for n, f, t, fk, tk in zip(
            ln["branch_name"].to_list(), ln["from_station_name_psse_bus_name"].to_list(), ln["to_station_name_psse_bus_name"].to_list(),
            ln["from_psse_kv"].to_list(), ln["to_psse_kv"].to_list())}
        return out

    kinds = {"network_model", "generators", "loads", "settlement_points", "lines"}
    next_hour = load_dam(dam_path, hour + 1 if hour < 24 else hour - 1, kinds)
    compare(f"DAM bus numbers, hour {hour} vs next hour", bus_identity(D), bus_identity(next_hour))
    for label, x, y in ((k, equipment(D)[k], equipment(next_hour)[k]) for k in ("generator", "load", "settlement_point", "branch")):
        common = set(x) & set(y)
        show(f"  {label} name -> (station, kV) across hours", shared=len(common), same=sum(1 for k in common if x[k] == y[k]))
    other_paths = [p for p in package_paths(mis, "NP4-500-SG") if p != dam_path]
    if other_paths:
        other = load_dam(other_paths[-1], hour, kinds)
        compare("DAM bus numbers vs the oldest archived day", bus_identity(D), bus_identity(other))
        for label, x, y in ((k, equipment(D)[k], equipment(other)[k]) for k in ("generator", "load", "settlement_point", "branch")):
            common = set(x) & set(y)
            show(f"  {label} name -> (station, kV) across days", shared=len(common), same=sum(1 for k in common if x[k] == y[k]))
    for path in package_paths(mis, "NP7-800-M"):
        with zipfile.ZipFile(path) as z:
            months = {m.month for n in z.namelist() if (m := crr.classify_member(n)) and m.month}
        if months and crr_month not in months:
            compare(f"CRR bus numbers, {crr_month:%Y-%m} vs {max(months):%Y-%m}", bus_identity(C), bus_identity(load_crr(path, {"network_model"})))
            break


def node_identity(mis: em.Session, dam_path, hour: int, D, C) -> None:
    """How stable the equipment-based node keys are (core/node.py)."""
    section("Node identity (equipment-based keys)")
    kinds = {"network_model", "generators", "loads", "settlement_points", "lines", "transformers"}

    def dam_keys(tables) -> pl.DataFrame:
        return node.dam_nodes(tables["psse_bus"], tables["dam_lines"], tables["dam_transformers"], tables["dam_generators"],
                                  tables["dam_loads"], tables["dam_settlement_points"])

    def compare(label: str, a: pl.DataFrame, b: pl.DataFrame) -> None:
        ka, kb = set(a["node_key"]), set(b["node_key"])
        show(label, nodes_a=a.height, nodes_b=b.height, keys_shared=len(ka & kb), only_a=len(ka - kb), only_b=len(kb - ka),
             ambiguous_a=int(a["is_ambiguous"].sum()), no_attachments_a=int((a["n_attachments"] == 0).sum()))

    here = dam_keys(D)
    compare(f"DAM hour {hour} vs next hour", here, dam_keys(load_dam(dam_path, hour + 1 if hour < 24 else hour - 1, kinds)))
    others = [p for p in package_paths(mis, "NP4-500-SG") if p != dam_path]
    if others:
        compare("DAM vs the oldest archived day", here, dam_keys(load_dam(others[-1], hour, kinds)))
    show("DAM attachments per node", quantiles=[int(here["n_attachments"].quantile(q)) for q in (0, 0.25, 0.5, 0.75, 1)],
         nodes_with_settlement_point=int(here["attachments"].str.contains("S:").sum()))

    crr = node.crr_nodes(C["psse_bus"], C["psse_branch"], C["psse_transformer"], C["crr_mapping_autos"], C["crr_sources_and_sinks"])
    groups = crr.filter(pl.col("is_tie_member")).group_by("node_group").len()
    show("CRR contraction", buses=crr.height, nodes=crr["node_group"].n_unique(), tie_groups=groups.height,
         buses_in_tie_groups=int(groups["len"].sum()), largest_group=int(groups["len"].max()) if groups.height else 0,
         ambiguous=int(crr["is_ambiguous"].sum()), no_attachments=int((crr["n_attachments"] == 0).sum()))
    for path in package_paths(mis, "NP7-800-M"):
        with zipfile.ZipFile(path) as z:
            months = {m.month for n in z.namelist() if (m := crr.classify_member(n)) and m.month} if False else None
        other = load_crr(path, {"network_model", "mapping_document", "sources_and_sinks"})
        if other["psse_bus"].height != C["psse_bus"].height or not other["psse_bus"]["name"].equals(C["psse_bus"]["name"]):
            other_nodes = node.crr_nodes(other["psse_bus"], other["psse_branch"], other["psse_transformer"], other["crr_mapping_autos"], other["crr_sources_and_sinks"])
            ka, kb = set(crr["node_key"]), set(other_nodes["node_key"])
            show("CRR nodes vs another month", nodes_a=crr["node_group"].n_unique(), nodes_b=other_nodes["node_group"].n_unique(), keys_shared=len(ka & kb))
            break


def matching(mis: em.Session, crr_month: date, day: date, hour: int) -> None:
    """How the session's matchers do on the built core tables (needs build_core)."""
    section("Matching (core.match_*)")
    crr_id, dam_id = f"crr:monthly:{crr_month:%Y-%m}:r1", f"dam:{day}:he{hour:02d}:r1"
    snaps = set(mis.core("snapshot").select("snapshot_id").collect()["snapshot_id"])
    if crr_id not in snaps or dam_id not in snaps:
        show("skipped", reason="core tables not built for these snapshots", crr=crr_id, dam=dam_id)
        return
    for name, frame, left, right in (("branches", mis.match_branches(crr_id, dam_id), "crr_branch_id", "dam_branch_id"),
                                     ("nodes", mis.match_nodes(crr_id, dam_id), "crr_node_key", "dam_node_key"),
                                     ("contingencies", mis.match_contingencies(crr_id, dam_id), "crr_contingency_id", "dam_contingency_id")):
        counts = frame.group_by("match_method").agg(pl.len().alias("n"), pl.col(left).is_not_null().sum().alias("crr"), pl.col(right).is_not_null().sum().alias("dam")).sort("match_method")
        show(name, **{f"{m}": f"{n} (crr {c}, dam {d})" for m, n, c, d in counts.rows()})
    ctg = mis.match_contingencies(crr_id, dam_id).filter(pl.col("match_method") == "name")
    show("name-matched contingencies", n=ctg.height, identical_branch_sets=int(((ctg["n_shared_branches"] == ctg["n_crr_branches"]) & (ctg["n_crr_branches"] == ctg["n_dam_branches"])).sum()),
         no_shared_branch=int((ctg["n_shared_branches"] == 0).sum()), with_dam_load_gen_sp_rows=int((ctg["n_dam_other_rows"] > 0).sum()), split_bus=int(ctg["has_split_bus"].sum()))
    nodes = mis.match_nodes(crr_id, dam_id).filter(pl.col("match_method") != "unmatched")
    kv = (nodes.join(mis.core("node").filter(pl.col("snapshot_id") == crr_id).select("node_key", "kv").unique(subset=["node_key"]).collect(), left_on="crr_node_key", right_on="node_key")
          .join(mis.core("node").filter(pl.col("snapshot_id") == dam_id).select("node_key", pl.col("kv").alias("dam_kv")).collect(), left_on="dam_node_key", right_on="node_key"))
    show("matched nodes", n=nodes.height, same_kv=int((kv["kv"] == kv["dam_kv"]).sum()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--month", type=lambda s: date.fromisoformat(s + "-01"), default=None, help="CRR monthly model, YYYY-MM")
    parser.add_argument("--day", type=date.fromisoformat, default=None, help="DAM operating day")
    parser.add_argument("--hour", type=int, default=12)
    args = parser.parse_args()
    with em.open() as mis:
        crr_path, month = crr_package(mis, args.month)
        dam_path, day = dam_package(mis, args.day)
        print(f"CRR monthly model {month:%Y-%m}; DAM {day} hour {args.hour}")
        C = load_crr(crr_path, {"network_model", "contingencies", "monitored_lines_and_transformers", "non_thermal_constraints", "sources_and_sinks", "mapping_document"})
        D = load_dam(dam_path, args.hour, {"network_model", "contingencies", "generators", "hub_buses", "loads", "lines", "settlement_points", "transformers", "settlement_point_contingencies"})
        crr_internal(C)
        dam_internal(D)
        cross(C, D)
        stability(mis, dam_path, day, args.hour, D, C, month)
        node_identity(mis, dam_path, args.hour, D, C)
        matching(mis, month, day, args.hour)
        out = mis.data_dir / "reports" / "identity"
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = out / f"{month:%Y-%m}_{day}_he{args.hour:02d}.json"
        path.write_text(json.dumps({"measured_at": datetime.now(timezone.utc).isoformat(), "crr_month": f"{month:%Y-%m}",
                                    "dam_day": str(day), "hour": args.hour, "sections": REPORT}, indent=1, default=str))
        path.chmod(0o600)
        print(f"\nreport written to {path.relative_to(mis.data_dir.parent)}")


if __name__ == "__main__":
    main()
