import pytest

from arbscan.bankroll import PickRules, simulate, window_rate

LOOSE = PickRules(min_window_s=1, min_annualized=0.10, max_edge=None, max_days=None, max_stake=None)


def w(start, cost, profit, days, open_s=10, edge=0.02):
    return {"start_ts": start, "end_ts": start + open_s, "cost_at_max": cost, "max_profit": profit,
            "days_to_resolve": days, "max_top_edge": edge}


def test_cash_is_tied_up_until_resolution():
    windows = [
        w(0, 400, 8, days=30),         # 2% for a month: ~24%/yr, taken; $400 tied up for 30 days
        w(100, 400, 40, days=0.01),    # only $100 left: a quarter of it, tied up for the minimum hour
        w(2000, 100, 10, days=0.01),   # nothing free yet, and it's gone before the hour is up
        w(4000, 100, 10, days=0.01),   # the quarter-size stake came back
        w(4500, 50, 5, days=1, open_s=0),    # gone before anyone could act
        w(5000, 100, 0.01, days=30),   # pays less than 10% a year
    ]
    r = simulate(windows, 500, LOOSE)
    assert r["taken"] == 3
    assert r["profit"] == pytest.approx(8 + 40 * 0.25 + 10)
    assert r["skipped"] == {"too short": 1, "low return": 1, "no cash": 1}
    assert r["tied_up"] == pytest.approx(400 + 100)  # the month-long stake and the last one


def test_best_open_window_gets_the_cash_first():
    slow = w(0, 100, 2, days=30)     # 2% over a month: ~24%/yr
    fast = w(0, 100, 1, days=0.1)    # 1% by tonight: ~15x a year
    r = simulate([slow, fast], 100, LOOSE)
    assert r["taken"] == 1 and r["profit"] == pytest.approx(1)
    assert window_rate(fast) > window_rate(slow)


def test_pick_rules():
    rules = PickRules(min_window_s=1, min_annualized=1.0, max_edge=0.05, max_days=7)
    assert rules.window_reason(w(0, 100, 1, days=0.5)) is None
    assert rules.window_reason(w(0, 100, 30, days=0.5, edge=0.30)) == "suspicious edge"
    assert rules.window_reason(w(0, 100, 1, days=0.5, open_s=0)) == "too short"
    assert rules.window_reason(w(0, 100, 5, days=30)) == "resolves too late"
    assert rules.window_reason(w(0, 100, 5, days=None)) == "resolves too late"
    assert rules.window_reason(w(0, 100, 0.5, days=5)) == "low return"  # 0.5% over 5 days: ~37%/yr
    assert PickRules(max_days=None).window_reason(w(0, 100, 30, days=30)) is None


def test_no_cash_left():
    r = simulate([w(0, 500, 5, days=60), w(10, 100, 50, days=0.01)], 500, PickRules(min_annualized=0, max_days=None, max_stake=None))
    assert r["taken"] == 1 and r["skipped"] == {"no cash": 1}


def test_suspicious_edges_are_skipped():
    rules = PickRules(min_window_s=1, min_annualized=0.1, max_edge=0.05, max_days=None)
    r = simulate([w(0, 300, 120, days=0.01, edge=0.40), w(10, 100, 3, days=0.01)], 500, rules)
    assert r["taken"] == 1 and r["profit"] == pytest.approx(3) and r["skipped"] == {"suspicious edge": 1}


def test_stake_cap_shrinks_with_lock_up():
    rules = PickRules(max_stake=0.5)
    assert rules.stake_fraction(0.2) == 0.5 and rules.stake_fraction(1) == 0.5
    assert rules.stake_fraction(2) == pytest.approx(0.25) and rules.stake_fraction(7) == pytest.approx(0.5 / 7)
    # A 3-day pick gets a sixth of the money, leaving the rest for a same-day one.
    slow, fast = w(0, 300, 9, days=3), w(100, 300, 3, days=0.2)
    r = simulate([slow, fast], 300, PickRules(min_annualized=0.1, max_stake=0.5))
    assert r["taken"] == 2
    assert r["profit"] == pytest.approx(9 * (50 / 300) + 3 * (150 / 300))  # fast: capped at half of $300
