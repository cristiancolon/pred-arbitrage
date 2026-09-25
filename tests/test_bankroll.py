import pytest

from arbscan.bankroll import simulate


def w(start, cost, profit, days, open_s=10, edge=0.02):
    return {"start_ts": start, "end_ts": start + open_s, "cost_at_max": cost, "max_profit": profit,
            "days_to_resolve": days, "max_top_edge": edge}


def test_cash_is_tied_up_until_resolution():
    windows = [
        w(0, 400, 8, days=30),         # 2% for a month: ~24%/yr, taken; $400 tied up for 30 days
        w(100, 400, 40, days=0.01),    # only $100 left: a quarter of it
        w(2000, 100, 10, days=0.01),   # the quarter-size window resolved (~15 min) and freed its cash
        w(3000, 50, 5, days=1, open_s=0),   # gone before anyone could act
        w(4000, 100, 0.01, days=30),   # pays less than 10% a year
    ]
    r = simulate(windows, 500, min_window_s=1, min_annualized=0.10)
    assert r["taken"] == 3
    assert r["profit"] == pytest.approx(8 + 40 * 0.25 + 10)
    assert r["skipped"] == {"too short": 1, "low return": 1}
    assert r["tied_up"] == pytest.approx(400)  # only the month-long stake is still waiting to resolve


def test_no_cash_left():
    r = simulate([w(0, 500, 5, days=60), w(10, 100, 50, days=0.01)], 500, 1, 0)
    assert r["taken"] == 1 and r["skipped"] == {"no cash": 1}


def test_suspicious_edges_are_skipped():
    r = simulate([w(0, 300, 120, days=0.01, edge=0.40), w(10, 100, 3, days=0.01)], 500, 1, 0.1, max_edge=0.05)
    assert r["taken"] == 1 and r["profit"] == pytest.approx(3) and r["skipped"] == {"suspicious edge": 1}
