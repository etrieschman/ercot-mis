"""How the keys of a CRR model and a DAM hour line up: the identity report.

For one monthly CRR model and one DAM hour, sections of counts and *masked* name patterns
(letters become A, digits 9), never names:

- CRR internal: which files name lines by the RAW comment and transformers by the ``Autos``
  workbook; source and sink weights; workbook coverage; ratings against RAW rate A.
- DAM internal: CSV and RAW keys, monitored flags, settlement point and hub buses.
- CRR against DAM: bus numbers, bus names, branch names (exact, letters and digits, prefix),
  contingency names, settlement points.
- Stability: DAM bus numbers across hours and days, CRR across months, and whether equipment
  names give a stable (substation, kV) identity.
- Matching and coverage: how ``core.match_*`` and ``core.coverage`` do on the pair.

The dataset notes say what these measure; the numbers live in ``data/reports/identity/``.
"""

from __future__ import annotations

import collections
import re

import polars as pl

from ..core import coverage, match, node
from . import write_report

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


class Report:
    """Sections of counts, printed as they are recorded."""

    def __init__(self, quiet: bool = False):
        self.sections: dict[str, dict] = {}
        self.current = ""
        self.quiet = quiet

    def section(self, title: str) -> None:
        self.current = title
        if not self.quiet:
            print(f"\n== {title}")

    def show(self, title: str, **counts) -> None:
        self.sections.setdefault(self.current, {})[title] = {k: (list(v) if isinstance(v, (set, tuple)) else v) for k, v in counts.items()}
        if not self.quiet:
            print(f"  {title}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


def numeric(column: str) -> pl.Expr:
    return pl.col(column).cast(pl.Int64, strict=False)


# ------------------------------------------------------------------ loading

CRR_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "crr_mapping_autos", "crr_mapping_lines", "crr_contingencies",
              "crr_monitored_lines_and_transformers", "crr_non_thermal_constraints", "crr_sources_and_sinks")
DAM_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "psse_generator", "psse_load", "dam_lines", "dam_transformers", "dam_contingencies",
              "dam_settlement_points", "dam_hub_buses", "dam_generators", "dam_loads", "dam_settlement_point_contingencies")


def tables(session, snapshot_id: str, names=None) -> dict[str, pl.DataFrame]:
    """One model's raw tables."""
    names = names or (DAM_TABLES if snapshot_id.startswith("dam:") else CRR_TABLES)
    return {t: session.raw_rows(t, snapshot_id) for t in names}


def latest(snaps: pl.DataFrame, **where) -> str | None:
    """The highest revision of the snapshot matching ``where`` (TOP-11), or None."""
    rows = snaps.filter(*[pl.col(k) == v for k, v in where.items()]).sort("revision")
    return rows["snapshot_id"][-1] if rows.height else None


def tenths(kv: pl.Series) -> int:
    """Buses whose base kV carries a tenths digit that is not part of a nominal level (13.8, 34.5, ...)."""
    digit = (kv * 10 % 10).round(0)
    return int((~digit.is_in([0, 5, 8])).sum())


def bus_identity(tables) -> dict[int, tuple[str | None, float]]:
    bus = tables["psse_bus"]
    return dict(zip(bus["i"].to_list(), zip([norm(x) for x in bus["name"].to_list()], bus["basekv"].to_list())))


# ------------------------------------------------------------------ sections


def crr_internal(C: dict[str, pl.DataFrame], r: Report) -> None:
    r.section("CRR internal")
    bus, br, xf = C["psse_bus"], C["psse_branch"], C["psse_transformer"]
    r.show("bus", n=bus.height, distinct_name=bus["name"].n_unique(), distinct_name_kv=bus.select("name", "basekv").n_unique(),
         basekv_with_tenths=tenths(bus["basekv"]),
         distinct_comment=bus["comment"].n_unique(), isolated_ide4=int((bus["ide"] == 4).sum()),
         name_patterns=patterns(bus["name"].to_list()), comment_patterns=patterns(bus["comment"].to_list()))
    r.show("branch", n=br.height, in_service=int((br["st"] == 1).sum()), x_le_1e4=int((br["x"].abs() <= 1e-4).sum()),
         comment_distinct=br["comment"].n_unique(), comment_patterns=patterns(br["comment"].to_list()))
    r.show("transformer", n=xf.height, in_service=int((xf["stat"] == 1).sum()), comment_distinct=xf["comment"].n_unique())
    line_names, xf_names, bus_names, bus_comments = names(br, "comment"), names(xf, "comment"), names(bus, "name"), names(bus, "comment")

    autos, lines = C["crr_mapping_autos"], C["crr_mapping_lines"]
    auto_names = names(autos, "crr_name")

    ctg = C["crr_contingencies"]
    r.show("contingencies", n_names=ctg["contingency"].n_unique(), rows=ctg.height, name_patterns=patterns(ctg["contingency"].to_list()),
         type_patterns=patterns(ctg["device_type"].to_list()), action_patterns=patterns(ctg["action"].to_list()))
    for (device_type,), sub in ctg.group_by("device_type"):
        devices = names(sub, "device_name")
        r.show(f"  devices type={mask(device_type)}", distinct=len(devices), in_raw_line_comment=len(devices & line_names),
             in_autos_workbook=len(devices & auto_names), in_bus=len(devices & (bus_names | bus_comments)),
             unmatched=len(devices - line_names - auto_names - bus_names - bus_comments))
    mon = C["crr_monitored_lines_and_transformers"]
    r.show("monitored", rows=mon.height, distinct_devices=mon["device_name"].n_unique(), tou_patterns=patterns(mon["time_of_use"].to_list()))
    for (device_type,), sub in mon.group_by("device_type"):
        devices = names(sub, "device_name")
        r.show(f"  devices type={mask(device_type)}", distinct=len(devices), in_raw_line_comment=len(devices & line_names),
             in_autos_workbook=len(devices & auto_names), unmatched=len(devices - line_names - auto_names))
    tou = mon.pivot(on="time_of_use", index="device_name", values="base_case_rating")
    tou_columns = [c for c in tou.columns if c != "device_name"]
    r.show("time-of-use ratings", devices=tou.height, blocks=len(tou_columns),
         devices_with_identical_ratings=sum(1 for row in tou.select(tou_columns).rows() if len(set(row)) == 1))
    peak = mon.filter(pl.col("time_of_use").str.to_uppercase() == "PEAKWD")
    joined = peak.join(br.select(pl.col("comment").alias("device_name"), "ratea", "rateb", "ratec", "x"), on="device_name")
    ratio = joined["base_case_rating"] / joined["ratea"]
    emergency = joined["emergency_rating"] / joined["ratea"]
    r.show("ratings vs RAW (PeakWD lines)", joined=joined.height,
         base_over_ratea_quantiles=[round(float(ratio.quantile(q)), 3) for q in (0, 0.05, 0.5, 0.95, 1)],
         emergency_over_ratea_quantiles=[round(float(emergency.quantile(q)), 3) for q in (0, 0.05, 0.5, 0.95, 1)],
         monitored_with_x_le_1e4=int((joined["x"].abs() <= 1e-4).sum()))
    r.show("coverage", branches_in_service=int((br["st"] == 1).sum()),
         branches_monitored_peakwd=len(names(br.filter(pl.col("st") == 1), "comment") & names(peak, "device_name")),
         xf_in_service=int((xf["stat"] == 1).sum()),
         xf_monitored_peakwd=len(names(peak, "device_name") & auto_names))

    gtc = C["crr_non_thermal_constraints"]
    members = names(gtc, "device_name")
    r.show("gtc", n_gtcs=gtc["name"].n_unique(), member_rows=gtc.height, members_in_raw_line_comment=len(members & line_names),
         members_in_autos_workbook=len(members & auto_names), unmatched=len(members - line_names - auto_names),
         type_patterns=patterns(gtc["device_type"].to_list()), direction_patterns=patterns(gtc["flow_direction"].to_list()))

    ss = C["crr_sources_and_sinks"]
    sums = ss.group_by("name").agg(pl.col("participation_factor").sum().alias("s"), pl.len().alias("k"))
    r.show("sources_sinks", n_names=ss["name"].n_unique(), rows=ss.height, bus_in_raw_comment=len(names(ss, "bus_name") & bus_comments),
         bus_in_raw_name=len(names(ss, "bus_name") & bus_names), bus_unmatched=len(names(ss, "bus_name") - bus_names - bus_comments),
         names_summing_to_1=int(((sums["s"] - 1).abs() < 1e-3).sum()), names_summing_above_1=int((sums["s"] > 1.001).sum()),
         name_patterns=patterns(ss["name"].to_list()))

    with_numbers = lines.with_columns(numeric("from_number").alias("f"), numeric("to_number").alias("t"))
    ops_dupes = lines.group_by("operations_name").len().filter(pl.col("len") > 1)
    r.show("mapping_lines", rows=lines.height, tag_in_raw_line_comment=len(names(lines, "crr_tag") & line_names),
         tag_unmatched=len(names(lines, "crr_tag") - line_names), ops_distinct=lines["operations_name"].n_unique(),
         ops_shared_by_many_tags=ops_dupes.height, max_tags_per_ops=int(ops_dupes["len"].max()) if ops_dupes.height else 0,
         from_to_numeric=int((with_numbers["f"].is_not_null() & with_numbers["t"].is_not_null()).sum()),
         key_in_raw=len(either(keys(with_numbers.filter(pl.col("f").is_not_null() & pl.col("t").is_not_null()), "f", "t",
                                    "circuit_id")) & keys(br, "i", "j", "ckt")),
         raw_branches_without_tag=br.height - len(line_names & names(lines, "crr_tag")),
         tag_patterns=patterns(lines["crr_tag"].to_list()), ops_patterns=patterns(lines["operations_name"].to_list()))
    with_numbers = autos.with_columns(numeric("from_number").alias("f"), numeric("to_number").alias("t"))
    r.show("mapping_autos", rows=autos.height, name_in_raw_xf_comment=len(auto_names & xf_names),
         key_in_raw=len(either(keys(with_numbers.filter(pl.col("f").is_not_null() & pl.col("t").is_not_null()), "f", "t", "id")) & keys(xf, "i", "j", "ckt")),
         name_patterns=patterns(autos["crr_name"].to_list()), ops_patterns=patterns(autos["operations_name"].to_list()))


def dam_internal(D: dict[str, pl.DataFrame], r: Report) -> None:
    r.section("DAM internal (one hour)")
    bus, br, xf = D["psse_bus"], D["psse_branch"], D["psse_transformer"]
    groups = bus.group_by("name", "basekv").len()
    r.show("bus", n=bus.height, distinct_name=bus["name"].n_unique(), distinct_name_kv=groups.height,
         buses_sharing_name_kv=int(groups.filter(pl.col("len") > 1)["len"].sum()), max_per_name_kv=int(groups["len"].max()),
         isolated_ide4=int((bus["ide"] == 4).sum()), slack_ide3=int((bus["ide"] == 3).sum()), name_patterns=patterns(bus["name"].to_list()),
         basekv_with_tenths=tenths(bus["basekv"]), distinct_name_kv_ignoring_tenths=bus.select("name", pl.col("basekv").floor()).n_unique())
    ln, dx, dc, sp, hb, gn, ld = (D[k] for k in ("dam_lines", "dam_transformers", "dam_contingencies", "dam_settlement_points",
                                                  "dam_hub_buses", "dam_generators", "dam_loads"))
    ln_keys = keys(ln, "psse_from_bus_number", "psse_to_bus_number", "psse_ckt_id")
    xf_keys = keys(dx, "psse_from_bus_number", "psse_to_bus_number", "psse_ckt_id")
    bus_numbers = set(bus["i"].to_list())
    bus_name = dict(zip(bus["i"].to_list(), [norm(x) for x in bus["name"].to_list()]))
    r.show("lines", csv_rows=ln.height, raw_rows=br.height, key_match=len(ln_keys & keys(br, "i", "j", "ckt")),
         raw_out_of_service=int((br["st"] != 1).sum()), raw_ratea_zero=int((br["ratea"] == 0).sum()), raw_x_le_1e4=int((br["x"].abs() <= 1e-4).sum()),
         raw_x_at_floor=int((br["x"].abs() == match.DAM_REACTANCE_FLOOR).sum()), raw_x_below_floor=int((br["x"].abs() < match.DAM_REACTANCE_FLOOR).sum()),
         raw_x_below_1e3=int((br["x"].abs() < 1e-3).sum()), raw_x_negative=int((br["x"] < 0).sum()),

         branch_name_distinct=ln["branch_name"].n_unique(), name_patterns=patterns(ln["branch_name"].to_list()),
         from_station_eq_raw_bus_name=sum(1 for b, s in zip(ln["psse_from_bus_number"].to_list(),
                                                            ln["from_station_name_psse_bus_name"].to_list()) if bus_name.get(b) == norm(s)))
    r.show("transformers", csv_rows=dx.height, raw_rows=xf.height, key_match=len(xf_keys & keys(xf, "i", "j", "ckt")),
         raw_out_of_service=int((xf["stat"] != 1).sum()), name_patterns=patterns(dx["branch_name"].to_list()))
    for label, frame in (("lines", ln), ("transformers", dx)):
        combos = frame.group_by("monitored", "monitored_and_secured").len().sort("monitored", "monitored_and_secured")
        r.show(f"monitored flags {label}", combos=[(mask(a), mask(b), n) for a, b, n in combos.rows()])
    r.show("contingencies", n_names=dc["contingency_name"].n_unique(), rows=dc.height, type_patterns=patterns(dc["equipment_type"].to_list(), 5),
         operation_patterns=patterns(dc["contingency_operation"].to_list()), name_patterns=patterns(dc["contingency_name"].to_list()))
    gen_ids = {(b, norm(i) or "") for b, i in zip(D["psse_generator"]["i"].to_list(), D["psse_generator"]["id"].to_list())}
    load_ids = {(b, norm(i) or "") for b, i in zip(D["psse_load"]["i"].to_list(), D["psse_load"]["id"].to_list())}
    for (equipment_type,), sub in dc.group_by("equipment_type"):
        k = keys(sub, "psse_from_bus_number", "psse_to_bus_number", "psse_ckt_id")
        ids = {(b, norm(i) or "") for b, i in zip(sub["psse_gen_or_load_or_sp_bus_number"].to_list(), sub["psse_gen_or_load_id"].to_list()) if b is not None}
        r.show(f"  rows type={mask(equipment_type)}", rows=sub.height, branch_keys=len(k), in_lines_or_xf=len(either(k) & (ln_keys | xf_keys)),
             bus_ids=len(ids), in_raw_generators=len(ids & gen_ids), in_raw_loads=len(ids & load_ids), bus_in_raw=len({b for b, _ in ids} & bus_numbers))
    r.show("settlement_points", rows=sp.height, type_patterns=patterns(sp["settlement_point_type"].to_list(), 6),
         bus_null=sp["psse_bus_number"].null_count(), bus_in_raw=len(set(sp["psse_bus_number"].drop_nulls().to_list()) & bus_numbers))
    r.show("hubs", rows=hb.height, hubs=hb["hub_name"].n_unique(), bus_in_raw=len(set(hb["psse_bus_number"].drop_nulls().to_list()) & bus_numbers))
    r.show("generators", rows=gn.height, gen_bus_id_in_raw=len({(b, norm(i) or "") for b, i in zip(gn["psse_bus_number"].to_list(), gn["psse_gen_id"].to_list())} & gen_ids),
         resource_node_bus_null=gn["resource_node_psse_bus_number"].null_count(),
         resource_node_sp_in_sp_file=len(names(gn, "resource_node_settlement_point_name") & names(sp, "settlement_point_name")))
    r.show("loads", rows=ld.height, ldf_mw_sum=round(float(ld["raw_mw_ldf"].sum()), 1), load_zones=ld["load_zone_name"].n_unique())
    spc = D["dam_settlement_point_contingencies"]
    r.show("sp_contingencies", rows=spc.height, names_not_in_ctg=len(names(spc, "contingency_name") - names(dc, "contingency_name")))


def cross(C: dict[str, pl.DataFrame], D: dict[str, pl.DataFrame], r: Report) -> None:
    r.section("CRR vs DAM")
    c_bus, d_bus = bus_identity(C), bus_identity(D)
    shared = set(c_bus) & set(d_bus)
    r.show("bus numbers", crr=len(c_bus), dam=len(d_bus), shared=len(shared),
         shared_same_name=sum(1 for b in shared if c_bus[b][0] == d_bus[b][0]),
         shared_same_name_kv=sum(1 for b in shared if c_bus[b] == d_bus[b]))
    c_names, d_names = {v[0] for v in c_bus.values()}, {v[0] for v in d_bus.values()}
    r.show("bus names", crr=len(c_names), dam=len(d_names), shared=len(c_names & d_names))
    ln, dx = D["dam_lines"], D["dam_transformers"]
    dam_names = names(ln, "branch_name", norm2)
    ops = names(C["crr_mapping_lines"], "operations_name", norm2)
    exact = ops & dam_names
    rest = ops - dam_names
    r.show("workbook Operations_Name vs DAM Branch Name", ops=len(ops), exact=len(names(C["crr_mapping_lines"], "operations_name") & names(ln, "branch_name")),
         letters_digits_only=len(exact), prefix_of_a_dam_name=sum(1 for o in rest if any(d.startswith(o) for d in dam_names)),
         unmatched=len(rest), dam_names_embedding_a_station=sum(
             1 for n, f, t in zip(ln["branch_name"].to_list(), ln["from_station_name_psse_bus_name"].to_list(), ln["to_station_name_psse_bus_name"].to_list())
             if norm2(f) in norm2(n) or norm2(t) in norm2(n)))
    auto_ops = names(C["crr_mapping_autos"], "operations_name")
    r.show("workbook Autos Operations_Name vs DAM transformer Branch Name", ops=len(auto_ops), exact=len(auto_ops & names(dx, "branch_name")))
    r.show("branch keys (from, to, ckt)", crr=len(keys(C["psse_branch"], "i", "j", "ckt")), dam=len(keys(D["psse_branch"], "i", "j", "ckt")),
         shared=len(either(keys(C["psse_branch"], "i", "j", "ckt")) & keys(D["psse_branch"], "i", "j", "ckt")))
    c_ctg, d_ctg = names(C["crr_contingencies"], "contingency"), names(D["dam_contingencies"], "contingency_name")
    r.show("contingency names", crr=len(c_ctg), dam=len(d_ctg), shared=len(c_ctg & d_ctg))
    ss, sp = C["crr_sources_and_sinks"], D["dam_settlement_points"]
    r.show("settlement points", crr_source_sink_names=len(names(ss, "name")), dam_sps=len(names(sp, "settlement_point_name")),
         crr_names_in_dam=len(names(ss, "name") & names(sp, "settlement_point_name")))
    r.show("monitored", crr_peakwd_devices=C["crr_monitored_lines_and_transformers"].filter(pl.col("time_of_use").str.to_uppercase() == "PEAKWD").height,
         dam_lines=ln.height, dam_transformers=dx.height)
    r.show("gtc", crr_gtcs=C["crr_non_thermal_constraints"]["name"].n_unique(), dam_gtc_tables=[t for t in D if "constraint" in t])


def _compare_numbers(a: dict, b: dict) -> dict:
    """Two models' bus numbers: shared, same (name, kV), and whether uniquely named buses keep their number."""
    shared = set(a) & set(b)
    count_a, count_b = collections.Counter(a.values()), collections.Counter(b.values())
    unique_a = {v: k for k, v in a.items() if count_a[v] == 1}
    unique_b = {v: k for k, v in b.items() if count_b[v] == 1}
    both = set(unique_a) & set(unique_b)
    return {"buses_a": len(a), "buses_b": len(b), "shared_numbers": len(shared), "same_name_kv": sum(1 for k in shared if a[k] == b[k]),
            "uniquely_named_in_both": len(both), "keeping_their_number": sum(1 for v in both if unique_a[v] == unique_b[v])}


def stability(session, dam_id: str, crr_id: str, D, C, r: Report) -> None:
    r.section("Stability of identity")
    snaps = session.core("snapshot").collect()
    dam, crr = session.snapshot(dam_id), session.snapshot(crr_id)
    hour = dam["hour"]

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

    names = ("psse_bus", "dam_generators", "dam_loads", "dam_settlement_points", "dam_lines")
    next_hour = tables(session, latest(snaps, operating_date=dam["operating_date"], hour=hour + 1 if hour < 24 else hour - 1), names)
    r.show(f"DAM bus numbers, hour {hour} vs next hour", **_compare_numbers(bus_identity(D), bus_identity(next_hour)))
    here_eq, next_eq = equipment(D), equipment(next_hour)
    for label, x, y in ((k, here_eq[k], next_eq[k]) for k in ("generator", "load", "settlement_point", "branch")):
        common = set(x) & set(y)
        r.show(f"  {label} name -> (substation, kV) across hours", shared=len(common), same=sum(1 for k in common if x[k] == y[k]))
    oldest_day = snaps.filter(pl.col("model_kind") == "dam")["operating_date"].min()
    if oldest_day != dam["operating_date"]:
        other = tables(session, latest(snaps, operating_date=oldest_day, hour=hour), names)
        r.show("DAM bus numbers vs the oldest archived day", **_compare_numbers(bus_identity(D), bus_identity(other)))
        other_eq = equipment(other)
        for label, x, y in ((k, here_eq[k], other_eq[k]) for k in ("generator", "load", "settlement_point", "branch")):
            common = set(x) & set(y)
            r.show(f"  {label} name -> (substation, kV) across days", shared=len(common), same=sum(1 for k in common if x[k] == y[k]))
    months = snaps.filter((pl.col("model_kind") == "crr_monthly") & (pl.col("month") != crr["month"]))["month"]
    if months.len():
        other_month = months.max()
        other = tables(session, latest(snaps, model_kind="crr_monthly", month=other_month), ("psse_bus",))
        r.show(f"CRR bus numbers, {crr['month']:%Y-%m} vs {other_month:%Y-%m}", **_compare_numbers(bus_identity(C), bus_identity(other)))


def node_identity(session, dam_id: str, crr_id: str, D, C, r: Report) -> None:
    """How stable the equipment-based bus keys are (core/node.py)."""
    r.section("Bus identity (equipment-based keys)")
    snaps = session.core("snapshot").collect()
    dam, crr_snap = session.snapshot(dam_id), session.snapshot(crr_id)
    hour = dam["hour"]
    names = ("psse_bus", "dam_lines", "dam_transformers", "dam_generators", "dam_loads", "dam_settlement_points")

    def dam_keys(tables) -> pl.DataFrame:
        return node.dam_nodes(tables["psse_bus"], tables["dam_lines"], tables["dam_transformers"], tables["dam_generators"],
                                  tables["dam_loads"], tables["dam_settlement_points"])

    def compare(label: str, a: pl.DataFrame, b: pl.DataFrame) -> None:
        ka, kb = set(a["bus_key"]), set(b["bus_key"])
        r.show(label, nodes_a=a.height, nodes_b=b.height, keys_shared=len(ka & kb), only_a=len(ka - kb), only_b=len(kb - ka),
             ambiguous_a=int(a["is_ambiguous"].sum()), no_attachments_a=int((a["n_attachments"] == 0).sum()))

    here = dam_keys(D)
    compare(f"DAM hour {hour} vs next hour", here, dam_keys(tables(session, latest(snaps, operating_date=dam["operating_date"],
                                                                                   hour=hour + 1 if hour < 24 else hour - 1), names)))
    oldest_day = snaps.filter(pl.col("model_kind") == "dam")["operating_date"].min()
    if oldest_day != dam["operating_date"]:
        compare("DAM vs the oldest archived day", here, dam_keys(tables(session, latest(snaps, operating_date=oldest_day, hour=hour), names)))
    r.show("DAM attachments per node", quantiles=[int(here["n_attachments"].quantile(q)) for q in (0, 0.25, 0.5, 0.75, 1)],
         nodes_with_settlement_point=int(here["attachments"].str.contains("S:").sum()))

    crr = node.crr_nodes(C["psse_bus"], C["psse_branch"], C["psse_transformer"], C["crr_mapping_autos"], C["crr_sources_and_sinks"])
    groups = crr.filter(pl.col("is_tie_member")).group_by("bus_group").len()
    ties = C["psse_branch"].filter(pl.col("x").abs() <= node.TIE_REACTANCE)
    kv = dict(zip(C["psse_bus"]["i"].to_list(), C["psse_bus"]["basekv"].to_list()))
    r.show("CRR bus ties (|x| <= TIE_REACTANCE)", n=ties.height, same_kv_both_ends=sum(1 for i, j in zip(ties["i"], ties["j"]) if kv.get(i) == kv.get(j)),
         r_zero=int((ties["r"] == 0).sum()), in_service=int((ties["st"] == 1).sum()), ratea_9999_or_more=int((ties["ratea"] >= 9999).sum()),
         x_patterns=patterns(ties["x"].to_list(), 3))
    r.show("CRR contraction", buses=crr.height, nodes=crr["bus_group"].n_unique(), tie_groups=groups.height,
         buses_in_tie_groups=int(groups["len"].sum()), largest_group=int(groups["len"].max()) if groups.height else 0,
         ambiguous=int(crr["is_ambiguous"].sum()), no_attachments=int((crr["n_attachments"] == 0).sum()))
    months = snaps.filter((pl.col("model_kind") == "crr_monthly") & (pl.col("month") != crr_snap["month"]))["month"]
    if months.len():
        other = tables(session, latest(snaps, model_kind="crr_monthly", month=months.max()), ("psse_bus", "psse_branch", "psse_transformer",
                                                                                              "crr_mapping_autos", "crr_sources_and_sinks"))
        other_nodes = node.crr_nodes(other["psse_bus"], other["psse_branch"], other["psse_transformer"], other["crr_mapping_autos"],
                                     other["crr_sources_and_sinks"])
        ka, kb = set(crr["bus_key"]), set(other_nodes["bus_key"])
        r.show("CRR nodes vs another month", nodes_a=crr["bus_group"].n_unique(), nodes_b=other_nodes["bus_group"].n_unique(), keys_shared=len(ka & kb))


def matching(session, crr_id: str, dam_id: str, r: Report) -> None:
    """How the session's matchers do on the built core tables."""
    r.section("Matching (core.match_*)")
    for name, frame, left, right in (("branches", session.match_branches(crr_id, dam_id), "crr_branch_id", "dam_branch_id"),
                                     ("nodes", session.match_buses(crr_id, dam_id), "crr_bus_key", "dam_bus_key"),
                                     ("contingencies", session.match_contingencies(crr_id, dam_id), "crr_contingency_id", "dam_contingency_id")):
        counts = frame.group_by("match_method").agg(pl.len().alias("n"), pl.col(left).is_not_null().sum().alias("crr"),
                                                    pl.col(right).is_not_null().sum().alias("dam")).sort("match_method")
        r.show(name, **{f"{m}": f"{n} (crr {c}, dam {d})" for m, n, c, d in counts.rows()})
    ctg = session.match_contingencies(crr_id, dam_id).filter(pl.col("match_method") == "name")
    r.show("name-matched contingencies", n=ctg.height, identical_branch_sets=int(((ctg["n_shared_branches"] == ctg["n_crr_branches"]) & (ctg["n_crr_branches"] == ctg["n_dam_branches"])).sum()),
         no_shared_branch=int((ctg["n_shared_branches"] == 0).sum()), with_dam_load_gen_sp_rows=int((ctg["n_dam_other_rows"] > 0).sum()), split_bus=int(ctg["has_split_bus"].sum()))
    # Why a name-matched contingency's CRR branch set differs from the DAM one.
    branches = session.match_branches(crr_id, dam_id).filter(pl.col("crr_branch_id").is_not_null()).select("crr_branch_id", "dam_branch_id")
    crr_b = session.core_rows("branch", crr_id).select("branch_id", "kind", "is_tie")
    crr_o = session.core_rows("contingency_outage", crr_id)
    dam_o = (session.core_rows("contingency_outage", dam_id).filter(pl.col("element_kind") == "branch")
             .select(pl.col("contingency_id").alias("dam_contingency_id"), pl.col("branch_id").alias("dam_branch_id"), pl.lit(True).alias("in_dam")).unique())
    rows = (crr_o.join(ctg.select("crr_contingency_id", "dam_contingency_id"), left_on="contingency_id", right_on="crr_contingency_id")
            .join(branches, left_on="branch_id", right_on="crr_branch_id", how="left")
            .join(crr_b, on="branch_id", how="left")
            .join(dam_o, on=["dam_contingency_id", "dam_branch_id"], how="left")
            .with_columns(pl.when(pl.col("branch_id").is_null()).then(pl.lit("crr_device_unresolved"))
                          .when(pl.col("is_tie")).then(pl.lit("crr_bus_tie_no_dam_equivalent"))
                          .when(pl.col("dam_branch_id").is_null() & (pl.col("kind") == "transformer")).then(pl.lit("transformer_not_matched"))
                          .when(pl.col("dam_branch_id").is_null()).then(pl.lit("line_not_matched"))
                          .when(pl.col("in_dam")).then(pl.lit("shared"))
                          .otherwise(pl.lit("matched_branch_but_dam_contingency_omits_it")).alias("why")))
    r.show("name-matched contingency rows by outcome", **{why: n for why, n in rows.group_by("why").len().sort("len", descending=True).rows()})
    nodes = session.match_buses(crr_id, dam_id).filter(pl.col("match_method") != "unmatched")
    kv = (nodes.join(session.core_rows("node", crr_id).select("bus_key", "kv").unique(subset=["bus_key"]), left_on="crr_bus_key", right_on="bus_key")
          .join(session.core_rows("node", dam_id).select("bus_key", pl.col("kv").alias("dam_kv")), left_on="dam_bus_key", right_on="bus_key"))
    r.show("matched nodes", n=nodes.height, same_kv=int((kv["kv"] == kv["dam_kv"]).sum()),
         same_kv_ignoring_tenths=int((kv["kv"].floor() == kv["dam_kv"].floor()).sum()))
    # Do matched branches describe the same element? core.diff_branch puts both sides next to each other.
    pairs = session.diff_branches(crr_id, dam_id)
    raw_ok = pairs.select(((pl.col("crr_x_pu") - pl.col("dam_x_pu")).abs() / pl.max_horizontal(pl.col("crr_x_pu").abs(), pl.col("dam_x_pu").abs(), 1e-9)
                           <= match.REACTANCE_TOLERANCE).alias("ok"))["ok"]
    ties = crr_b.select("branch_id", "is_tie").rename({"branch_id": "crr_branch_id"})
    pairs = pairs.join(ties, on="crr_branch_id", how="left")
    r.show("matched branches", n=pairs.height, same_reactance=int(pairs["same_reactance"].sum()), same_reactance_without_floor=int(raw_ok.sum()),
         same_kind=int(pairs["same_kind"].sum()), same_kv_ignoring_tenths=int(pairs["same_kv"].sum()),
         in_service_both=int((pairs["crr_in_service"] & pairs["dam_in_service"]).sum()), enforced_both=int((pairs["crr_enforced"] & pairs["dam_enforced"]).sum()),
         enforced_crr_only=int((pairs["crr_enforced"] & ~pairs["dam_enforced"]).sum()), enforced_dam_only=int((~pairs["crr_enforced"] & pairs["dam_enforced"]).sum()),
         crr_ties_matched=int(pairs["is_tie"].fill_null(False).sum()),
         crr_ties_matched_to_dam_floor=int((pairs["is_tie"].fill_null(False) & (pairs["dam_x_pu"].abs() == match.DAM_REACTANCE_FLOOR)).sum()),
         crr_x_below_floor=int((pairs["crr_x_pu"].abs() < match.DAM_REACTANCE_FLOOR).sum()))
    r.show("  reactance agreement by method", **{m: f"{ok} of {n}" for m, ok,
                                                 n in pairs.group_by("match_method").agg(pl.col("same_reactance").sum(), pl.len()).sort("match_method").rows()})
    both = pairs.filter(pl.col("crr_enforced") & pl.col("dam_enforced") & pl.col("crr_base_mw").is_not_null() & (pl.col("dam_base_mw") > 0))
    ratio = both["crr_base_mw"] / both["dam_base_mw"]
    r.show("  enforced on both sides: CRR base over DAM rate A", n=both.height, quantiles=[round(ratio.quantile(q), 3) for q in (0.05, 0.25, 0.5, 0.75, 0.95)],
         within_1pct=int(((ratio - 1).abs() <= 0.01).sum()))
    # Settlement points: where each model puts them, and whether the two agree through the node match.
    for sid, label in ((crr_id, "CRR"), (dam_id, "DAM")):
        pts = session.core_rows("settlement_point", sid)
        r.show(f"settlement points {label}", n=pts.height, **{f"{k}": n for k, n in pts.group_by("kind").len().sort("kind").rows()},
             without_node=int((pts["n_buses"] == 0).sum()), with_unresolved_rows=int((pts["n_unresolved"] > 0).sum()),
             multi_node=int((pts["n_buses"] > 1).sum()), max_nodes=int(pts["n_buses"].max()))
    spd = session.diff_settlement_points(crr_id, dam_id)
    r.show("settlement points in both", n=spd.height, same_kind=int(spd["same_kind"].sum()), same_buses=int(spd["same_buses"].sum()),
         with_untranslatable_crr_nodes=int((spd["n_crr_buses_unmatched"] > 0).sum()),
         **{f"same_buses_{k}": f"{ok} of {n}" for k, ok, n in spd.group_by("dam_kind").agg(pl.col("same_buses").sum(), pl.len()).sort("dam_kind").rows()})
    hubs = spd.filter(pl.col("dam_kind").is_in(["hub", "load_zone"]))
    r.show("  hubs and zones: shared weight", crr_side_quantiles=[round(hubs["shared_weight_crr"].quantile(q), 3) for q in (0, 0.5, 1)],
         dam_side_quantiles=[round(hubs["shared_weight_dam"].quantile(q), 3) for q in (0, 0.5, 1)])


def coverage_section(session, crr_id: str, dam_id: str, r: Report) -> None:
    """What each model carries (core.coverage), and the matched and unmatched counts of the pair."""
    r.section("coverage")
    for label, sid in (("crr", crr_id), ("dam", dam_id)):
        rows = {t: session.core_rows(t, sid) for t in ("node", "branch", "branch_rating", "load", "settlement_point",
                                                       "settlement_point_bus", "contingency_outage")}
        counts = coverage.snapshot_coverage(rows["node"], rows["branch"], rows["branch_rating"], rows["load"], rows["settlement_point"],
                                            rows["settlement_point_bus"], rows["contingency_outage"], session.network(sid).branches)
        r.show(label, snapshot_id=sid, **counts)
    r.show("pair", **coverage.pair_coverage(session.match_branches(crr_id, dam_id), session.match_buses(crr_id, dam_id),
                                            session.match_contingencies(crr_id, dam_id)))


def run(session, crr_id: str | None = None, dam_id: str | None = None, quiet: bool = False) -> dict:
    """Every section for one monthly CRR model and one DAM hour (the newest of each by default); writes the report."""
    snaps = session.core("snapshot").collect()
    crr_id = crr_id or snaps.filter(pl.col("model_kind") == "crr_monthly").sort("month", "revision")["snapshot_id"][-1]
    dam_id = dam_id or snaps.filter((pl.col("model_kind") == "dam") & (pl.col("hour") == 12)).sort("operating_date", "revision")["snapshot_id"][-1]
    crr, dam = session.snapshot(crr_id), session.snapshot(dam_id)
    r = Report(quiet)
    C, D = tables(session, crr_id), tables(session, dam_id)
    crr_internal(C, r)
    dam_internal(D, r)
    cross(C, D, r)
    stability(session, dam_id, crr_id, D, C, r)
    node_identity(session, dam_id, crr_id, D, C, r)
    matching(session, crr_id, dam_id, r)
    coverage_section(session, crr_id, dam_id, r)
    result = {"crr": crr_id, "dam": dam_id, "crr_month": f"{crr['month']:%Y-%m}", "dam_day": str(dam["operating_date"]),
              "hour": dam["hour"], "sections": r.sections}
    write_report(session, "identity", f"{crr['month']:%Y-%m}_{dam['operating_date']}_he{dam['hour']:02d}", result)
    return result
