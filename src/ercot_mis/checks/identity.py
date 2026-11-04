"""How the keys of a CRR model and a DAM hour line up: the identity report.

For one monthly CRR model and one DAM hour, sections of counts, never names:

- Stability: PSS/E numbers across DAM hours and days and across CRR months, and whether
  equipment names give a stable (substation, kV) identity (TOP-08).
- Bus identity: how stable the equipment-based bus keys are, and the CRR ties (TOP-10).
- Matching and coverage: how ``core.match_*`` and ``core.coverage`` do on the pair (MAT-01 to MAT-04).

The raw-file counts that settled the key design in 2026-09 are recorded in
``docs/datasets/identity-and-matching.md``; core now carries those facts as columns.

The dataset notes say what these measure; the numbers live in ``data/reports/identity/``.
"""

from __future__ import annotations

import collections
import re

import polars as pl

from ..core import bus, coverage, match
from ..core.snapshot import is_dam
from . import write_report

# ------------------------------------------------------------------ helpers

def mask(value) -> str:
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", str(value)))


def norm(value) -> str | None:
    """Upper case, collapsed whitespace."""
    return re.sub(r"\s+", " ", str(value).strip().upper()) if value is not None else None


def patterns(values, k: int = 3) -> list[tuple[str, int]]:
    return collections.Counter(mask(v) for v in values if v is not None).most_common(k)


class Report:
    """Sections of counts, printed as they are recorded."""

    def __init__(self):
        self.sections: dict[str, dict] = {}
        self.current = ""

    def section(self, title: str) -> None:
        self.current = title
        print(f"\n== {title}")

    def show(self, title: str, **counts) -> None:
        self.sections.setdefault(self.current, {})[title] = {k: (list(v) if isinstance(v, (set, tuple)) else v) for k, v in counts.items()}
        print(f"  {title}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


# ------------------------------------------------------------------ loading

CRR_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "crr_mapping_autos", "crr_mapping_lines", "crr_contingencies",
              "crr_monitored_lines_and_transformers", "crr_non_thermal_constraints", "crr_sources_and_sinks")
DAM_TABLES = ("psse_bus", "psse_branch", "psse_transformer", "psse_generator", "psse_load", "dam_lines", "dam_transformers", "dam_contingencies",
              "dam_settlement_points", "dam_hub_buses", "dam_generators", "dam_loads", "dam_settlement_point_contingencies")


def tables(session, snapshot_id: str, names=None) -> dict[str, pl.DataFrame]:
    """One model's raw tables."""
    names = names or (DAM_TABLES if is_dam(snapshot_id) else CRR_TABLES)
    return {t: session.raw_rows(t, snapshot_id) for t in names}


def latest(snaps: pl.DataFrame, **where) -> str | None:
    """The highest revision of the snapshot matching ``where`` (TOP-11), or None."""
    rows = snaps.filter(*[pl.col(k) == v for k, v in where.items()]).sort("revision")
    return rows["snapshot_id"][-1] if rows.height else None


def bus_identity(tables) -> dict[int, tuple[str | None, float]]:
    bus = tables["psse_bus"]
    return dict(zip(bus["i"].to_list(), zip([norm(x) for x in bus["name"].to_list()], bus["basekv"].to_list())))


# ------------------------------------------------------------------ sections


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


def bus_keys(session, dam_id: str, crr_id: str, D, C, r: Report) -> None:
    """How stable the equipment-based bus keys are (core/bus.py)."""
    r.section("Bus identity (equipment-based keys)")
    snaps = session.core("snapshot").collect()
    dam, crr_snap = session.snapshot(dam_id), session.snapshot(crr_id)
    hour = dam["hour"]
    names = ("psse_bus", "dam_lines", "dam_transformers", "dam_generators", "dam_loads", "dam_settlement_points")

    def dam_keys(tables) -> pl.DataFrame:
        return bus.dam_buses(tables["psse_bus"], tables["dam_lines"], tables["dam_transformers"], tables["dam_generators"],
                             tables["dam_loads"], tables["dam_settlement_points"])

    def compare(label: str, a: pl.DataFrame, b: pl.DataFrame) -> None:
        ka, kb = set(a["bus_key"]), set(b["bus_key"])
        r.show(label, buses_a=a.height, buses_b=b.height, keys_shared=len(ka & kb), only_a=len(ka - kb), only_b=len(kb - ka),
             ambiguous_a=int(a["is_ambiguous"].sum()), no_attachments_a=int((a["n_attachments"] == 0).sum()))

    here = dam_keys(D)
    compare(f"DAM hour {hour} vs next hour", here, dam_keys(tables(session, latest(snaps, operating_date=dam["operating_date"],
                                                                                   hour=hour + 1 if hour < 24 else hour - 1), names)))
    oldest_day = snaps.filter(pl.col("model_kind") == "dam")["operating_date"].min()
    if oldest_day != dam["operating_date"]:
        compare("DAM vs the oldest archived day", here, dam_keys(tables(session, latest(snaps, operating_date=oldest_day, hour=hour), names)))
    r.show("DAM attachments per bus", quantiles=[int(here["n_attachments"].quantile(q)) for q in (0, 0.25, 0.5, 0.75, 1)],
         buses_with_settlement_point=int(here["attachments"].str.contains("S:").sum()))

    crr_nodes, crr = bus.crr_buses(C["psse_bus"], C["psse_branch"], C["psse_transformer"], C["crr_mapping_autos"], C["crr_sources_and_sinks"])
    groups = crr.filter(pl.col("n_nodes") > 1)
    ties = C["psse_branch"].filter(pl.col("x").abs() <= bus.TIE_REACTANCE)
    kv = dict(zip(C["psse_bus"]["i"].to_list(), C["psse_bus"]["basekv"].to_list()))
    r.show("CRR bus ties (|x| <= TIE_REACTANCE)", n=ties.height, same_kv_both_ends=sum(1 for i, j in zip(ties["i"], ties["j"]) if kv.get(i) == kv.get(j)),
         r_zero=int((ties["r"] == 0).sum()), in_service=int((ties["st"] == 1).sum()), ratea_9999_or_more=int((ties["ratea"] >= 9999).sum()),
         x_patterns=patterns(ties["x"].to_list(), 3))
    r.show("CRR contraction", nodes=crr_nodes.height, buses=crr.height, tie_groups=groups.height,
         nodes_in_tie_groups=int(groups["n_nodes"].sum()), largest_group=int(groups["n_nodes"].max()) if groups.height else 0,
         ambiguous=int(crr["is_ambiguous"].sum()), no_attachments=int((crr["n_attachments"] == 0).sum()))
    months = snaps.filter((pl.col("model_kind") == "crr_monthly") & (pl.col("month") != crr_snap["month"]))["month"]
    if months.len():
        other = tables(session, latest(snaps, model_kind="crr_monthly", month=months.max()), ("psse_bus", "psse_branch", "psse_transformer",
                                                                                              "crr_mapping_autos", "crr_sources_and_sinks"))
        _, other_buses = bus.crr_buses(other["psse_bus"], other["psse_branch"], other["psse_transformer"], other["crr_mapping_autos"],
                                       other["crr_sources_and_sinks"])
        ka, kb = set(crr["bus_key"]), set(other_buses["bus_key"])
        r.show("CRR buses vs another month", buses_a=crr.height, buses_b=other_buses.height, keys_shared=len(ka & kb))


def matching(session, crr_id: str, dam_id: str, r: Report) -> None:
    """How the session's matchers do on the built core tables."""
    r.section("Matching (core.match_*)")
    for name, frame, left, right in (("branches", session.match_branches(crr_id, dam_id), "crr_branch_id", "dam_branch_id"),
                                     ("buses", session.match_buses(crr_id, dam_id), "crr_bus_key", "dam_bus_key"),
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
    buses = session.match_buses(crr_id, dam_id).filter(pl.col("match_method") != "unmatched")
    kv = (buses.join(session.core_rows("bus", crr_id).select("bus_key", "kv"), left_on="crr_bus_key", right_on="bus_key")
          .join(session.core_rows("bus", dam_id).select("bus_key", pl.col("kv").alias("dam_kv")), left_on="dam_bus_key", right_on="bus_key"))
    r.show("matched buses", n=buses.height, same_kv=int((kv["kv"] == kv["dam_kv"]).sum()),
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
    # Settlement points: where each model puts them, and whether the two agree through the bus match.
    for sid, label in ((crr_id, "CRR"), (dam_id, "DAM")):
        pts = session.core_rows("settlement_point", sid)
        r.show(f"settlement points {label}", n=pts.height, **{f"{k}": n for k, n in pts.group_by("kind").len().sort("kind").rows()},
             without_node=int((pts["n_buses"] == 0).sum()), with_unresolved_rows=int((pts["n_unresolved"] > 0).sum()),
             multi_node=int((pts["n_buses"] > 1).sum()), max_nodes=int(pts["n_buses"].max()))
    spd = session.diff_settlement_points(crr_id, dam_id)
    r.show("settlement points in both", n=spd.height, same_kind=int(spd["same_kind"].sum()), same_buses=int(spd["same_buses"].sum()),
         with_untranslatable_crr_buses=int((spd["n_crr_buses_unmatched"] > 0).sum()),
         **{f"same_buses_{k}": f"{ok} of {n}" for k, ok, n in spd.group_by("dam_kind").agg(pl.col("same_buses").sum(), pl.len()).sort("dam_kind").rows()})
    hubs = spd.filter(pl.col("dam_kind").is_in(["hub", "load_zone"]))
    r.show("  hubs and zones: shared weight", crr_side_quantiles=[round(hubs["shared_weight_crr"].quantile(q), 3) for q in (0, 0.5, 1)],
         dam_side_quantiles=[round(hubs["shared_weight_dam"].quantile(q), 3) for q in (0, 0.5, 1)])


def coverage_section(session, crr_id: str, dam_id: str, r: Report) -> None:
    """What each model carries (core.coverage), and the matched and unmatched counts of the pair."""
    r.section("coverage")
    for label, sid in (("crr", crr_id), ("dam", dam_id)):
        rows = {t: session.core_rows(t, sid) for t in ("bus", "node", "branch", "branch_rating", "load", "settlement_point",
                                                       "settlement_point_bus", "contingency_outage")}
        counts = coverage.snapshot_coverage(rows["bus"], rows["node"], rows["branch"], rows["branch_rating"], rows["load"], rows["settlement_point"],
                                            rows["settlement_point_bus"], rows["contingency_outage"], session.network(sid).branches)
        r.show(label, snapshot_id=sid, **counts)
    r.show("pair", **coverage.pair_coverage(session.match_branches(crr_id, dam_id), session.match_buses(crr_id, dam_id),
                                            session.match_contingencies(crr_id, dam_id)))


def run(session, crr_id: str | None = None, dam_id: str | None = None) -> dict:
    """Every section for one monthly CRR model and one DAM hour (the newest of each by default); writes the report."""
    newest = session.newest()
    crr_id, dam_id = crr_id or newest[0], dam_id or newest[1]
    crr, dam = session.snapshot(crr_id), session.snapshot(dam_id)
    r = Report()
    C, D = tables(session, crr_id), tables(session, dam_id)
    stability(session, dam_id, crr_id, D, C, r)
    bus_keys(session, dam_id, crr_id, D, C, r)
    matching(session, crr_id, dam_id, r)
    coverage_section(session, crr_id, dam_id, r)
    result = {"crr": crr_id, "dam": dam_id, "crr_month": f"{crr['month']:%Y-%m}", "dam_day": str(dam["operating_date"]),
              "hour": dam["hour"], "sections": r.sections}
    write_report(session, "identity", f"{crr['month']:%Y-%m}_{dam['operating_date']}_he{dam['hour']:02d}", result)
    return result
