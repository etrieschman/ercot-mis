"""Signed awards and their sums on synthetic disclosure rows."""

import polars as pl

from ercot_mis.core import award


def _frame(**cols):
    n = len(next(iter(cols.values())))
    return pl.DataFrame({"delivery_date": ["08/05/2026"] * n, "hour_ending": ["1:00"] * n, **cols})


def test_awards_are_signed_as_injections_and_balance():
    gen = _frame(settlement_point_name=["RN_A", "RN_B"], awarded_quantity=[100.0, 0.0], resource_name=["G1", "G2"])
    esr = _frame(settlement_point_name=["RN_S"], awarded_quantity=[-20.0], resource_name=["S1"])  # charging
    offers = _frame(settlement_point=["HB_X"], energy_only_offer_award_in_mw=[30.0], offer_id=["o1"])
    bids = _frame(settlement_point=["LZ_Q"], energy_only_bid_award_in_mw=[-110.0], bid_id=["b1"])  # ERCOT writes bid awards negative
    ptp_bids = _frame(settlement_point_source=["RN_A"], settlement_point_sink=["LZ_Q"], pt_p_bid_award_mw=[40.0], bid_id=["p1"])
    ptp_options = _frame(settlement_point_source=["HB_X"], settlement_point_sink=["RN_B"], mw=[5.0], offer_id=["x1"])
    rows = award.hourly_awards(gen, esr, offers, bids, ptp_bids, ptp_options)
    assert list(rows.columns) == list(award.COLUMNS) and rows.height == 8  # the zero award is dropped
    by_kind = dict(rows.group_by("kind").agg(pl.col("mw").sum()).rows())
    assert by_kind == {"generation": 100.0, "storage": -20.0, "energy_only_offer": 30.0, "energy_bid": -110.0, "ptp_obligation_source": 45.0, "ptp_obligation_sink": -45.0}
    assert rows["interval_start_utc"][0].hour == 5  # hour ending 1 on a summer day starts at 05:00 UTC
    q = award.net_injections(rows)
    assert dict(q.select("settlement_point", "net_mw").rows()) == {"RN_A": 140.0, "RN_S": -20.0, "HB_X": 35.0, "LZ_Q": -150.0, "RN_B": -5.0}
    bal = award.hourly_balance(rows)
    assert bal.height == 1 and bal["net_mw"][0] == 0.0 and bal["supply_mw"][0] == 175.0
