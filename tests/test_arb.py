import pytest

from arbscan.arb import Leg, directions, top_edge, walk
from arbscan.book import kalshi_ladders, pmus_ladders
from arbscan.fees import kalshi_taker_coef, per_contract

K = 0.07
P = 0.0695


def test_kalshi_coef():
    assert kalshi_taker_coef("quadratic", 1) == pytest.approx(0.07)
    assert kalshi_taker_coef("quadratic_with_maker_fees", 0.5) == pytest.approx(0.035)
    assert kalshi_taker_coef("quadratic", 0) == 0
    # Unknown fee types never understate cost.
    assert kalshi_taker_coef("flat", 0.5) == pytest.approx(0.07)
    assert kalshi_taker_coef(None, None) == pytest.approx(0.07)


def test_fee_peaks_at_half():
    assert per_contract(K, 0.5) == pytest.approx(0.0175)
    assert per_contract(K, 0.1) == pytest.approx(0.0063)
    assert per_contract(K, 0.0) == 0


def test_kalshi_ladders_complement_bids():
    ob = {
        "yes_dollars": [["0.0100", "100.00"], ["0.4000", "5.00"]],
        "no_dollars": [["0.5500", "7.50"], ["0.5000", "20.00"]],
    }
    yes_asks, no_asks = kalshi_ladders(ob)
    # Best NO bid 0.55 -> YES ask 0.45; sorted cheapest first.
    assert yes_asks == [(0.45, 7.5), (0.5, 20.0)]
    assert no_asks == [(0.6, 5.0), (0.99, 100.0)]


def test_kalshi_ladders_empty():
    assert kalshi_ladders({"yes_dollars": [], "no_dollars": None}) == ([], [])


def test_pmus_ladders():
    md = {
        "bids": [{"px": {"value": "0.4100"}, "qty": "10"}, {"px": {"value": "0.4000"}, "qty": "3"}],
        "offers": [{"px": {"value": "0.4300"}, "qty": "8"}, {"px": {"value": "0.4200"}, "qty": "2"}],
    }
    yes_asks, no_asks = pmus_ladders(md)
    assert yes_asks == [(0.42, 2.0), (0.43, 8.0)]
    assert no_asks == [(0.59, 10.0), (0.6, 3.0)]


def test_top_edge_matches_worked_example():
    # 48c + 48c with Kalshi and Polymarket US taker fees -> ~0.5c.
    e = top_edge(0.48, K, 0.48, P)
    assert e == pytest.approx(0.04 - 0.07 * 0.2496 - 0.0695 * 0.2496)
    assert 0.004 < e < 0.006
    assert top_edge(None, K, 0.4, P) is None


def test_walk_no_arb():
    r = walk(Leg([(0.5, 100)], K), Leg([(0.5, 100)], P))
    assert r.size == 0 and r.profit == 0 and not r.positive
    assert r.top_edge < 0


def test_walk_single_level():
    r = walk(Leg([(0.40, 10)], K), Leg([(0.50, 25)], P))
    unit = 0.9 + per_contract(K, 0.4) + per_contract(P, 0.5)
    assert r.size == 10
    assert r.cost == pytest.approx(10 * unit)
    assert r.profit == pytest.approx(10 * (1 - unit))
    assert r.positive


def test_walk_stops_when_marginal_edge_gone():
    a = Leg([(0.40, 5), (0.45, 5), (0.60, 50)], K)
    b = Leg([(0.50, 8), (0.52, 100)], P)
    r = walk(a, b)
    # Pairs: 5 @ .40+.50, 3 @ .45+.50; then .45+.52 plus ~3.5c of fees is over $1.
    last_unit = 0.95 + per_contract(K, 0.45) + per_contract(P, 0.5)
    exp_cost = 5 * (0.9 + per_contract(K, 0.4) + per_contract(P, 0.5)) + 3 * last_unit
    assert 0.97 + per_contract(K, 0.45) + per_contract(P, 0.52) > 1
    assert r.size == 8
    assert r.cost == pytest.approx(exp_cost)
    assert r.last_edge == pytest.approx(1 - last_unit)


def test_walk_min_edge_threshold():
    a = Leg([(0.40, 5), (0.45, 5)], K)
    b = Leg([(0.50, 100)], P)
    r = walk(a, b, min_edge=0.03)  # second level edge is ~0.0152, below threshold
    assert r.size == 5


def test_walk_floors_fractional_kalshi_size():
    r = walk(Leg([(0.30, 2.6)], K), Leg([(0.60, 10)], P))
    unit = 0.9 + per_contract(K, 0.3) + per_contract(P, 0.6)
    assert r.size == 2
    assert r.cost == pytest.approx(2 * unit)


def test_walk_less_than_one_contract():
    r = walk(Leg([(0.30, 0.4)], K), Leg([(0.60, 10)], P))
    assert r.size == 0 and not r.positive
    assert r.top_edge > 0


def test_walk_max_size():
    r = walk(Leg([(0.30, 100)], K), Leg([(0.60, 100)], P), max_size=7)
    assert r.size == 7


def test_walk_empty_side():
    r = walk(Leg([], K), Leg([(0.5, 1)], P))
    assert r.top_edge is None and r.size == 0


def test_directions():
    assert [d[0] for d in directions("same")] == ["K:YES+P:NO", "K:NO+P:YES"]
    assert directions("inverse")[0][1:] == ("yes", "yes")
    with pytest.raises(KeyError):
        directions("sideways")


def test_walk_stops_at_each_venues_budget():
    # Deep books, 10c edge before fees: the cash on each venue is the limit.
    a, b = Leg([(0.40, 10_000)], 0.0), Leg([(0.50, 10_000)], 0.0)
    res = walk(a, b, budget_a=100.0, budget_b=100.0)
    assert res.size == 200  # $100 buys 250 at 40c but only 200 at 50c
    assert res.cost == pytest.approx(200 * 0.90) and res.profit == pytest.approx(20.0)
    with_fees = walk(Leg([(0.40, 10_000)], 0.07), Leg([(0.50, 10_000)], 0.0695), budget_a=100.0, budget_b=100.0)
    assert with_fees.size == 193  # 50c + 1.74c fee per contract
