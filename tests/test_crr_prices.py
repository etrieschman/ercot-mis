import polars as pl

from ercot_mis.checks import crr_prices

HOURS = {"PeakWD": 320, "PeakWE": 160, "Off-peak": 240}


def test_obligations_clear_at_source_minus_sink_and_linked_bids_at_the_hour_weighted_mean():
    published = pl.DataFrame({"source_sink": ["A", "B"] * 3, "time_of_use": ["PeakWD", "PeakWD", "PeakWE", "PeakWE", "Off-peak", "Off-peak"],
                              "shadow_price_per_mwh": [5.0, 1.0, 3.0, 1.0, 2.0, 1.0]})
    weighted = (4.0 * 320 + 2.0 * 160 + 1.0 * 240) / 720
    awards = pl.DataFrame({"hedge_type": ["OBL", "OBL", "OBL", "OBL", "OPT"], "bid_24_hour": ["No", "Yes", "Yes", "Yes", "No"],
                           "source": ["A"] * 5, "sink": ["B"] * 5, "time_of_use": ["PeakWD", "PeakWD", "PeakWE", "Off-peak", "PeakWD"],
                           "shadow_price_per_mwh": [4.0, weighted, weighted, weighted, 9.0]})
    out = crr_prices.obligation_prices(awards, published, HOURS)
    assert (out["awards"], out["unpriced_ends"]) == (4, 0)
    assert out["single_block"]["n"] == 1 and out["single_block"]["within_0.01"] == 1
    assert out["linked_three_blocks"]["n"] == 3 and out["linked_three_blocks"]["within_0.01"] == 3
