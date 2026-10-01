"""The replay of recorded decisions (backtest.py), on small hand-made records."""

import json
import random

import pytest

from arbscan import backtest
from arbscan.backtest import BEFORE, NOW, Decision, Policy, simulate
from arbscan.store import connect

T0 = 1_000_000.0
FAST = {"look": {"K": 0.05, "P": 0.12}, "rtt": {"K": 0.09, "P": 0.18}}


def row(src="paper", steady=(2.0, 60.0), n=10, profit=0.20, **kw):
    """YES on Kalshi at 45c, NO on Polymarket US at 50c (its YES bid 50c)."""
    books = {"seen": {"K": [[0.45, 100.0], [0.46, 50.0]], "P": [[0.50, 30.0], [0.52, 100.0]]},
             "steady": {"K": steady[0], "P": steady[1]}, "lead": "K"}
    return {"id": "t1", "ts": T0, "src": src, "pair": "K-1|p-1", "direction": "K:YES+P:NO", "k_side": "yes",
            "p_side": "no", "planned_size": n, "planned_profit": profit, "k_limit": 0.45, "p_limit": 0.50,
            "status": "open", "unwind_qty": 0.0, "books": books, **kw}


def quotes(*changes):
    """Quote rows: the books as decided on, then ``changes`` (seconds after, K YES ask, its size, P YES bid)."""
    rows = [(T0 - 30, 0.45, 100.0, 0.57, 100.0, 0.50, 0.52)]
    for dt, k_ask, k_sz, p_bid in changes:
        rows.append((T0 + dt, k_ask, k_sz, 0.57, 100.0, p_bid, 0.52))
    return rows


def test_both_legs_fill_when_the_books_stay_put():
    d = Decision(row(), quotes(), [], [])
    for pol in (BEFORE, NOW):
        out, orders = simulate(d, pol, FAST)
        assert out == "filled" and [o[1:] for o in orders] == [(10, 10), (10, 10)]
    assert [o[0] for o in simulate(d, BEFORE, FAST)[1]] == ["K", "P"]  # Kalshi moved last: it led
    assert [o[0] for o in simulate(d, NOW, FAST)[1]] == ["P", "K"]


def test_a_polymarket_offer_that_goes_costs_a_sale_when_kalshi_led_and_nothing_when_it_didnt():
    # Polymarket's bid drops 0.2 s after the decision (on our feed): NO at 50c is gone.
    d = Decision(row(), quotes((0.2, 0.45, 100.0, 0.47)), [], [])
    out, orders = simulate(d, BEFORE, FAST)  # Kalshi at +0.05, then Polymarket at +0.09 + 0.12 (+ its feed lag)
    assert out == "sold back" and orders == [("K", 10, 10), ("P", 10, 0), ("P", 10, 0)]
    out, orders = simulate(d, NOW, FAST)  # Polymarket first, at +0.12: seen at +0.24 on the feed, gone by then
    assert out == "missed" and orders == [("P", 10, 0)]
    # With a quicker trip it still catches the offer, and Kalshi follows.
    quick = {"look": {"K": 0.05, "P": 0.05}, "rtt": {"K": 0.09, "P": 0.10}}
    assert simulate(d, NOW, quick)[0] == "filled"


def test_the_second_leg_follows_for_what_the_first_filled():
    r = row()
    r["books"]["seen"]["P"] = [[0.50, 6.0], [0.52, 100.0]]  # six at the limit, ten wanted (sized on an older book)
    d = Decision(r, quotes(), [], [])
    assert simulate(d, BEFORE, FAST) == ("sold back", [("K", 10, 10), ("P", 10, 6), ("P", 4, 0)])
    assert simulate(d, Policy("p", lead="P"), FAST) == ("filled", [("P", 10, 6), ("K", 6, 6)])
    assert simulate(d, NOW, FAST) == ("filled", [("P", 6, 6), ("K", 6, 6)])  # checked: sized on the book as it is


def test_paying_up_to_break_even_on_the_second_leg():
    # Kalshi's 45c goes 0.2 s after the decision; 46c is still there, and the pair has a cent of room.
    d = Decision(row(steady=(60.0, 60.0)), quotes((0.2, 0.46, 50.0, 0.50)), [], [])
    assert simulate(d, Policy("p", lead="P"), FAST) == ("sold back", [("P", 10, 10), ("K", 10, 0), ("K", 10, 0)])
    assert simulate(d, NOW, FAST) == ("filled", [("P", 10, 10), ("K", 10, 10)])
    # A cent and a half of planned profit a contract is counted as no room: never more than the live cap.
    thin = Decision(row(steady=(60.0, 60.0), profit=0.15), quotes((0.2, 0.46, 50.0, 0.50)), [], [])
    assert thin.room() == 0.0 and simulate(thin, NOW, FAST)[0] == "sold back"
    assert Decision(row(), quotes(), [], []).room() == pytest.approx(0.01)


def test_both_prices_must_have_settled():
    # Both legs moved 2 s ago; Kalshi moves again a second later. Waiting for 3 s of quiet skips the pick.
    d = Decision(row(steady=(2.0, 2.1)), quotes((0.5, 0.46, 50.0, 0.50)), [], [])
    assert simulate(d, NOW, FAST) == ("skipped", [])
    assert simulate(d, Policy("p", lead="P", breakeven=True), FAST)[0] == "filled"  # caught, at break-even
    still = Decision(row(steady=(2.0, 2.1)), quotes(), [], [])
    assert simulate(still, NOW, FAST)[0] == "filled"  # nothing moved during the wait: traded 0.9 s later


def live_orders(p_filled):
    return [{"ts": T0 + 0.005, "venue": "K", "action": "buy", "qty": 10, "limit_price": 0.45, "filled": 10,
             "rtt_ms": 90, "exch_ts": T0 + 0.05},
            {"ts": T0 + 0.10, "venue": "P", "action": "buy", "qty": 10, "limit_price": 0.50, "filled": p_filled,
             "rtt_ms": 180, "exch_ts": T0 + 0.22}]


def test_a_frozen_book_is_known_from_the_order_that_found_nothing():
    # The Polymarket order found nothing, and our feed went on showing the offer: the feed was wrong.
    r = row(src="live", status="settled", unwind_qty=10.0)
    d = Decision(r, quotes(), [], live_orders(0))
    assert d.frozen and d.real() == "sold back"
    assert simulate(d, BEFORE, FAST)[0] == "sold back"
    assert simulate(d, NOW, FAST) == ("skipped", [])  # the check repairs the book; the pick is gone
    blind = Policy("b", lead="P", check=True, check_sees_frozen=False)
    assert simulate(d, blind, FAST) == ("missed", [("P", 10, 0)])  # at worst an order that costs nothing
    # An offer our feed saw go is not a frozen book.
    gone = Decision(r, quotes((0.3, 0.45, 100.0, 0.47)), [], live_orders(0))
    assert not gone.frozen
    assert backtest.agreement([d, gone]) == (2, 2)


def test_what_our_own_order_took_is_put_back_and_a_short_fill_caps_the_leg():
    # Live: Kalshi filled 10 (the feed then showed 90 left), Polymarket only 4 of 10.
    r = row(src="live", unwind_qty=6.0)
    d = Decision(r, quotes((0.09, 0.45, 90.0, 0.50)), [], live_orders(4))
    assert d.avail("K", T0 + 1.0) == 100 and not d.frozen  # not the 90 the feed showed: our 10 are put back
    assert simulate(d, BEFORE, FAST) == ("sold back", [("K", 10, 10), ("P", 10, 4), ("P", 6, 0)])
    # Polymarket first, 0.1 s sooner than the real order: only the feed can say, and it showed 30.
    assert simulate(d, NOW, FAST)[1][0] == ("P", 10, 10)
    late = {"look": {"K": 0.05, "P": 0.25}, "rtt": {"K": 0.09, "P": 0.3}}
    assert simulate(d, NOW, late) == ("filled", [("P", 10, 4), ("K", 4, 4)])  # no sooner: what it really got


def test_loading_and_replaying_from_the_database(tmp_path):
    db = connect(str(tmp_path / "b.db"))
    cols = ("id", "ts", "pair", "direction", "k_side", "p_side", "planned_size", "planned_profit", "k_limit",
            "p_limit", "status", "unwind_qty", "books")
    for table, tid, dt in (("live_trades", "L1", 0.0), ("paper_trades", "P1", 0.4), ("paper_trades", "P2", 400.0)):
        r = row()
        r.update(id=tid, ts=T0 + dt, books=json.dumps(r["books"]))
        db.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   tuple(r[c] for c in cols))
    db.execute("INSERT INTO paper_trades (id, ts, pair, direction, k_side, p_side, planned_size, planned_profit, "
               "k_limit, p_limit, status, books) VALUES ('old', ?, 'K-9|p-9', 'K:YES+P:NO', 'yes', 'no', 5, 0.1, "
               "0.4, 0.5, 'open', ?)", (T0 - 9e5, json.dumps(row()["books"])))  # its quotes are no longer kept
    for q in quotes((0.2, 0.45, 100.0, 0.47)) + [(T0 + 399, 0.45, 100.0, 0.57, 100.0, 0.50, 0.52)]:
        db.execute("INSERT INTO quotes (ts, pair, k_yes_ask, k_yes_sz, k_no_ask, k_no_sz, p_yes_bid, p_yes_ask) "
                   "VALUES (?, 'K-1|p-1', ?, ?, ?, ?, ?, ?)", q)
    for o in live_orders(0):
        db.execute("INSERT INTO live_orders (id, ts, mode, venue, market, side, action, qty, limit_price, body, "
                   "filled, rtt_ms, exch_ts, trade) VALUES (?, ?, 'live', ?, 'm', 'yes', ?, ?, ?, '{}', ?, ?, ?, 'L1')",
                   (o["venue"], o["ts"], o["venue"], o["action"], o["qty"], o["limit_price"], o["filled"],
                    o["rtt_ms"], o["exch_ts"]))
    ds = backtest.load(db)
    # The paper trader's decision on the window the live trader took is the same decision.
    assert [(d.src, d.row["id"]) for d in ds] == [("live", "L1"), ("paper", "P2")]
    # The first order's trip counts from the decision, the second's from when it was sent.
    assert backtest.timings(ds) == {"K": [(pytest.approx(0.05), pytest.approx(0.095))],
                                    "P": [(pytest.approx(0.12), pytest.approx(0.18))]}
    before = backtest.replay(ds, BEFORE, 20, random.Random(1))
    assert (before["trades"], before["filled"], before["sold back"]) == (2, 1, 1) and before["fill_rate"] == 0.5
    assert before["order_rate"] == pytest.approx(30 / 50)
    now = backtest.replay(ds, NOW, 20, random.Random(1))
    assert (now["trades"], now["filled"], now["missed"], now["sold back"]) == (2, 1, 1, 0)
    backtest.run(db, draws=5)


def test_a_real_order_that_found_nothing_caps_any_order_at_its_price_or_less():
    # Live: Kalshi filled, Polymarket found nothing at 50c, nor at 51c a moment later (the chase).
    orders = live_orders(0) + [{"ts": T0 + 0.30, "venue": "P", "action": "buy", "qty": 10, "limit_price": 0.51,
                                "filled": 0, "rtt_ms": 150, "exch_ts": T0 + 0.37}]
    r = row(src="live", status="settled", unwind_qty=10.0, profit=0.40)
    r["books"]["seen"]["P"] = [[0.50, 30.0], [0.51, 100.0]]
    d = Decision(r, quotes((0.3, 0.45, 100.0, 0.47)), [], orders)  # the feed saw the bid drop: not a frozen book
    assert not d.frozen and d.room() == pytest.approx(0.03)
    assert d.fill("P", 10, T0 + 0.5, 0.51) == 0 and d.fill("P", 10, T0 + 0.5, 0.50) == 0
    assert d.fill("P", 10, T0 + 0.1) == 10  # before the real orders, only the feed can say


def test_a_pick_sized_down_to_too_little_is_skipped():
    r = row(profit=0.20)
    r["books"]["seen"]["P"] = [[0.50, 2.0], [0.52, 100.0]]  # two left of the ten planned: 4c of profit
    d = Decision(r, quotes(), [], [])
    assert simulate(d, NOW, FAST) == ("skipped", [])
    assert simulate(d, Policy("p", lead="P"), FAST)[0] == "filled"  # unchecked: sent for ten, two filled
