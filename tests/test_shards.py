import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from arbscan.orders import KalshiTrading
from arbscan.scanner import KMeta
from arbscan.shards import Rebalancer, plan, targets
from arbscan.store import connect

from test_livetrade import K_BASE, SIGNER, Exchange


def test_each_shard_in_use_keeps_a_full_trade_and_the_rest_follows_demand():
    t = targets(100.0, {0, 3}, {0: 10.0, 3: 90.0}, floor=20.0)
    assert sum(t.values()) == pytest.approx(100.0)
    assert t[0] >= 20.0 and t[3] > 2 * t[0]
    assert targets(30.0, {0, 3}, {3: 50.0}, floor=20.0)[0] == pytest.approx(15.0)  # too little: an even share
    assert targets(100.0, {0, 3}, {}, floor=20.0) == {0: 50.0, 3: 50.0}  # no demand yet: even
    assert targets(100.0, set(), {0: 5.0}, floor=20.0) == {}


def test_money_moves_only_once_a_shard_drifts_well_below_target():
    want = {0: 40.0, 3: 60.0}
    assert plan({0: 38.0, 3: 62.0}, want) == []  # close enough
    assert plan({0: 20.0, 3: 80.0}, want) == [(3, 0, 20.0)]
    assert plan({0: 38.0, 3: 62.0}, want, short={0}) == [(3, 0, 2.0)]  # it just ran short: any gap counts
    assert plan({0: 40.0, 1: 5.0, 3: 55.0}, want) == [(1, 3, 5.0)]  # an unused shard is emptied
    moves = plan({0: 0.0, 1: 0.004, 3: 100.0}, {0: 33.333, 3: 66.667})
    assert moves == [(3, 0, 33.33)]  # whole cents, and dust stays put


def _rebalancer(tmp_path, cash, trades=()):
    ex = Exchange()
    ex.cash["K"] = dict(cash)
    http = httpx.AsyncClient(transport=httpx.MockTransport(ex.handler))
    kmeta = {"KXATP-1": KMeta("active", time.time() + 86400, 0.07, 3), "KXNFL-1": KMeta("active", time.time() + 86400, 0.07, 0),
             "KXOLD-1": KMeta("finalized", 0, 0.07, 1)}
    guard = SimpleNamespace(shard_cash=dict(cash), series={"KXATP", "KXNFL"}, epoch=0)
    trader = SimpleNamespace(busy=set(), table="live_trades", account_stale=False, stake_cap=lambda: 20.0)
    db = connect(str(tmp_path / "s.db"))
    for i, (ticker, spent) in enumerate(trades):
        db.execute("INSERT INTO live_trades (id, ts, pair, direction, k_side, p_side, planned_size, k_out, status) "
                   "VALUES (?, ?, ?, 'K:YES+P:NO', 'yes', 'no', 1, ?, 'settled')", (str(i), time.time() - 60,
                                                                                    f"{ticker}|p", spent))
    db.commit()
    return Rebalancer(KalshiTrading(http, K_BASE, SIGNER), guard, trader, kmeta), ex, db


def test_the_rebalancer_moves_cash_to_where_trades_are(tmp_path):
    rb, ex, db = _rebalancer(tmp_path, {0: 90.0, 1: 5.0, 2: 0.0, 3: 5.0},
                             trades=[("KXATP-OLD", 30.0), ("KXNFL-1", 10.0)])  # a finished market, by its series
    done = asyncio.run(rb.step(db))
    assert done and sum(a for _, _, a in done) == pytest.approx(sum(a for _, _, a in ex.transfers))
    assert ex.cash["K"][1] == pytest.approx(0.0)  # shard 1 has no market in use
    assert ex.cash["K"][3] > ex.cash["K"][0] >= 20.0  # shard 3 had three times the demand
    assert rb.guard.shard_cash == pytest.approx(ex.cash["K"]) and rb.trader.account_stale
    assert sum(ex.allocation.values()) == 100 and ex.allocation[3] > ex.allocation[0]
    assert rb.snapshot()["moves"][0]["amount"] > 0
    assert asyncio.run(rb.step(db)) == []  # not again so soon


def test_a_shard_that_ran_short_is_topped_up_at_once(tmp_path):
    rb, ex, db = _rebalancer(tmp_path, {0: 45.0, 3: 55.0})
    assert asyncio.run(rb.step(db)) == []  # even, and no demand yet
    rb.ran_short(3, 12.0)
    done = asyncio.run(rb.step(db))  # without waiting out the interval
    assert done and done[0][:2] == (0, 3)


def test_nothing_moves_during_a_trade_and_a_failed_transfer_backs_off(tmp_path):
    rb, ex, db = _rebalancer(tmp_path, {0: 100.0, 3: 0.0})
    rb.trader.busy.add("K-1|p-1")
    assert asyncio.run(rb.step(db)) == [] and ex.transfers == []
    rb.trader.busy.clear()
    ex.script["transfer"].append("insufficient_balance")
    assert asyncio.run(rb.step(db)) == []
    assert rb.guard.shard_cash == {0: 100.0, 3: 0.0} and rb.trader.account_stale  # read the truth back
    rb.ran_short(3, 5.0)
    assert asyncio.run(rb.step(db)) == []  # backing off
