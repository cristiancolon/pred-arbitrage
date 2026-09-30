"""The live trader, against simulated exchanges: real order bodies and replies over a
mocked transport, books that fill and run out, timeouts, 5xx and rejections. Nothing
here reaches a venue."""

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from arbscan import livetrade
from arbscan.config import Config
from arbscan.fees import order_fee
from arbscan.livetrade import Journal, LiveGuard, LiveRun, LiveTrader
from arbscan.orders import KalshiTrading, Order, PMTrading
from arbscan.scanner import KMeta

from test_paper import DAY, PAIR, Harness, kbook, pbook

K_BASE = "https://k.example/trade-api/v2"
P_BASE = "https://p.example"
SIGNER = type("S", (), {"headers": lambda self, m, p: {}})()
COEF = {"K": 0.07, "P": 0.0695}


class Exchange:
    """Kalshi and Polymarket US behind one mock transport, each with books, positions
    and cash. An immediate-or-cancel order fills against the book at its limit or
    better and takes what it fills. ``script[venue]`` makes the next orders there time
    out after filling ("timeout"), fail with a 500 ("500"), or be rejected ("reject")."""

    def __init__(self):
        self.asks: dict[tuple[str, str, str], list] = {}  # (venue, market, side) -> [(price, qty)]
        self.pos: dict[tuple[str, str], float] = defaultdict(float)  # + YES, - NO
        self.cash = {"K": {0: 100.0}, "P": 100.0}
        self.script = {"K": [], "P": []}
        self.sent: list[tuple[str, dict]] = []
        self.fail_reads = False

    def book(self, venue, market, side, levels):
        self.asks[(venue, market, side)] = sorted(levels)

    def _fill(self, venue, market, side, action, qty, limit):
        """Contracts of ``side`` bought at ``limit`` or less, or sold at ``limit`` or more
        (a sale meets the bids: the other side's asks, at 1 - price)."""
        key = (venue, market, side if action == "buy" else ("no" if side == "yes" else "yes"))
        levels, left, rest = [], qty, []
        for p, q in self.asks.get(key, []):
            price = p if action == "buy" else round(1 - p, 4)
            ok = price <= limit + 1e-9 if action == "buy" else price >= limit - 1e-9
            n = min(q, left) if ok else 0
            if n:
                levels.append((price, n))
                left -= n
            if q - n:
                rest.append((p, q - n))
        self.asks[key] = rest
        fee = order_fee(venue, COEF[venue], levels) if levels else 0.0
        n = sum(q for _, q in levels)
        self.pos[(venue, market)] += (1 if side == "yes" else -1) * (1 if action == "buy" else -1) * n
        moved = (-1 if action == "buy" else 1) * sum(p * q for p, q in levels) - fee
        if venue == "K":
            self.cash["K"][0] += moved
        else:
            self.cash["P"] += moved
        return n, (sum(p * q for p, q in levels) / n if n else None), fee

    def handler(self, request: httpx.Request) -> httpx.Response:
        venue = "K" if request.url.host == "k.example" else "P"
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            self.sent.append((venue, body))
            how = self.script[venue].pop(0) if self.script[venue] else "fill"
            if how == "reject":
                return httpx.Response(400, json={"error": {"code": "insufficient_balance"}})
            if how == "500":
                return httpx.Response(500, text="oops")
            reply = self._kalshi_order(body) if venue == "K" else self._pm_order(body)
            if how == "timeout":
                raise httpx.ReadTimeout("slow")  # after the exchange acted on it
            return reply
        if self.fail_reads:
            return httpx.Response(500, text="down")
        if venue == "K" and path.endswith("/portfolio/positions"):
            t = request.url.params.get("ticker")
            rows = [{"ticker": m, "position_fp": f"{v:.2f}"} for (ve, m), v in self.pos.items()
                    if ve == "K" and (t is None or m == t)]
            return httpx.Response(200, json={"market_positions": rows, "cursor": ""})
        if venue == "K" and path.endswith("/portfolio/balance"):
            return httpx.Response(200, json={"balance_breakdown": [{"balance": f"{c:.4f}", "exchange_index": s}
                                                                   for s, c in self.cash["K"].items()]})
        if venue == "K" and path.endswith("/api_keys"):
            lapses = int(time.time() + 5 * DAY)
            return httpx.Response(200, json={"api_keys": [], "api_key_region_expiration_ts": lapses})
        if venue == "P" and path == "/v1/portfolio/positions":
            return httpx.Response(200, json={"positions": {m: {"netPosition": str(v)} for (ve, m), v in self.pos.items()
                                                           if ve == "P"}, "eof": True})
        if venue == "P" and path == "/v1/account/balances":
            return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": self.cash["P"]}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    def _kalshi_order(self, b):
        yes, sell = float(b["price"]), bool(b.get("reduce_only"))
        if b["side"] == "bid":  # buy YES, or sell NO
            side, action, limit = ("no", "sell", 1 - yes) if sell else ("yes", "buy", yes)
        else:
            side, action, limit = ("yes", "sell", yes) if sell else ("no", "buy", 1 - yes)
        n, avg, fee = self._fill("K", b["ticker"], side, action, float(b["count"]), round(limit, 4))
        return httpx.Response(201, json={
            "order_id": f"k{len(self.sent)}", "fill_count": f"{n:.2f}",
            "remaining_count": f"{float(b['count']) - n:.2f}",
            "average_fill_price": None if avg is None else f"{(avg if side == 'yes' else 1 - avg):.4f}",
            "average_fee_paid": f"{fee / n:.4f}" if n else "0", "ts_ms": int(time.time() * 1000)})

    def _pm_order(self, b):
        side = "yes" if b["intent"].endswith("LONG") else "no"
        action = "buy" if "_BUY_" in b["intent"] else "sell"
        yes, qty = float(b["price"]["value"]), float(b["quantity"])
        n, avg, fee = self._fill("P", b["marketSlug"], side, action, qty, round(yes if side == "yes" else 1 - yes, 4))
        done = n >= qty - 1e-9
        stamp = "2026-09-28T12:00:00.100Z"
        execs = []
        if n:
            px = f"{(avg if side == 'yes' else 1 - avg):.4f}"  # quoted in YES
            execs.append({"type": "EXECUTION_TYPE_FILL" if done else "EXECUTION_TYPE_PARTIAL_FILL",
                          "lastShares": str(n), "lastPx": {"value": px},
                          "commissionNotionalCollected": {"value": f"{fee:.2f}"},
                          "transactTime": stamp, "order": {"state": "ORDER_STATE_FILLED" if done else
                                                           "ORDER_STATE_PARTIALLY_FILLED", "cumQuantity": n}})
        if not done:
            execs.append({"type": "EXECUTION_TYPE_CANCELED", "transactTime": stamp,
                          "order": {"state": "ORDER_STATE_CANCELED", "cumQuantity": n}})
        return httpx.Response(200, json={"id": f"p{len(self.sent)}", "executions": execs})


class LiveHarness(Harness):
    """The live trader on the paper test's books, sending its orders to the simulated
    exchange. The exchange's books start out as the scanner sees them."""

    def new_trader(self):
        self.ex = Exchange()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.ex.handler))
        self.fresh = True
        folder = Path(self.cfg.db_path).parent
        self.guard = LiveGuard(self.cfg, self.kmeta, folder / livetrade.HALT_FILE, lambda pair: self.fresh)
        g = self.guard
        g.series, g.shard_cash, g.pm_cash, g.attested_until = {"K"}, {0: 100.0}, 100.0, time.time() + 4 * DAY
        self.journal = Journal(folder / livetrade.JOURNAL_FILE)
        trader = LiveTrader(self.cfg, self.db, self.db, self.lat, self.kbooks, self.pbooks, self.kmeta,
                            KalshiTrading(self.http, K_BASE, SIGNER), PMTrading(self.http, P_BASE, SIGNER),
                            self.guard, self.journal, wake=lambda pair, delay: self.woken.append((pair, delay)))
        trader.resolve_waits = (0.0, 0.0)
        return trader

    def market(self, k_yes=((0.45, 100),), k_no=((0.57, 100),), p_no=((0.50, 100),)):
        """The scanner's books and the exchange's: Kalshi YES at 45c (YES bid 43c), NO on
        Polymarket at 50c."""
        self.kbooks["K-1"] = kbook({round(1 - p, 4): q for p, q in k_yes})
        self.pbooks["p-1"] = pbook([(round(1 - p, 4), q) for p, q in p_no])
        self.ex.book("K", "K-1", "yes", list(k_yes))
        self.ex.book("K", "K-1", "no", list(k_no))
        self.ex.book("P", "p-1", "no", list(p_no))

    def trade(self, window=1.0):
        async def go():
            self.offer(window=window)
            await asyncio.gather(*self.trader.tasks)
        asyncio.run(go())
        return self.trades()[-1] if self.trades() else None

    def trades(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM live_trades ORDER BY ts")]

    def orders(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM live_orders ORDER BY ts")]


def test_both_legs_fill_with_real_orders_written_down_first(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    t = h.trade()
    assert t["status"] == "open" and t["planned_size"] == 10  # $10 a trade
    assert (t["k_qty"], t["p_qty"], t["k_hold"], t["p_hold"]) == (10, 10, 10, 10)
    (kv, kb), (pv, pb) = h.ex.sent
    assert kv == "K" and (kb["side"], kb["price"], kb["count"], kb["time_in_force"]) == \
        ("bid", "0.4500", "10.00", "immediate_or_cancel")
    assert pv == "P" and (pb["intent"], pb["price"]["value"], pb["quantity"]) == ("ORDER_INTENT_BUY_SHORT", "0.5", 10)
    assert h.ex.pos == {("K", "K-1"): 10, ("P", "p-1"): -10}
    # Fees are what the venues charged, and the account's cash moved by what was spent.
    assert t["k_fees"] == pytest.approx(order_fee("K", 0.07, [(0.45, 10)]), abs=1e-3)
    assert t["p_fees"] == pytest.approx(order_fee("P", 0.0695, [(0.50, 10)]))
    assert h.trader.cash["K"] == pytest.approx(100 - t["k_out"]) and t["locked_profit"] > 0  # what Kalshi holds
    orders = h.orders()
    assert [(o["mode"], o["venue"], o["status"], o["trade"]) for o in orders] == \
        [("live", "K", "filled", t["id"]), ("live", "P", "filled", t["id"])]
    events = [json.loads(line)["event"] for line in (tmp_path / livetrade.JOURNAL_FILE).read_text().splitlines()]
    assert events == ["send", "done", "send", "done"] and h.journal.unresolved() == []
    # The account now holds both markets: the same pair isn't traded again until it settles.
    assert h.guard.positions == {"K-1": 10, "p-1": -10} and not h.trader.busy
    h.trade(window=2.0)
    assert len(h.trades()) == 1 and h.trader.stats["account holds a position"] == 1


def test_a_second_leg_that_comes_up_short_is_bought_up_to_break_even(tmp_path):
    h = LiveHarness(tmp_path / "a", paper_lead_venue="K")
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 6), (0.51, 100)])  # the book thinned before the order got there
    t = h.trade()
    assert (t["k_hold"], t["p_hold"], t["status"], t["unwind_qty"]) == (10, 10, "open", 0)
    # 45c + fees on Kalshi leaves room for NO at up to 51.5c after its fee: whole cents, 51c.
    chase = h.ex.sent[2][1]
    assert chase["quantity"] == 4 and float(chase["price"]["value"]) == pytest.approx(1 - 0.51)

    # Past break-even the rest isn't bought: the Kalshi contracts left over are sold back.
    h = LiveHarness(tmp_path / "b", paper_lead_venue="K")
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 6), (0.52, 100)])
    t = h.trade()
    assert (t["k_hold"], t["p_hold"], t["unwind_venue"], t["unwind_qty"]) == (6, 6, "K", 4)
    assert t["status"] == "open" and t["note"] is None and h.guard.halted is None


def test_what_stays_unhedged_is_sold_back_no_lower_than_the_floor(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    h.ex.book("P", "p-1", "no", [])  # the Polymarket NO offer was taken first
    t = h.trade()
    sale = h.ex.sent[-1]
    assert sale[0] == "K" and (sale[1]["side"], sale[1]["price"], sale[1]["reduce_only"]) == ("ask", "0.3500", True)
    assert (t["status"], t["note"], t["unwind_venue"], t["unwind_qty"]) == ("settled", "unwound", "K", 10)
    assert (t["k_hold"], t["p_hold"]) == (0, 0) and h.ex.pos[("K", "K-1")] == 0
    # Bought at 45c, sold at the 43c bid: two cents a contract plus both orders' fees.
    assert t["pnl"] == pytest.approx(-(0.02 * 10 + t["k_fees"]), abs=1e-6)
    assert t["unwind_loss"] == pytest.approx(-t["pnl"], abs=1e-6)
    assert h.guard.lost_today == pytest.approx(-t["pnl"]) and h.guard.halted is None


def test_no_bid_at_the_floor_stops_trading_and_keeps_the_position(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market(k_no=((0.70, 100),))  # YES bid 30c: below 45c - 10c
    h.ex.book("P", "p-1", "no", [])
    t = h.trade()
    assert t["status"] == "open" and (t["k_hold"], t["p_hold"]) == (10, 0)
    assert t["note"].startswith("10 contracts unhedged; stopped: 10 YES contracts left unhedged in K-1 on Kalshi")
    assert "left unhedged" in h.guard.halted and "left unhedged" in (tmp_path / livetrade.HALT_FILE).read_text()
    sent = len(h.ex.sent)
    h.market()
    h.trade(window=2.0)
    assert len(h.ex.sent) == sent and h.trader.stats["halted"] == 1  # nothing more is sent
    (tmp_path / livetrade.HALT_FILE).unlink()  # arbscan live-resume
    h.guard.poll()
    assert h.guard.halted is None


def test_an_order_whose_reply_is_lost_is_read_back_from_the_position(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    h.ex.script["K"] = ["timeout"]
    t = h.trade()
    assert (t["k_hold"], t["p_hold"], t["status"]) == (10, 10, "open")
    k = h.orders()[0]
    assert (k["status"], k["filled"], k["avg_price"]) == ("filled", 10, 0.45)  # priced at its limit
    assert "outcome read from the position" in k["error"] and len(h.ex.sent) == 2  # sent once, never again


def test_an_order_that_cant_be_read_back_stops_trading_at_once(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    h.ex.script["K"] = ["timeout"]
    h.ex.fail_reads = True
    t = h.trade()
    assert len(h.ex.sent) == 1  # no second leg on an unknown first
    assert t["status"] == "open" and "couldn't tell whether an order filled" in t["note"]
    assert h.guard.halted.startswith("couldn't tell") and h.orders()[0]["status"] == "unknown"
    assert [s["market"] for s in h.journal.unresolved()] == ["K-1"]


def test_a_rejected_first_leg_is_a_miss_and_rejections_in_a_row_stop_trading(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="P")
    h.market()
    h.ex.script["P"] = ["reject"]
    t = h.trade()
    assert t["status"] == "missed" and len(h.ex.sent) == 1 and h.trader.rejects == 1
    assert h.guard.halted is None
    h.ex.script["P"] = ["reject", "reject"]
    h.trade(window=2.0)
    h.trade(window=3.0)
    assert h.trader.rejects == 3 and "the last 3 orders were rejected" in h.guard.halted


def test_the_live_limits(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    key = (PAIR.id, "K:YES+P:NO")
    h.guard.busy.add("another pair")
    h.trade(window=1.0)
    assert h.trader.stats["a trade in flight"] == 1 and key not in h.trader.traded and h.woken  # looks again
    h.guard.busy.clear()
    h.fresh = False
    h.trade(window=2.0)
    assert h.trader.stats["feed not fresh"] == 1 and key not in h.trader.traded
    h.fresh = True
    h.guard.trades_today = h.cfg.live_max_trades_per_day
    h.trade(window=3.0)
    assert h.trader.stats["daily trade limit"] == 1 and h.trader.traded[key] == 3.0
    h.guard.trades_today = 0
    h.kmeta["K-1"] = replace(h.kmeta["K-1"], shard=3)
    h.trade(window=4.0)
    assert h.trader.stats["no cash on Kalshi shard 3"] == 1 and not h.ex.sent
    # Sized to the cash on the market's shard, not just the account's $150.
    h.kmeta["K-1"] = replace(h.kmeta["K-1"], shard=0)
    h.guard.shard_cash = {0: 3.0}
    t = h.trade(window=5.0)
    assert t["planned_size"] == 6 and t["k_out"] <= 3.0


def test_settling_counts_toward_todays_losses(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    t = h.trade()
    # The legs settled against each other: both lost.
    h.trader.settle(lambda keys: {("K", "K-1"): 0.0, ("P", "p-1"): 1.0}, {PAIR.id})
    assert h.guard.lost_today == pytest.approx(t["k_out"] + t["p_out"])


def test_the_account_holds_exactly_what_the_venues_hold(tmp_path):
    h = LiveHarness(tmp_path, paper_lead_venue="K")
    h.market()
    t = h.trade()
    # The fills moved the account's cash as they moved the venues' balances (to the
    # cent the fill reports give; the next read of the balances makes it exact).
    assert h.trader.cash["K"] == pytest.approx(h.ex.cash["K"][0], abs=0.01)
    assert h.trader.cash["P"] == pytest.approx(h.ex.cash["P"], abs=0.01)
    # Kalshi's YES won: Kalshi pays $10 for the 10 contracts. The account reads it back
    # at once, and the next trade can spend it.
    h.trader.account_stale = False
    h.trader.settle(lambda keys: {("K", "K-1"): 1.0, ("P", "p-1"): 1.0}, {PAIR.id})
    assert h.trader.account_stale
    h.ex.cash["K"][0] += 10.0
    h.ex.cash["K"][3] = 25.0  # cash on another shard counts too
    asyncio.run(h.guard.read_account(h.trader.venues["K"], h.trader.venues["P"]))
    h.trader.sync_cash()
    assert h.trader.cash == {"K": pytest.approx(h.ex.cash["K"][0] + 25), "P": pytest.approx(h.ex.cash["P"])}
    assert h.trader.cash["K"] == pytest.approx(100 - t["k_out"] + 10 + 25, abs=0.01)


def test_a_restart_after_an_order_with_no_known_outcome_stays_stopped(tmp_path, capsys):
    cfg = Config(db_path=str(tmp_path / "l.db"), live_trading=True)
    from arbscan.store import connect
    db = connect(cfg.db_path)
    Journal(tmp_path / livetrade.JOURNAL_FILE).send("t1", Order("K", "K-1", "yes", "buy", 10, 0.45, client_id="c1"),
                                                     0.0)  # then the process died

    def start():
        return LiveRun(cfg, db, db, None, {}, {}, {"K-1": KMeta("active", time.time() + DAY, 0.07)},
                       KalshiTrading(None, K_BASE, SIGNER), PMTrading(None, P_BASE, SIGNER))

    run = start()
    assert "no known outcome (Kalshi K-1)" in run.guard.halted
    assert not livetrade.resume(cfg, checked=False) and (tmp_path / livetrade.HALT_FILE).exists()
    assert "K-1" in capsys.readouterr().out
    assert livetrade.resume(cfg, checked=True) and not (tmp_path / livetrade.HALT_FILE).exists()
    assert start().guard.halted is None
    livetrade.halt(cfg, "stopped by hand")
    assert start().guard.halted == "stopped by hand"


def test_live_trading_is_off_unless_switched_on(tmp_path):
    from test_feeds import _live

    assert Config().live_trading is False
    sc, _ = _live(tmp_path)
    assert sc.live is None and sc.dry is not None
    asyncio.run(sc.http.aclose())

    sc, _ = _live(tmp_path / "on", live_trading=True)
    assert sc.live is not None and sc.live.trader.table == "live_trades" and sc.live.trader.max_stake == 0.10
    assert not sc._feeds_fresh(PAIR)  # the feeds never connected
    asyncio.run(sc.http.aclose())


def test_live_trading_runs_with_paper_trading_and_the_dry_run_off(tmp_path):
    from arbscan.feeds import KalshiBook
    from test_feeds import _live, _pm

    sc, _ = _live(tmp_path, live_trading=True, paper_trading=False, dry_run=False)
    assert sc.paper is None and sc.dry is None and sc.traders == [sc.live.trader]
    seen = []
    sc.live.trader.consider = lambda *args: seen.append(args[1])
    kb = sc.kfeed.books.setdefault("K-1", KalshiBook())
    kb.snapshot({"yes_dollars_fp": [["0.3500", "100"]], "no_dollars_fp": [["0.4000", "20"]]}, time.time())
    sc._on_kalshi("K-1")
    _pm(sc, bids=[(0.50, 15)], offers=[(0.52, 50)])
    assert seen == ["K:YES+P:NO"]  # the pick still reaches the live trader
    asyncio.run(sc.http.aclose())
