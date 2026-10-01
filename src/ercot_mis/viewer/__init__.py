"""A single-file network viewer: one or two snapshots, station by station.

``build(session, left, right)`` packs the core tables of the snapshots into one HTML
file (``template.html`` plus the data as JSON) under ``data/reports/viewer/``. The page
shows one station at a time: its buses by voltage level, the ties, lines and
transformers between them with their status, loads, generators and settlement points,
and the neighbouring stations as boxes to click through to. With two snapshots the
panes stay on the same station and each branch is coloured by how it compares with
the other side.

The file holds ERCOT CEII (names of stations, buses and equipment). It is written
inside the data folder, owner-only, says so in a banner, and must not be published.

**Stations are a display choice, not a core fact.** The DAM RAW names every bus by its
station. The CRR RAW names buses, not stations, so a CRR bus takes the station of the
DAM node it is matched to (``core.match_node`` against ``reference_dam``); a bus with no
match takes the station of a bus it is tied or transformed to, then the station its
matched lines lead to, and failing that keeps its own bus name, marked ``~``.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

TEMPLATE = Path(__file__).with_name("template.html")


def _is_dam(snapshot_id: str) -> bool:
    return snapshot_id.startswith("dam:")


def _rows(session, table: str, snapshot_id: str) -> pl.DataFrame:
    return session.core(table).filter(pl.col("snapshot_id") == snapshot_id).collect()


def crr_stations(session, crr_id: str, dam_id: str, node: pl.DataFrame, branch: pl.DataFrame) -> dict[int, str]:
    """A station name for every CRR bus (see the module docstring)."""
    dam_station = dict(_rows(session, "node", dam_id).select("node_key", "station").rows())
    matched = {c: dam_station.get(d) for c, d in session.match_nodes(crr_id, dam_id).drop_nulls(["crr_node_key", "dam_node_key"]).select("crr_node_key", "dam_node_key").rows()}
    station = {bus: matched[key] for bus, key in node.select("psse_bus_number", "node_key").rows() if matched.get(key)}
    # Spread to unmatched buses over ties and transformers (equipment inside a station), whatever their status.
    inside = branch.filter(pl.col("is_tie") | (pl.col("kind") == "transformer")).select("from_bus", "to_bus").rows()
    changed = True
    while changed:
        changed = False
        for a, b in inside:
            if a in station and b not in station:
                station[b], changed = station[a], True
            elif b in station and a not in station:
                station[a], changed = station[b], True
    # Then along matched lines: when one end of a CRR line is placed and its DAM line joins that
    # station to another, the other end is in the other station. This places buses the node match
    # left undecided between several DAM buses of one station.
    dam_bus_station = dict(_rows(session, "node", dam_id).select("psse_bus_number", "station").rows())
    dam_ends = {i: (dam_bus_station.get(f), dam_bus_station.get(t)) for i, f, t in _rows(session, "branch", dam_id).select("branch_id", "from_bus", "to_bus").rows()}
    crr_ends = dict((i, (f, t)) for i, f, t in branch.select("branch_id", "from_bus", "to_bus").rows())
    pairs = [(crr_ends[c], dam_ends[d]) for c, d in session.match_branches(crr_id, dam_id).drop_nulls(["crr_branch_id", "dam_branch_id"])
             .select("crr_branch_id", "dam_branch_id").rows() if c in crr_ends and d in dam_ends and None not in dam_ends[d]]
    changed = True
    while changed:
        changed = False
        for (f, t), (sf, st) in pairs:
            if f in station and t in station:
                continue
            if sf == st:
                station.setdefault(f, sf), station.setdefault(t, st)
            elif f in station and station[f] in (sf, st):
                station[t] = st if station[f] == sf else sf
            elif t in station and station[t] in (sf, st):
                station[f] = sf if station[t] == st else st
            else:
                continue
            changed = True
        for a, b in inside:
            if a in station and b not in station:
                station[b], changed = station[a], True
            elif b in station and a not in station:
                station[a], changed = station[b], True
    for bus, name in node.select("psse_bus_number", "station").rows():
        station.setdefault(bus, f"~{name}")
    return station


def model(session, snapshot_id: str, reference_dam: str | None = None) -> dict:
    """One snapshot as the page reads it: buses, branches with limits, loads, attached names."""
    node, branch, load = (_rows(session, t, snapshot_id) for t in ("node", "branch", "load"))
    if node.is_empty():
        raise KeyError(f"no core rows for {snapshot_id!r}; run build_core()")
    dam = _is_dam(snapshot_id)
    if dam:
        station = dict(node.select("psse_bus_number", "station").rows())
    else:
        if reference_dam is None:
            raise ValueError("a CRR snapshot needs reference_dam (a DAM snapshot to take station names from)")
        station = crr_stations(session, snapshot_id, reference_dam, node, branch)
    source = "psse_raw" if dam else "crr_monitored"
    rating = _rows(session, "branch_rating", snapshot_id).filter(pl.col("rating_source") == source)
    if not dam:
        rating = rating.filter(pl.col("time_of_use") == "PeakWD")
    rating = rating.select("branch_id", "base_mw", "emergency_mw").unique(subset=["branch_id"], keep="first")
    raw = _rows(session, "branch_rating", snapshot_id).filter(pl.col("rating_source") == "psse_raw").select(
        "branch_id", pl.col("base_mw").alias("rate_a"), pl.col("emergency_mw").alias("rate_b")).unique(subset=["branch_id"], keep="first")
    branch = branch.join(rating, on="branch_id", how="left").join(raw, on="branch_id", how="left")
    ctg = (_rows(session, "contingency_outage", snapshot_id).filter(pl.col("branch_id").is_not_null())
           .group_by("branch_id").agg(pl.col("contingency_id").unique().sort().alias("ctg")))
    branch = branch.join(ctg, on="branch_id", how="left")

    # Settlement points by node: resource nodes and DC ties sit on a bus; hubs and load zones are sets of buses.
    points = (_rows(session, "settlement_point_node", snapshot_id).filter(pl.col("is_resolved"))
              .join(_rows(session, "settlement_point", snapshot_id).select("settlement_point_id", "kind"), on="settlement_point_id"))
    on_node: dict[str, list[str]] = {}
    part_of: dict[str, list[str]] = {}
    for sp_id, key, kind in points.select("settlement_point_id", "node_key", "kind").sort("settlement_point_id").rows():
        (on_node if kind in ("resource_node", "dc_tie") else part_of).setdefault(key, []).append(sp_id)
    # A three-winding transformer is three two-winding legs meeting at a fictitious star bus (1 kV, transformers only).
    ends = pl.concat([branch.select(pl.col("from_bus").alias("bus"), "kind"), branch.select(pl.col("to_bus").alias("bus"), "kind")])
    stars = set(ends.group_by("bus").agg(pl.len().alias("n"), (pl.col("kind") == "transformer").sum().alias("nt"))
                .filter((pl.col("n") == pl.col("nt")) & (pl.col("nt") >= 3))["bus"]) & set(node.filter(pl.col("kv") <= 1.0)["psse_bus_number"])

    def labels(text: str, prefix: str) -> list[str]:
        return [part[2:] for part in (text or "").split("|") if part.startswith(prefix)]

    return {
        "id": snapshot_id, "kind": "dam" if dam else "crr",
        "limit_source": "RAW rate A / B" if dam else "CRR monitored CSV, PeakWD",
        "buses": [{"n": bus, "kv": kv, "st": station[bus], "name": name, "type": bus_type, "key": key,
                   "sp": on_node.get(key, []), "agg": part_of.get(key, []), "gen": labels(att, "G:"), "star": bus in stars}
                  for bus, kv, name, bus_type, key, att in node.select("psse_bus_number", "kv", "station", "bus_type", "node_key", "attachments").rows()],
        "branches": [{"id": i, "k": kind[0].upper(), "f": f, "t": t, "ckt": ckt, "x": x, "on": on, "tie": tie, "mon": bool(mon), "sec": bool(sec),
                      "base": base, "emer": emer, "ra": ra, "rb": rb, "ctg": c or []}
                     for i, kind, f, t, ckt, x, on, tie, mon, sec, base, emer, ra, rb, c in branch.select(
                         "branch_id", "kind", "from_bus", "to_bus", "ckt", "x_pu", "is_in_service", "is_tie", "is_monitored", "is_secured",
                         "base_mw", "emergency_mw", "rate_a", "rate_b", "ctg").rows()],
        "loads": [{"id": i, "bus": bus, "on": on, "mw": mw, "ldf": ldf, "zone": zone}
                  for i, bus, on, mw, ldf, zone in load.select("load_id", "psse_bus", "is_in_service", "mw", "mw_ldf", "load_zone").rows()],
    }


def comparison(session, left: str, right: str) -> dict:
    """Per branch id of each side: the other side's id (or null) and whether reactance and service agree."""
    out = {"left": {}, "right": {}}
    if _is_dam(left) != _is_dam(right):
        crr, dam = (right, left) if _is_dam(left) else (left, right)
        pairs = session.diff_branches(crr, dam).select("crr_branch_id", "dam_branch_id", "same_reactance", "crr_in_service", "dam_in_service").rows()
        for c, d, same_x, c_on, d_on in pairs:
            same = bool(same_x) and c_on == d_on
            a, b = (d, c) if _is_dam(left) else (c, d)
            out["left"][a], out["right"][b] = {"other": b, "same": same}, {"other": a, "same": same}
        return out
    # Same vocabulary on both sides: branch ids are the key.
    cols = ("branch_id", "x_pu", "is_in_service")
    a = dict((i, (x, on)) for i, x, on in _rows(session, "branch", left).select(cols).rows())
    b = dict((i, (x, on)) for i, x, on in _rows(session, "branch", right).select(cols).rows())
    for i in a.keys() & b.keys():
        same = a[i] == b[i]
        out["left"][i] = out["right"][i] = {"other": i, "same": same}
    return out


def build(session, left: str, right: str | None = None, *, reference_dam: str | None = None, path: Path | None = None) -> Path:
    """Write the viewer for one snapshot or a pair; returns the file's path (inside the data folder)."""
    ids = [left] + ([right] if right else [])
    if reference_dam is None:
        dams = [i for i in ids if _is_dam(i)]
        if dams:
            reference_dam = dams[0]
        else:
            snaps = session.core("snapshot").filter((pl.col("model_kind") == "dam") & (pl.col("hour") == 12)).sort("operating_date", "revision").collect()
            reference_dam = snaps["snapshot_id"][-1]
    data = {"models": [model(session, i, reference_dam) for i in ids], "reference_dam": reference_dam,
            "compare": comparison(session, left, right) if right else None}
    html = TEMPLATE.read_text().replace("/*DATA*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    if path is None:
        folder = session.data_dir / "reports" / "viewer"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / ("__".join(ids).replace(":", "-") + ".html")
    path.write_text(html)
    path.chmod(0o600)
    return path
