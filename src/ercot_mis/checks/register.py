"""A measurement for each row of ``docs/assumptions.md`` that no other check measures.

Rows the other checks already measure name them in their ``check`` cell (``checks/prices.py``,
``checks/flows.py`` and so on). Every other row has a function here, registered under its id
with ``@measures``. Each one reads one DAM hour and one monthly CRR model (the newest of each by
default) and returns counts that either support the row's assumption or show where it fails.
Counts only: never a name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import polars as pl

from ..core.match import DAM_REACTANCE_FLOOR, name_key, placeholder_name
from ..out.network import Network, Options
from . import write_report

MEASURES: dict[str, Callable[[Models], dict]] = {}


def measures(*row_ids: str):
    """Register a function as the measurement behind one or more register rows."""
    def register(function):
        for row_id in row_ids:
            MEASURES[row_id] = function
        return function
    return register


@dataclass
class Models:
    """The two models every measurement reads, with their networks built once."""

    session: object
    dam: str
    crr: str
    _networks: dict = field(default_factory=dict)

    def network(self, snapshot_id: str, contract_ties: bool = False) -> Network:
        if (snapshot_id, contract_ties) not in self._networks:
            self._networks[snapshot_id, contract_ties] = self.session.network(snapshot_id, Options(contract_ties=contract_ties))
        return self._networks[snapshot_id, contract_ties]

    def core(self, table: str, snapshot_id: str) -> pl.DataFrame:
        return self.session.core_rows(table, snapshot_id)

    def raw(self, table: str, snapshot_id: str) -> pl.DataFrame:
        return self.session.raw_rows(table, snapshot_id)


def newest(session) -> tuple[str, str]:
    """Hour 12 of the newest DAM day and the newest monthly CRR model, latest revisions."""
    snaps = session.core("snapshot").collect()
    dam = snaps.filter((pl.col("model_kind") == "dam") & (pl.col("hour") == 12)).sort("operating_date", "revision")["snapshot_id"][-1]
    crr = snaps.filter(pl.col("model_kind") == "crr_monthly").sort("month", "revision")["snapshot_id"][-1]
    return dam, crr


def run(session, dam: str | None = None, crr: str | None = None) -> dict[str, dict]:
    """Every measurement; writes ``data/reports/register/<dam>__<crr>.json``."""
    default_dam, default_crr = newest(session)
    models = Models(session, dam or default_dam, crr or default_crr)
    done: dict[Callable, dict] = {}
    results = {}
    for row_id, function in sorted(MEASURES.items()):
        if function not in done:
            done[function] = function(models)
        results[row_id] = done[function]
    write_report(session, "register", f"{models.dam}__{models.crr}", {"dam": models.dam, "crr": models.crr, "rows": results})
    return results


# ---------------------------------------------------------------------- topology


@measures("TOP-02")
def key_stability(m: Models) -> dict:
    """Bus keys survive the DAM's hourly renumbering and the CRR's monthly models; node numbers do not."""
    snaps = m.session.core("snapshot").collect()
    snap = snaps.filter(pl.col("snapshot_id") == m.dam).row(0, named=True)
    next_hour = snaps.filter((pl.col("operating_date") == snap["operating_date"]) & (pl.col("hour") == snap["hour"] + 1)).sort("revision")["snapshot_id"]
    months = snaps.filter(pl.col("model_kind") == "crr_monthly").sort("month", "revision").unique(subset=["month"], keep="last").sort("month")["snapshot_id"]
    out = {}
    for label, a, b in (("dam_next_hour", m.dam, next_hour[-1] if next_hour.len() else None),
                        ("crr_previous_month", months[-2] if months.len() > 1 else None, m.crr)):
        if a is None or b is None:
            continue
        x, y = m.core("node", a), m.core("node", b)
        out[label] = {"bus_keys": x["bus_key"].n_unique(), "bus_keys_in_both": len(set(x["bus_key"]) & set(y["bus_key"])),
                      "node_numbers_in_both_with_same_key": x.join(y, on=["node_number", "bus_key"]).height}
    return out


@measures("TOP-03", "TOP-04")
def outages_already_out(m: Models) -> dict:
    """CRR: equipment the Outages file names is already out of service in the RAW, so the file adds nothing."""
    ops = m.raw("crr_mapping_lines", m.crr).select(pl.col("crr_tag").alias("branch_id"), name_key(pl.col("operations_name")).alias("ops"))
    named = m.raw("crr_outages", m.crr).select(name_key(pl.col("equipment_name")).alias("ops")).unique()
    hit = ops.join(named, on="ops").join(m.core("branch", m.crr).select("branch_id", "is_in_service"), on="branch_id")
    return {"outage_rows": m.raw("crr_outages", m.crr).height, "branches_named_by_an_outage": hit.height,
            "of_which_out_of_service_in_raw": int((~hit["is_in_service"]).sum())}


@measures("TOP-05")
def dropped_by_reason(m: Models) -> dict:
    """What the largest-component rule drops, by reason, against what it keeps."""
    return {label: {"nodes_kept": net.n_nodes, "branches_kept": net.n_branches,
                    "dropped_nodes": dict(net.dropped_nodes.group_by("reason").len().sort("reason").rows()),
                    "dropped_branches": dict(net.dropped_branches.group_by("reason").len().sort("reason").rows())}
            for label, net in (("dam", m.network(m.dam)), ("crr", m.network(m.crr)))}


@measures("TOP-06")
def slack(m: Models) -> dict:
    """The slack is the RAW's swing record: how many swing records survive, and which rule chose the slack."""
    out = {}
    for label, sid in (("dam", m.dam), ("crr", m.crr)):
        net, nodes = m.network(sid), m.core("node", sid)
        swing = nodes.filter(pl.col("node_type") == 3)
        out[label] = {"swing_records": swing.height, "swing_records_in_network": int(net.nodes["node_number"].is_in(swing["node_number"].implode()).sum()),
                      "slack_source": net.slack_source}
    return out


@measures("TOP-07")
def ambiguous_keys(m: Models) -> dict:
    """Bus keys made unique by an ordinal suffix, and how many reach the network as nodes."""
    out = {}
    for label, sid in (("dam", m.dam), ("crr", m.crr)):
        nodes = m.core("node", sid).filter(pl.col("is_ambiguous"))
        ids = set(m.network(sid).nodes["node_id"].str.split("@").list.first())
        out[label] = {"ambiguous_nodes": nodes.height, "in_network": int(nodes["bus_key"].is_in(list(ids)).sum())}
    return out


@measures("TOP-09")
def open_ties(m: Models) -> dict:
    """CRR ties out of service stay branches between separate buses, even when ties are contracted."""
    ties = m.core("branch", m.crr).filter(pl.col("is_tie"))
    open_ties = ties.filter(~pl.col("is_in_service"))
    return {"ties": ties.height, "ties_out_of_service": open_ties.height,
            "of_which_ends_in_different_buses": int((open_ties["from_bus_key"] != open_ties["to_bus_key"]).sum())}


@measures("TOP-12")
def crr_substations(m: Models) -> dict:
    """The viewer's first source for a CRR node's substation is its matched DAM node: how many CRR buses have one."""
    buses = m.session.match_buses(m.crr, m.dam)
    return {"crr_buses": buses["crr_bus_key"].drop_nulls().n_unique(),
            "with_a_matched_dam_bus": buses.drop_nulls(["crr_bus_key", "dam_bus_key"])["crr_bus_key"].n_unique()}


@measures("TOP-13")
def contingency_operations(m: Models) -> dict:
    """Every operation and action the contingency files use: anything besides an outage or a split would be misread."""
    dam = m.raw("dam_contingencies", m.dam)["contingency_operation"].str.to_uppercase().value_counts().sort("contingency_operation")
    crr = m.raw("crr_contingencies", m.crr)["action"].str.to_uppercase().value_counts().sort("action")
    return {"dam_operations": dict(dam.rows()), "crr_actions": dict(crr.rows())}


# ---------------------------------------------------------------------- impedances and ratings


@measures("RAT-02", "RAT-03")
def reactances(m: Models) -> dict:
    """Reactances below the DAM's floor, and negative ones (series capacitors), in both models."""
    out = {}
    for label, sid in (("dam", m.dam), ("crr", m.crr)):
        x = m.core("branch", sid)["x_pu"]
        out[label] = {"branches": x.len(), "below_dam_floor": int((x.abs() < DAM_REACTANCE_FLOOR).sum()), "negative": int((x < 0).sum())}
    return out


@measures("RAT-04")
def rate_b_against_crr_emergency(m: Models) -> dict:
    """DAM post-contingency limit is rate B: on matched branches, DAM rate B against the CRR emergency rating."""
    pairs = m.session.match_branches(m.crr, m.dam).drop_nulls(["crr_branch_id", "dam_branch_id"]).select("crr_branch_id", "dam_branch_id")
    crr = m.core("branch_rating", m.crr).filter((pl.col("rating_source") == "crr_monitored") & (pl.col("time_of_use") == "PeakWD"))
    dam = m.core("branch_rating", m.dam).filter(pl.col("rating_source") == "psse_raw")
    both = (pairs.join(crr.select(pl.col("branch_id").alias("crr_branch_id"), pl.col("emergency_mw").alias("crr_emergency")), on="crr_branch_id")
            .join(dam.select(pl.col("branch_id").alias("dam_branch_id"), pl.col("emergency_mw").alias("dam_rate_b"),
                             pl.col("base_mw").alias("dam_rate_a")), on="dam_branch_id")
            .filter(pl.col("crr_emergency") > 0))
    return {"matched_with_both_ratings": both.height,
            "rate_b_within_1_mw": int(((both["dam_rate_b"] - both["crr_emergency"]).abs() <= 1).sum()),
            "rate_a_within_1_mw": int(((both["dam_rate_a"] - both["crr_emergency"]).abs() <= 1).sum())}


@measures("RAT-06", "RAT-07")
def crr_blocks(m: Models) -> dict:
    """The monitored CSV carries its own ratings per time-of-use block; how many devices differ between blocks."""
    csv = m.raw("crr_monitored_lines_and_transformers", m.crr)
    spread = csv.group_by("device_name").agg(pl.col("base_case_rating").n_unique().alias("n"))
    return {"blocks": csv["time_of_use"].n_unique(), "devices": spread.height, "devices_whose_rating_differs_by_block": int((spread["n"] > 1).sum())}


@measures("RAT-08")
def enforced_set(m: Models) -> dict:
    """Every limited branch under every contingency, against the rows that actually bound in the DAM hour."""
    net = m.network(m.dam)
    snap = m.session.snapshot(m.dam)
    from .binding import hour_rows  # the checks' own reader of ERCOT's hourly reports
    bound = hour_rows(m.session, "dam_shadow_prices", snap["interval_start_utc"])
    return {"limited_branches": int(net.branches["is_limited"].sum()), "contingencies": net.contingencies.height, "binding_rows_published": bound.height}


@measures("RAT-10")
def empty_contingencies(m: Models) -> dict:
    """Contingencies dropped as empty: those with only non-branch rows, and those whose branches the model lacks."""
    out = {}
    for label, sid in (("dam", m.dam), ("crr", m.crr)):
        d = m.network(sid).dropped_contingencies
        out[label] = {"dropped": d.height, "only_other_rows": int(((d["n_dropped"] + d["n_unresolved"]) == 0).sum()),
                      "branches_absent_or_out": int(((d["n_dropped"] + d["n_unresolved"]) > 0).sum())}
    return out


# ---------------------------------------------------------------------- names, orientation, constraints


@measures("NAM-01")
def crr_line_names(m: Models) -> dict:
    """CRR line names read from RAW ``i`` to ``j``: the from node's name comes first in the name."""
    nodes = m.core("node", m.crr).select("node_number", name_key(pl.col("raw_name")).alias("n"))
    lines = (m.core("branch", m.crr).filter(pl.col("kind") == "line").select(name_key(pl.col("branch_id")).alias("b"), "from_node", "to_node")
             .join(nodes.rename({"node_number": "from_node", "n": "f"}), on="from_node").join(nodes.rename({"node_number": "to_node", "n": "t"}), on="to_node"))
    pos = lines.with_columns(pl.col("b").str.find(pl.col("f"), literal=True).alias("pf"), pl.col("b").str.find(pl.col("t"), literal=True).alias("pt"))
    both = pos.filter(pl.col("pf").is_not_null() & pl.col("pt").is_not_null() & (pl.col("f") != pl.col("t")))
    return {"lines": lines.height, "both_names_found": both.height, "from_name_first": int((both["pf"] < both["pt"]).sum())}


@measures("NAM-02", "NAM-11")
def crr_transformer_names(m: Models) -> dict:
    """CRR transformer names follow the Autos sheet, reversed against the RAW for a minority; duplicate sheet rows are counted."""
    xf = m.core("branch", m.crr).filter(pl.col("kind") == "transformer")
    autos = m.raw("crr_mapping_autos", m.crr).select("from_number", "to_number", "id")
    duplicates = autos.group_by("from_number", "to_number", "id").len().filter(pl.col("len") > 1).height
    return {"transformers": xf.height, "name_reversed": int(xf["is_name_reversed"].fill_null(False).sum()),
            "autos_rows": autos.height, "autos_keys_listed_twice": duplicates}


@measures("NAM-03")
def gtc_signs(m: Models) -> dict:
    """GTC members by the CSV's flow direction and the sign of their factor."""
    members = m.core("gtc_member", m.crr)
    return {"members": members.height,
            "by_direction_and_sign": {f"{d}|{'+' if s else '-'}": n for d, s, n in
                                      members.group_by(pl.col("flow_direction").str.to_uppercase(), pl.col("factor") >= 0).len().sort("flow_direction", "factor").rows()}}


@measures("NAM-04")
def gtl_postings(m: Models) -> dict:
    """GTL workbooks: delivery days posted more than once, and postings with no posting time (TOP-11 decides both)."""
    rows = m.session.raw("gtl_hourly").select("delivery_date", "doc_id").unique().collect()
    docs = m.session.catalog.documents("NP3-766-M", rows["doc_id"].unique())
    return {"days": rows["delivery_date"].n_unique(), "days_posted_more_than_once": int((rows.group_by("delivery_date").len()["len"] > 1).sum()),
            "postings_without_a_time": int(docs["posted_at"].is_null().sum())}


@measures("NAM-06")
def contingency_matching(m: Models) -> dict:
    """Contingency matching is one-to-one: DAM contingencies claimed twice, and CRR ones left unmatched."""
    rows = m.session.match_contingencies(m.crr, m.dam)
    matched = rows.filter(pl.col("match_method") != "unmatched").drop_nulls("dam_contingency_id")
    return {"matched": matched.height, "dam_matched_more_than_once": int((matched.group_by("dam_contingency_id").len()["len"] > 1).sum()),
            "crr_unmatched": int(((rows["match_method"] == "unmatched") & rows["crr_contingency_id"].is_not_null()).sum())}


@measures("NAM-10")
def dam_branch_names(m: Models) -> dict:
    """DAM line and transformer names join the RAW on (from, to, circuit) as written: rows that only join the other way round."""
    raw = pl.concat([m.raw("psse_branch", m.dam).select("i", "j", pl.col("ckt").str.strip_chars()), m.raw("psse_transformer",
                                                                                                          m.dam).select("i", "j", pl.col("ckt").str.strip_chars())])
    csv = pl.concat([m.raw(t, m.dam).select(pl.col("psse_from_bus_number").cast(pl.Int64).alias("i"), pl.col("psse_to_bus_number").cast(pl.Int64).alias("j"),
                                            pl.col("psse_ckt_id").str.strip_chars().alias("ckt")) for t in ("dam_lines", "dam_transformers")])
    raw = raw.with_columns(pl.col("i").cast(pl.Int64), pl.col("j").cast(pl.Int64))
    as_written = csv.join(raw, on=["i", "j", "ckt"], how="semi").height
    reversed_only = csv.join(raw, on=["i", "j", "ckt"], how="anti").join(raw.rename({"i": "j", "j": "i"}), on=["i", "j", "ckt"], how="semi").height
    return {"csv_rows": csv.height, "join_as_written": as_written, "join_only_reversed": reversed_only,
            "core_branches_without_id": int(m.core("branch", m.dam)["branch_id"].is_null().sum())}


# ---------------------------------------------------------------------- settlement points


@measures("SP-03", "SP-08")
def weights_sum_to_one(m: Models) -> dict:
    """Bus weights sum to one per point; the raw weight on rows that did not resolve is handed to the others, and is counted here."""
    out = {}
    for label, sid in (("dam", m.dam), ("crr", m.crr)):
        rows = m.core("settlement_point_bus", sid)
        sums = rows.filter(pl.col("is_resolved")).group_by("settlement_point_id").agg(pl.col("weight").sum())
        lost = rows.group_by("settlement_point_id").agg(pl.col("raw_weight").filter(~pl.col("is_resolved")).sum().alias("lost"),
                                                        pl.col("raw_weight").sum().alias("total"))
        lost = lost.filter((pl.col("lost") > 0) & (pl.col("total") > pl.col("lost")))
        out[label] = {"points": sums.height, "max_abs_sum_minus_one": float((sums["weight"] - 1).abs().max() or 0.0),
                      "points_with_some_weight_unresolved": lost.height,
                      "largest_unresolved_share": round(float((lost["lost"] / lost["total"]).max() or 0.0), 4)}
    return out


@measures("SP-07")
def point_kinds(m: Models) -> dict:
    """CRR kinds from name prefixes; DAM DC-tie points placed through their zone's loads."""
    crr = m.core("settlement_point", m.crr)
    dam = m.core("settlement_point_bus", m.dam).join(m.core("settlement_point",
                                                            m.dam).filter(pl.col("kind") == "dc_tie").select("settlement_point_id"), on="settlement_point_id")
    return {"crr_by_kind": dict(crr.group_by("kind").len().sort("kind").rows()),
            "dam_dc_ties_by_source": dict(dam.group_by("settlement_point_id", "source").len().group_by("source").len().sort("source").rows())}


@measures("SP-09")
def load_status(m: Models) -> dict:
    """DAM load service status: the RAW's status against the ``Ld`` file's, for the same load."""
    raw = m.raw("psse_load", m.dam).select(pl.col("i").cast(pl.Int64).alias("bus"), pl.col("id").str.strip_chars().alias("id"),
                                           (pl.col("status") == 1).alias("raw_in"))
    csv = m.raw("dam_loads", m.dam).select(pl.col("psse_bus_number").cast(pl.Int64).alias("bus"), pl.col("psse_load_id").str.strip_chars().alias("id"),
                                           pl.col("load_status").str.to_uppercase().str.starts_with("IN").alias("csv_in"))
    both = raw.join(csv, on=["bus", "id"])
    return {"loads_in_both": both.height, "status_differs": int((both["raw_in"] != both["csv_in"]).sum())}


# ---------------------------------------------------------------------- matching


@measures("MAT-05")
def unmatched(m: Models) -> dict:
    """Nothing is guessed: rows each matcher leaves unmatched, on each side."""
    out = {}
    for label, rows, a, b in (("branches", m.session.match_branches(m.crr, m.dam), "crr_branch_id", "dam_branch_id"),
                              ("buses", m.session.match_buses(m.crr, m.dam), "crr_bus_key", "dam_bus_key"),
                              ("contingencies", m.session.match_contingencies(m.crr, m.dam), "crr_contingency_id", "dam_contingency_id")):
        out[label] = {"rows": rows.height, "crr_unmatched": int((rows[a].is_not_null() & rows[b].is_null()).sum()),
                      "dam_unmatched": int((rows[a].is_null() & rows[b].is_not_null()).sum())}
    return out


@measures("MAT-06")
def placeholder(m: Models) -> dict:
    """The workbook's placeholder operations name: the rows behind the most and second most common names."""
    lines = m.raw("crr_mapping_lines", m.crr)
    counts = lines.group_by("operations_name").len().sort("len", descending=True)["len"].to_list()
    return {"rows": lines.height, "most_common_name_rows": counts[0] if counts else 0, "second_most_common_name_rows": counts[1] if len(counts) > 1 else 0,
            "placeholder_found": placeholder_name(lines) is not None}
