"""A measurement for each row of ``docs/assumptions.md`` that no other check measures.

Rows the other checks already measure name them in their ``check`` cell (``checks/prices.py``,
``checks/flows.py`` and so on). Every other row has a function here, listed under its id in
``MEASURES``. Each one reads one DAM hour and one monthly CRR model (the newest of each by
default) and returns counts that either support the row's assumption or show where it fails.
Counts only: never a name.
"""

from __future__ import annotations

import polars as pl

from ..raw.table import flag

from ..core.match import DAM_REACTANCE_FLOOR, name_key, placeholder_name
from . import write_report
from .binding import hour_rows

def run(session, dam: str | None = None, crr: str | None = None) -> dict[str, dict]:
    """Every measurement of ``MEASURES`` on one DAM hour and one CRR month (the newest by default); writes ``data/reports/register/<dam>__<crr>.json``."""
    default_crr, default_dam = session.newest()
    dam, crr = dam or default_dam, crr or default_crr
    done: dict = {}
    results = {}
    for row_id, measure in sorted(MEASURES.items()):
        if measure not in done:
            done[measure] = measure(session, dam, crr)
        results[row_id] = done[measure]
    write_report(session, "register", f"{dam}__{crr}", {"dam": dam, "crr": crr, "rows": results})
    return results


# ---------------------------------------------------------------------- topology


def key_stability(session, dam: str, crr: str) -> dict:
    """Bus keys survive the DAM's hourly renumbering and the CRR's monthly models; node numbers do not."""
    snaps = session.core("snapshot").collect()
    snap = snaps.filter(pl.col("snapshot_id") == dam).row(0, named=True)
    next_hour = snaps.filter((pl.col("operating_date") == snap["operating_date"]) & (pl.col("hour") == snap["hour"] + 1)).sort("revision")["snapshot_id"]
    months = snaps.filter(pl.col("model_kind") == "crr_monthly").sort("month", "revision").unique(subset=["month"], keep="last").sort("month")["snapshot_id"]
    out = {}
    for label, a, b in (("dam_next_hour", dam, next_hour[-1] if next_hour.len() else None),
                        ("crr_previous_month", months[-2] if months.len() > 1 else None, crr)):
        if a is None or b is None:
            continue
        x, y = session.core_rows("bus", a), session.core_rows("bus", b)
        numbers = (x.join(y, on=["bus_number", "bus_key"]).height if label == "dam_next_hour"
                   else session.core_rows("node", a).join(session.core_rows("node", b), on=["node_number", "bus_key"]).height)
        out[label] = {"bus_keys": x["bus_key"].n_unique(), "bus_keys_in_both": len(set(x["bus_key"]) & set(y["bus_key"])), "psse_numbers_in_both_with_same_key": numbers}
    return out


def outages_already_out(session, dam: str, crr: str) -> dict:
    """CRR: equipment the Outages file names is already out of service in the RAW, so the file adds nothing."""
    ops = session.raw_rows("crr_mapping_lines", crr).select(pl.col("crr_tag").alias("branch_id"), name_key(pl.col("operations_name")).alias("ops"))
    named = session.raw_rows("crr_outages", crr).select(name_key(pl.col("equipment_name")).alias("ops")).unique()
    hit = ops.join(named, on="ops").join(session.core_rows("branch", crr).select("branch_id", "is_in_service"), on="branch_id")
    return {"outage_rows": session.raw_rows("crr_outages", crr).height, "branches_named_by_an_outage": hit.height,
            "of_which_out_of_service_in_raw": int((~hit["is_in_service"]).sum())}


def dropped_by_reason(session, dam: str, crr: str) -> dict:
    """What the largest-component rule drops, by reason, against what it keeps."""
    return {label: {"buses_kept": net.n_buses, "branches_kept": net.n_branches,
                    "dropped_buses": dict(net.dropped_buses.group_by("reason").len().sort("reason").rows()),
                    "dropped_branches": dict(net.dropped_branches.group_by("reason").len().sort("reason").rows())}
            for label, net in (("dam", session.network(dam)), ("crr", session.network(crr)))}


def slack(session, dam: str, crr: str) -> dict:
    """The slack is the RAW's swing record: how many swing records survive, and which rule chose the slack."""
    out = {}
    for label, sid, table, number, kind in (("dam", dam, "bus", "bus_number", "bus_type"), ("crr", crr, "node", "node_number", "node_type")):
        net, swing = session.network(sid), session.core_rows(table, sid).filter(pl.col(kind) == 3)
        out[label] = {"swing_records": swing.height, "swing_records_in_network": int(net.buses[number].is_in(swing[number].implode()).sum()),
                      "slack_source": net.slack_source}
    return out


def ambiguous_keys(session, dam: str, crr: str) -> dict:
    """Bus keys made unique by an ordinal suffix, and how many reach the network."""
    out = {}
    for label, sid in (("dam", dam), ("crr", crr)):
        buses = session.core_rows("bus", sid).filter(pl.col("is_ambiguous"))
        keys = set(session.network(sid).buses["bus_key"])
        out[label] = {"ambiguous_buses": buses.height, "in_network": int(buses["bus_key"].is_in(list(keys)).sum())}
    return out


def open_ties(session, dam: str, crr: str) -> dict:
    """CRR ties out of service stay branches between separate buses, even when ties are contracted."""
    ties = session.core_rows("branch", crr).filter(pl.col("is_tie"))
    open_ties = ties.filter(~pl.col("is_in_service"))
    return {"ties": ties.height, "ties_out_of_service": open_ties.height,
            "of_which_ends_in_different_buses": int((open_ties["from_bus_key"] != open_ties["to_bus_key"]).sum())}


def crr_substations(session, dam: str, crr: str) -> dict:
    """The viewer's first source for a CRR node's substation is its matched DAM node: how many CRR buses have one."""
    buses = session.match_buses(crr, dam)
    return {"crr_buses": buses["crr_bus_key"].drop_nulls().n_unique(),
            "with_a_matched_dam_bus": buses.drop_nulls(["crr_bus_key", "dam_bus_key"])["crr_bus_key"].n_unique()}


def contingency_operations(session, dam: str, crr: str) -> dict:
    """Every operation and action the contingency files use: anything besides an outage or a split would be misread."""
    dam = session.raw_rows("dam_contingencies", dam)["contingency_operation"].str.to_uppercase().value_counts().sort("contingency_operation")
    crr = session.raw_rows("crr_contingencies", crr)["action"].str.to_uppercase().value_counts().sort("action")
    return {"dam_operations": dict(dam.rows()), "crr_actions": dict(crr.rows())}


# ---------------------------------------------------------------------- impedances and ratings


def reactances(session, dam: str, crr: str) -> dict:
    """Reactances below the DAM's floor, and negative ones (series capacitors), in both models."""
    out = {}
    for label, sid in (("dam", dam), ("crr", crr)):
        x = session.core_rows("branch", sid)["x_pu"]
        out[label] = {"branches": x.len(), "below_dam_floor": int((x.abs() < DAM_REACTANCE_FLOOR).sum()), "negative": int((x < 0).sum())}
    return out


def rate_b_against_crr_emergency(session, dam: str, crr: str) -> dict:
    """DAM post-contingency limit is rate B: on matched branches, DAM rate B against the CRR emergency rating."""
    pairs = session.match_branches(crr, dam).drop_nulls(["crr_branch_id", "dam_branch_id"]).select("crr_branch_id", "dam_branch_id")
    crr = session.core_rows("branch_rating", crr).filter((pl.col("rating_source") == "crr_monitored") & (pl.col("time_of_use") == "PeakWD"))
    dam = session.core_rows("branch_rating", dam).filter(pl.col("rating_source") == "psse_raw")
    both = (pairs.join(crr.select(pl.col("branch_id").alias("crr_branch_id"), pl.col("emergency_mw").alias("crr_emergency")), on="crr_branch_id")
            .join(dam.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("emergency_mw").alias("dam_rate_b"),
                             pl.col("base_mw").alias("dam_rate_a")), on="dam_branch_id")
            .filter(pl.col("crr_emergency") > 0))
    return {"matched_with_both_ratings": both.height,
            "rate_b_within_1_mw": int(((both["dam_rate_b"] - both["crr_emergency"]).abs() <= 1).sum()),
            "rate_a_within_1_mw": int(((both["dam_rate_a"] - both["crr_emergency"]).abs() <= 1).sum())}


def crr_blocks(session, dam: str, crr: str) -> dict:
    """The monitored CSV carries its own ratings per time-of-use block; how many devices differ between blocks."""
    csv = session.raw_rows("crr_monitored_lines_and_transformers", crr)
    spread = csv.group_by("device_name").agg(pl.col("base_case_rating").n_unique().alias("n"))
    return {"blocks": csv["time_of_use"].n_unique(), "devices": spread.height, "devices_whose_rating_differs_by_block": int((spread["n"] > 1).sum())}


def enforced_set(session, dam: str, crr: str) -> dict:
    """Every limited branch under every contingency, against the rows that actually bound in the DAM hour."""
    net = session.network(dam)
    snap = session.snapshot(dam)
    bound = hour_rows(session, "dam_shadow_prices", snap["interval_start_utc"])
    return {"limited_branches": int(net.branches["is_limited"].sum()), "contingencies": net.contingencies.height, "binding_rows_published": bound.height}


def empty_contingencies(session, dam: str, crr: str) -> dict:
    """Contingencies dropped as empty: those with only non-branch rows, and those whose branches the model lacks."""
    out = {}
    for label, sid in (("dam", dam), ("crr", crr)):
        d = session.network(sid).dropped_contingencies
        out[label] = {"dropped": d.height, "only_other_rows": int(((d["n_dropped"] + d["n_unresolved"]) == 0).sum()),
                      "branches_absent_or_out": int(((d["n_dropped"] + d["n_unresolved"]) > 0).sum())}
    return out


# ---------------------------------------------------------------------- names, orientation, constraints


def crr_line_names(session, dam: str, crr: str) -> dict:
    """CRR line names read from RAW ``i`` to ``j``: the from node's name comes first in the name."""
    nodes = session.core_rows("node", crr).select("node_number", name_key(pl.col("raw_name")).alias("n"))
    lines = (session.core_rows("branch", crr).filter(pl.col("kind") == "line").select(name_key(pl.col("branch_id")).alias("b"), "from_node", "to_node")
             .join(nodes.rename({"node_number": "from_node", "n": "f"}), on="from_node").join(nodes.rename({"node_number": "to_node", "n": "t"}), on="to_node"))
    pos = lines.with_columns(pl.col("b").str.find(pl.col("f"), literal=True).alias("pf"), pl.col("b").str.find(pl.col("t"), literal=True).alias("pt"))
    both = pos.filter(pl.col("pf").is_not_null() & pl.col("pt").is_not_null() & (pl.col("f") != pl.col("t")))
    return {"lines": lines.height, "both_names_found": both.height, "from_name_first": int((both["pf"] < both["pt"]).sum())}


def crr_transformer_names(session, dam: str, crr: str) -> dict:
    """CRR transformer names follow the Autos sheet, reversed against the RAW for a minority; duplicate sheet rows are counted."""
    xf = session.core_rows("branch", crr).filter(pl.col("kind") == "transformer")
    autos = session.raw_rows("crr_mapping_autos", crr).select("from_number", "to_number", "id")
    duplicates = autos.group_by("from_number", "to_number", "id").len().filter(pl.col("len") > 1).height
    return {"transformers": xf.height, "name_reversed": int(xf["is_name_reversed"].fill_null(False).sum()),
            "autos_rows": autos.height, "autos_keys_listed_twice": duplicates}


def gtc_signs(session, dam: str, crr: str) -> dict:
    """GTC members by the CSV's flow direction and the sign of their factor."""
    members = session.core_rows("gtc_member", crr)
    return {"members": members.height,
            "by_direction_and_sign": {f"{d}|{'+' if s else '-'}": n for d, s, n in
                                      members.group_by(pl.col("flow_direction").str.to_uppercase(), pl.col("factor") >= 0).len().sort("flow_direction", "factor").rows()}}


def gtl_postings(session, dam: str, crr: str) -> dict:
    """GTL workbooks: delivery days posted more than once, and postings with no posting time (TOP-11 decides both)."""
    rows = session.raw("gtl_hourly").select("delivery_date", "doc_id").unique().collect()
    docs = session.catalog.documents("NP3-766-M", rows["doc_id"].unique())
    return {"days": rows["delivery_date"].n_unique(), "days_posted_more_than_once": int((rows.group_by("delivery_date").len()["len"] > 1).sum()),
            "postings_without_a_time": int(docs["posted_at"].is_null().sum())}


def contingency_matching(session, dam: str, crr: str) -> dict:
    """Contingency matching is one-to-one: DAM contingencies claimed twice, and CRR ones left unmatched."""
    rows = session.match_contingencies(crr, dam)
    matched = rows.filter(pl.col("match_method") != "unmatched").drop_nulls("dam_contingency_id")
    return {"matched": matched.height, "dam_matched_more_than_once": int((matched.group_by("dam_contingency_id").len()["len"] > 1).sum()),
            "crr_unmatched": int(((rows["match_method"] == "unmatched") & rows["crr_contingency_id"].is_not_null()).sum())}


def dam_branch_names(session, dam: str, crr: str) -> dict:
    """DAM line and transformer names join the RAW on (from, to, circuit) as written: rows that only join the other way round."""
    raw = pl.concat([session.raw_rows("psse_branch", dam).select("i", "j", pl.col("ckt").str.strip_chars()), session.raw_rows("psse_transformer",
                                                                                                          dam).select("i", "j", pl.col("ckt").str.strip_chars())])
    csv = pl.concat([session.raw_rows(t, dam).select(pl.col("psse_from_bus_number").cast(pl.Int64).alias("i"), pl.col("psse_to_bus_number").cast(pl.Int64).alias("j"),
                                            pl.col("psse_ckt_id").str.strip_chars().alias("ckt")) for t in ("dam_lines", "dam_transformers")])
    raw = raw.with_columns(pl.col("i").cast(pl.Int64), pl.col("j").cast(pl.Int64))
    as_written = csv.join(raw, on=["i", "j", "ckt"], how="semi").height
    reversed_only = csv.join(raw, on=["i", "j", "ckt"], how="anti").join(raw.rename({"i": "j", "j": "i"}), on=["i", "j", "ckt"], how="semi").height
    return {"csv_rows": csv.height, "join_as_written": as_written, "join_only_reversed": reversed_only,
            "core_branches_without_id": int(session.core_rows("branch", dam)["branch_id"].is_null().sum())}


# ---------------------------------------------------------------------- settlement points


def weights_sum_to_one(session, dam: str, crr: str) -> dict:
    """Bus weights sum to one per point; the raw weight on rows that did not resolve is handed to the others, and is counted here."""
    out = {}
    for label, sid in (("dam", dam), ("crr", crr)):
        rows = session.core_rows("settlement_point_bus", sid)
        sums = rows.filter(pl.col("is_resolved")).group_by("settlement_point_id").agg(pl.col("weight").sum())
        lost = rows.group_by("settlement_point_id").agg(pl.col("raw_weight").filter(~pl.col("is_resolved")).sum().alias("lost"),
                                                        pl.col("raw_weight").sum().alias("total"))
        lost = lost.filter((pl.col("lost") > 0) & (pl.col("total") > pl.col("lost")))
        out[label] = {"points": sums.height, "max_abs_sum_minus_one": float((sums["weight"] - 1).abs().max() or 0.0),
                      "points_with_some_weight_unresolved": lost.height,
                      "largest_unresolved_share": round(float((lost["lost"] / lost["total"]).max() or 0.0), 4)}
    return out


def point_kinds(session, dam: str, crr: str) -> dict:
    """CRR kinds from name prefixes; DAM DC-tie points placed through their zone's loads."""
    crr = session.core_rows("settlement_point", crr)
    dam = session.core_rows("settlement_point_bus", dam).join(session.core_rows("settlement_point",
                                                            dam).filter(pl.col("kind") == "dc_tie").select("settlement_point_id"), on="settlement_point_id")
    return {"crr_by_kind": dict(crr.group_by("kind").len().sort("kind").rows()),
            "dam_dc_ties_by_source": dict(dam.group_by("settlement_point_id", "source").len().group_by("source").len().sort("source").rows())}


def load_status(session, dam: str, crr: str) -> dict:
    """DAM load service status: the RAW's status against the ``Ld`` file's, for the same load."""
    raw = session.raw_rows("psse_load", dam).select(pl.col("i").cast(pl.Int64).alias("bus"), pl.col("id").str.strip_chars().alias("id"),
                                           (pl.col("status") == 1).alias("raw_in"))
    csv = session.raw_rows("dam_loads", dam).select(pl.col("psse_bus_number").cast(pl.Int64).alias("bus"), pl.col("psse_load_id").str.strip_chars().alias("id"),
                                           flag(pl.col("load_status"), "IN").alias("csv_in"))
    both = raw.join(csv, on=["bus", "id"])
    return {"loads_in_both": both.height, "status_differs": int((both["raw_in"] != both["csv_in"]).sum())}


# ---------------------------------------------------------------------- matching


def unmatched(session, dam: str, crr: str) -> dict:
    """Nothing is guessed: rows each matcher leaves unmatched, on each side."""
    out = {}
    for label, rows, a, b in (("branches", session.match_branches(crr, dam), "crr_branch_id", "dam_branch_id"),
                              ("buses", session.match_buses(crr, dam), "crr_bus_key", "dam_bus_key"),
                              ("contingencies", session.match_contingencies(crr, dam), "crr_contingency_id", "dam_contingency_id")):
        out[label] = {"rows": rows.height, "crr_unmatched": int((rows[a].is_not_null() & rows[b].is_null()).sum()),
                      "dam_unmatched": int((rows[a].is_null() & rows[b].is_not_null()).sum())}
    return out


def placeholder(session, dam: str, crr: str) -> dict:
    """The workbook's placeholder operations name: the rows behind the most and second most common names."""
    lines = session.raw_rows("crr_mapping_lines", crr)
    counts = lines.group_by("operations_name").len().sort("len", descending=True)["len"].to_list()
    return {"rows": lines.height, "most_common_name_rows": counts[0] if counts else 0, "second_most_common_name_rows": counts[1] if len(counts) > 1 else 0,
            "placeholder_found": placeholder_name(lines) is not None}


# The measurement behind each register row (docs/assumptions.md); one function may measure several rows.
MEASURES = {
    "MAT-05": unmatched,
    "MAT-06": placeholder,
    "NAM-01": crr_line_names,
    "NAM-02": crr_transformer_names,
    "NAM-03": gtc_signs,
    "NAM-04": gtl_postings,
    "NAM-06": contingency_matching,
    "NAM-10": dam_branch_names,
    "NAM-11": crr_transformer_names,
    "RAT-02": reactances,
    "RAT-03": reactances,
    "RAT-04": rate_b_against_crr_emergency,
    "RAT-06": crr_blocks,
    "RAT-07": crr_blocks,
    "RAT-08": enforced_set,
    "RAT-10": empty_contingencies,
    "SP-03": weights_sum_to_one,
    "SP-07": point_kinds,
    "SP-08": weights_sum_to_one,
    "SP-09": load_status,
    "TOP-02": key_stability,
    "TOP-03": outages_already_out,
    "TOP-04": outages_already_out,
    "TOP-05": dropped_by_reason,
    "TOP-06": slack,
    "TOP-07": ambiguous_keys,
    "TOP-09": open_ties,
    "TOP-12": crr_substations,
    "TOP-13": contingency_operations,
}
