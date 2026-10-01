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
from arbscan.confirm import BookCheck
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
        self.script = {"K": [], "P": [], "transfer": []}
        self.transfers: list[tuple[int, int, float]] = []
        self.allocation: dict[int, int] | None = None
        self.sent: list[tuple[str, dict]] = []
        self.fail_reads = False
        self.book_reads: list[str] = []
        self.on_order = None  # called with (venue, body) as each order arrives
        self.book_time = "2026-09-28T12:00:00Z"  # when the Polymarket books last changed

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
        if request.method == "POST" and path.endswith("/portfolio/intra_exchange_instance_transfer"):
            b = json.loads(request.content)
            if self.script["transfer"]:
                return httpx.Response(400, json={"error": self.script["transfer"].pop(0)})
            amt = b["amount"] / 10000
            assert self.cash["K"].get(b["source_exchange_shard"], 0.0) >= amt - 1e-9, "more than the shard holds"
            self.cash["K"][b["source_exchange_shard"]] -= amt
            self.cash["K"][b["destination_exchange_shard"]] = self.cash["K"].get(b["destination_exchange_shard"], 0.0) + amt
            self.transfers.append((b["source_exchange_shard"], b["destination_exchange_shard"], amt))
            return httpx.Response(200, json={"transfer_id": "t1", "status": "complete"})
        if request.method == "POST" and path.endswith("/portfolio/target_balance_allocation"):
            self.allocation = {a["exchange_index"]: a["percent"] for a in json.loads(request.content)["allocations"]}
            return httpx.Response(200, json={})
        if request.method == "POST":
            body = json.loads(request.content)
            self.sent.append((venue, body))
            if self.on_order is not None:
                self.on_order(venue, body)
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
        if venue == "P" and path.startswith("/v1/markets/") and path.endswith("/book"):
            slug = path.split("/")[3]
            assert request.url.params.get("_"), "a unique query string, or the edge serves a cached book"
            self.book_reads.append(slug)
            return httpx.Response(200, json={"marketData": {
                "marketSlug": slug, "state": "MARKET_STATE_OPEN", "transactTime": self.book_time,
                "offers": [{"px": {"value": str(p)}, "qty": str(q)} for p, q in self.asks.get(("P", slug, "yes"), [])],
                "bids": [{"px": {"value": str(round(1 - p, 4))}, "qty": str(q)}
                         for p, q in self.asks.get(("P", slug, "no"), [])]}})
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

    checked = False
    window = 1.0

    def __init__(self, tmp_path, checked=False, **cfg):
        self.checked = checked
        super().__init__(tmp_path, **cfg)

    def new_trader(self):
        self.ex = Exchange()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.ex.handler))
        self.fresh = True
        folder = Path(self.cfg.db_path).parent
        self.guard = LiveGuard(self.cfg, self.kmeta, folder / livetrade.HALT_FILE, lambda pair: self.fresh)
        g = self.guard
        g.series, g.shard_cash, g.pm_cash, g.attested_until = {"K"}, {0: 100.0}, 100.0, time.time() + 4 * DAY
        self.journal = Journal(folder / livetrade.JOURNAL_FILE)
        pm = PMTrading(self.http, P_BASE, SIGNER)
        # ``checked``: with the Polymarket book checked against the exchange before a trade.
        self.check = BookCheck(pm.book, self.pbooks, lambda slug: self.offer(window=self.window)) \
            if self.checked else None
        trader = LiveTrader(self.cfg, self.db, self.db, self.lat, self.kbooks, self.pbooks, self.kmeta,
                            KalshiTrading(self.http, K_BASE, SIGNER), pm,
                            self.guard, self.journal, wake=lambda pair, delay: self.woken.append((pair, delay)),
                            check=self.check)
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
        self.window = window

        async def go():
            self.offer(window=window)
            if self.check is not None:  # the book's read comes back, and the pair is priced again
                await asyncio.gather(*self.check.tasks)
            await asyncio.gather(*self.trader.tasks)
        asyncio.run(go())
        return self.trades()[-1] if self.trades() else None

    def trades(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM live_trades ORDER BY ts")]

    def orders(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM live_orders ORDER BY ts")]


def test_both_legs_fill_with_real_orders_written_down_first(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="K")
    h.market()
    t = h.trade()
    assert t["status"] == "open" and t["planned_size"] == 10  # $10 a trade
    assert (t["k_qty"], t["p_qty"], t["k_hold"], t["p_hold"]) == (10, 10, 10, 10)
    (kv, kb), (pv, pb) = h.ex.sent
    assert kv == "K" and (kb["side"], kb["price"], kb["count"], kb["time_in_force"]) == \
        ("bid", "0.4500", "10.00", "immediate_or_cancel")
    # The second leg offers up to break-even (NO at 51c, quoted as YES at 49c) and fills at the book's 50c.
    assert pv == "P" and (pb["intent"], pb["price"]["value"], pb["quantity"]) == ("ORDER_INTENT_BUY_SHORT", "0.49", 10)
    assert t["p_out"] - t["p_fees"] == pytest.approx(10 * 0.50)
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
    h = LiveHarness(tmp_path / "a", live_lead_venue="K")
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 6), (0.51, 100)])  # the book thinned before the order got there
    t = h.trade()
    assert (t["k_hold"], t["p_hold"], t["status"], t["unwind_qty"]) == (10, 10, "open", 0)
    # 45c + fees on Kalshi leaves room for NO at up to 51.5c after its fee: whole cents, 51c.
    # One order, offering that much: it takes the 6 at 50c and 4 at 51c.
    assert len(h.ex.sent) == 2
    second = h.ex.sent[1][1]
    assert second["quantity"] == 10 and float(second["price"]["value"]) == pytest.approx(1 - 0.51)
    assert t["p_out"] - t["p_fees"] == pytest.approx(6 * 0.50 + 4 * 0.51) and t["locked_profit"] >= 0

    # Past break-even the rest isn't bought: the Kalshi contracts left over are sold back,
    # without a second try at the price the order just offered.
    h = LiveHarness(tmp_path / "b", live_lead_venue="K")
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 6), (0.52, 100)])
    t = h.trade()
    assert (t["k_hold"], t["p_hold"], t["unwind_venue"], t["unwind_qty"]) == (6, 6, "K", 4)
    assert t["status"] == "open" and t["note"] is None and h.guard.halted is None
    assert [(v, b.get("reduce_only", False)) for v, b in h.ex.sent] == [("K", False), ("P", False), ("K", True)]


def test_what_stays_unhedged_is_sold_back_no_lower_than_the_floor(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="K")
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
    h = LiveHarness(tmp_path, live_lead_venue="K")
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
    h = LiveHarness(tmp_path, live_lead_venue="K")
    h.market()
    h.ex.script["K"] = ["timeout"]
    t = h.trade()
    assert (t["k_hold"], t["p_hold"], t["status"]) == (10, 10, "open")
    k = h.orders()[0]
    assert (k["status"], k["filled"], k["avg_price"]) == ("filled", 10, 0.45)  # priced at its limit
    assert "outcome read from the position" in k["error"] and len(h.ex.sent) == 2  # sent once, never again


def test_an_order_that_cant_be_read_back_stops_trading_at_once(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="K")
    h.market()
    h.ex.script["K"] = ["timeout"]
    h.ex.fail_reads = True
    t = h.trade()
    assert len(h.ex.sent) == 1  # no second leg on an unknown first
    assert t["status"] == "open" and "couldn't tell whether an order filled" in t["note"]
    assert h.guard.halted.startswith("couldn't tell") and h.orders()[0]["status"] == "unknown"
    assert [s["market"] for s in h.journal.unresolved()] == ["K-1"]


def test_a_rejected_first_leg_is_a_miss_and_rejections_in_a_row_stop_trading(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="P")
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
    h = LiveHarness(tmp_path, live_lead_venue="K")
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
    h = LiveHarness(tmp_path, live_lead_venue="K")
    h.market()
    t = h.trade()
    # The legs settled against each other: both lost.
    h.trader.settle(lambda keys: {("K", "K-1"): 0.0, ("P", "p-1"): 1.0}, {PAIR.id})
    assert h.guard.lost_today == pytest.approx(t["k_out"] + t["p_out"])


def test_the_account_holds_exactly_what_the_venues_hold(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="K")
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


def test_a_trade_sized_down_to_its_shards_cash_asks_for_more_there(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="K")
    h.market()
    asked = []
    h.guard.on_short = lambda shard, dollars: asked.append((shard, dollars))
    h.guard.shard_cash = {0: 3.0, 3: 97.0}  # the pair's market is on shard 0
    t = h.trade()
    assert t["planned_size"] < 10 and asked and asked[0][0] == 0 and asked[0][1] > 5

    h2 = LiveHarness(tmp_path / "plenty", live_lead_venue="K")
    h2.market()
    h2.guard.on_short = lambda shard, dollars: asked.append((shard, dollars))
    asked.clear()
    assert h2.trade()["planned_size"] == 10 and asked == []  # enough there: nothing asked


def test_a_leftover_leg_sold_back_counts_toward_todays_losses_at_once(tmp_path):
    h = LiveHarness(tmp_path, live_lead_venue="K")
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 4)])  # Polymarket fills 4 of the 10 Kalshi bought
    t = h.trade()
    assert (t["status"], t["k_hold"], t["p_hold"], t["unwind_qty"]) == ("open", 4, 4, 6)
    assert t["unwind_loss"] > 0 and h.guard.lost_today == pytest.approx(t["unwind_loss"])  # not at settlement
    # A restart counts it again from the record.
    assert h.new_trader().guard.lost_today == pytest.approx(t["unwind_loss"])
    # Settling as one bet counts the rest of the trade, so the day ends at the trade's P&L.
    h2 = LiveHarness(tmp_path / "settle", live_lead_venue="K")
    h2.market()
    h2.ex.book("P", "p-1", "no", [(0.50, 4)])
    t2 = h2.trade()
    h2.trader.settle(lambda keys: {("K", "K-1"): 1.0, ("P", "p-1"): 1.0}, {PAIR.id})  # one bet: YES won
    pnl = h2.trades()[-1]["pnl"]
    assert h2.guard.pnl_today == pytest.approx(pnl) and pnl == pytest.approx(t2["locked_profit"])


# --- filling both legs ------------------------------------------------------------------

def test_polymarket_goes_first_and_kalshi_follows_for_what_it_filled(tmp_path):
    h = LiveHarness(tmp_path)
    assert h.cfg.live_lead_venue == "P"
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 6)])  # someone took 4 of the 10 the book showed
    t = h.trade()
    assert [v for v, _ in h.ex.sent] == ["P", "K"] and h.ex.sent[1][1]["count"] == "6.00"
    assert (t["status"], t["k_hold"], t["p_hold"], t["unwind_qty"]) == ("open", 6, 6, 0)  # smaller, nothing sold back
    assert t["locked_profit"] > 0 and h.guard.lost_today == 0


def test_a_first_leg_that_finds_nothing_costs_nothing_and_rests_the_market(tmp_path):
    h = LiveHarness(tmp_path)
    h.market()
    h.ex.book("P", "p-1", "no", [])  # the offer the scanner still shows is gone
    t = h.trade()
    assert (t["status"], t["note"], t["pnl"]) == ("missed", "no fill", 0) and [v for v, _ in h.ex.sent] == ["P"]
    assert h.guard.lost_today == 0 and h.guard.halted is None and h.ex.pos == {("P", "p-1"): 0}
    # Its book showed what wasn't there: no second order into it for a minute.
    h.market()
    h.trade(window=2.0)
    assert len(h.ex.sent) == 1 and h.trader.stats["an order there just missed"] == 1
    # The pick isn't written off: once the minute is up it is taken, if it's still there.
    key = (PAIR.id, "K:YES+P:NO")
    assert h.trader.traded.get(key) != 2.0 and h.woken
    h.guard.cooling["p-1"] = time.time() - 1  # a minute on
    h.trader.watch[key].due = 0.0
    t = h.trade(window=2.0)
    assert t["status"] == "open" and t["k_hold"] == t["p_hold"] == t["planned_size"]


def test_the_kalshi_leg_pays_up_to_break_even_rather_than_sell_polymarket_back(tmp_path):
    h = LiveHarness(tmp_path)
    h.market()
    h.ex.book("K", "K-1", "yes", [(0.46, 100)])  # Kalshi's 45c went while Polymarket's order was out
    t = h.trade()
    # NO at 50c plus its fee leaves room for YES at up to 46.5c after Kalshi's fee: whole cents, 46c.
    assert h.ex.sent[1][1]["price"] == "0.4600" and len(h.ex.sent) == 2
    assert (t["status"], t["k_hold"], t["p_hold"], t["unwind_qty"]) == ("open", 10, 10, 0)
    assert 0 <= t["locked_profit"] < 0.05

    # Past break-even it isn't bought, and the Polymarket contracts are sold back.
    h = LiveHarness(tmp_path / "b")
    h.market()
    h.ex.book("K", "K-1", "yes", [(0.47, 100)])
    h.ex.book("P", "p-1", "yes", [(0.52, 100)])  # NO bid 48c
    t = h.trade()
    assert h.ex.sent[1][1]["price"] == "0.4600"
    assert [(v, b.get("reduce_only", b.get("intent"))) for v, b in h.ex.sent][-1] == ("P", "ORDER_INTENT_SELL_SHORT")
    assert (t["status"], t["note"], t["unwind_venue"], t["unwind_qty"]) == ("settled", "unwound", "P", 10)
    assert len(h.ex.sent) == 3  # no second try at Kalshi at the price just offered


def moved_market(tmp_path, k_ask, p_yes_ask):
    """Polymarket's NO bought at 50c; by the time Kalshi's order lands, its YES is at
    ``k_ask`` and Polymarket's YES is offered at ``p_yes_ask`` (so NO is bid 1 - that)."""
    h = LiveHarness(tmp_path)
    h.market()

    def arrive(venue, body):
        if venue == "K" and len(h.ex.sent) == 2:  # the books our feeds show catch up with the exchange
            h.kbooks["K-1"] = kbook({round(1 - k_ask, 4): 100})
            h.pbooks["p-1"] = pbook([(0.40, 100)], [(p_yes_ask, 100)])
    h.ex.on_order = arrive
    h.ex.book("K", "K-1", "yes", [(k_ask, 100)])
    h.ex.book("P", "p-1", "yes", [(p_yes_ask, 100)])
    return h, h.trade()


def test_what_is_left_unhedged_is_got_out_of_the_cheaper_way(tmp_path):
    # Kalshi's YES went to 48c, two cents past break-even; Polymarket's NO is bid 44c.
    # Finishing the pair loses about 1.5c a contract, selling back about 9c: it finishes.
    h, t = moved_market(tmp_path / "a", 0.48, 0.56)
    assert [(v, b.get("price")) for v, b in h.ex.sent if v == "K"] == [("K", "0.4600"), ("K", "0.4800")]
    assert (t["status"], t["k_hold"], t["p_hold"], t["unwind_qty"]) == ("open", 10, 10, 0)
    assert -0.20 < t["locked_profit"] < -0.10 and h.guard.halted is None
    # That loss is certain now, and counts toward today's at once; settling adds nothing to it.
    assert h.guard.lost_today == pytest.approx(-t["locked_profit"])
    assert h.new_trader().guard.lost_today == pytest.approx(-t["locked_profit"])  # and after a restart
    h.trader.settle(lambda keys: {("K", "K-1"): 1.0, ("P", "p-1"): 1.0}, {PAIR.id})
    assert h.guard.lost_today == pytest.approx(-t["locked_profit"])

    # Kalshi's YES at 51c against a NO bid of 50c: selling back is the cheaper way out.
    h, t = moved_market(tmp_path / "b", 0.51, 0.50)
    assert [v for v, _ in h.ex.sent] == ["P", "K", "P"] and (t["note"], t["unwind_venue"]) == ("unwound", "P")
    assert t["pnl"] == pytest.approx(-(t["p_fees"]), abs=1e-6)  # bought and sold at 50c: the two fees

    # Kalshi's YES at 60c: finishing would lose more than the 10c a leftover contract may; sold back.
    h, t = moved_market(tmp_path / "c", 0.60, 0.56)
    assert [v for v, _ in h.ex.sent] == ["P", "K", "P"] and t["unwind_qty"] == 10
    assert t["pnl"] == pytest.approx(-(10 * 0.06 + t["p_fees"]), abs=1e-6)

    # No bid for the Polymarket contracts within the floor: finishing is the only way out.
    h, t = moved_market(tmp_path / "d", 0.50, 0.70)
    assert [v for v, _ in h.ex.sent] == ["P", "K", "K"] and (t["k_hold"], t["p_hold"]) == (10, 10)
    assert h.guard.halted is None


def test_the_second_legs_cap_stays_within_the_venues_cash(tmp_path):
    h = LiveHarness(tmp_path)
    from arbscan.livetrade import Fills
    lead = Fills(bought=10, cost=5.0, fees=0.18)
    assert h.trader._cap("K", "K-1", 0.07, 10, 0.45, lead) == pytest.approx(0.46)
    h.guard.shard_cash = {0: 4.78}  # 10 at 46c and their fee won't fit, 10 at 45c will
    assert h.trader._cap("K", "K-1", 0.07, 10, 0.45, lead) == pytest.approx(0.45)
    assert h.trader._cap("K", "K-1", 0.07, 10, 0.47, lead) == pytest.approx(0.47)  # never under the planned limit


def test_both_prices_must_have_settled(tmp_path):
    h = LiveHarness(tmp_path, paper_quiet_s=2.0, live_settle_s=3.0)
    h.market()
    now = time.time()
    h.kbooks["K-1"].ladders()
    h.kbooks["K-1"].top_ts = [now - 2.2] * 2
    h.pbooks["p-1"].top_ts = [now - 2.6] * 2  # both moved in the last three seconds: still repricing
    assert h.trade() is None and not h.ex.sent
    assert 0.3 < h.woken[-1][1] <= 0.42  # looks again once the older one has sat three seconds
    h.pbooks["p-1"].top_ts = [now - 30] * 2  # one side has sat a while; the other for the quiet time
    t = h.trade(window=2.0)
    assert t["status"] == "open" and json.loads(t["books"])["steady"]["P"] >= 3.0


# --- the Polymarket book, checked against the exchange ------------------------------------

def test_a_trade_waits_for_the_polymarket_book_to_be_checked(tmp_path):
    h = LiveHarness(tmp_path, checked=True)
    h.market()
    h.pbooks["p-1"].exch_ts = 1.0

    async def go():
        h.offer()
        assert not h.trader.tasks and not h.ex.sent  # nothing goes out on a book that hasn't been read
        assert h.trader.stats["confirming the book"] == 1 and h.check.pending == {"p-1"}
        await asyncio.gather(*h.check.tasks)  # the read is back: the pair is priced again, and trades
        await asyncio.gather(*h.trader.tasks)
    asyncio.run(go())
    t = h.trades()[-1]
    assert h.ex.book_reads == ["p-1"] and (t["status"], t["k_hold"], t["p_hold"]) == ("open", 10, 10)
    assert h.check.snapshot()["reads"] == 1 and h.check.frozen == 0


def test_a_frozen_polymarket_book_is_repaired_and_nothing_is_sent(tmp_path):
    h = LiveHarness(tmp_path, checked=True)
    h.market()
    book = h.pbooks["p-1"]
    book.exch_ts = book.recv_ts = time.time() - 180  # nothing streamed for three minutes
    h.ex.book("P", "p-1", "no", [(0.56, 100)])  # meanwhile the real book moved: NO is 56c now, not 50c
    h.ex.book_time = "2099-01-01T00:00:00Z"
    repriced = []
    h.check.on_checked = repriced.append

    async def go():
        h.offer()
        await asyncio.gather(*h.check.tasks)
    asyncio.run(go())
    assert not h.ex.sent and not h.trades() and repriced == ["p-1"]
    assert book.no_asks == [(0.56, 100.0)] and h.check.frozen == 1 and h.check.last_frozen["slug"] == "p-1"
    assert h.check.last_frozen["silent_s"] == pytest.approx(180, abs=1)
    # The streamed update that was on its way, older than the exchange's book, doesn't undo it.
    book.update({"bids": [{"px": {"value": "0.50"}, "qty": "100"}], "transactTime": "2026-09-28T12:00:00Z"},
                time.time())
    assert book.no_asks == [(0.56, 100.0)]
    book.update({"bids": [{"px": {"value": "0.45"}, "qty": "7"}], "transactTime": "2099-01-01T00:00:01Z"},
                time.time())
    assert book.no_asks == [(0.55, 7.0)]  # a newer one does


def test_the_book_check_is_asked_for_ahead_and_not_twice(tmp_path):
    h = LiveHarness(tmp_path, checked=True, paper_quiet_s=0.6)
    h.market()
    h.pbooks["p-1"].exch_ts = 1.0
    h.kbooks["K-1"].ladders()
    h.pbooks["p-1"].top_ts = [time.time() - 60] * 2

    async def go():
        h.offer()  # Kalshi's price is new: 0.6 s to wait, and the read goes out 0.4 s before that's up
        assert "p-1" in h.check.timers and not h.ex.book_reads
        h.offer()
        assert len(h.check.timers) == 1
        await asyncio.sleep(0.3)
        await asyncio.gather(*h.check.tasks)
        assert h.ex.book_reads == ["p-1"] and h.check.ok("p-1") and not h.ex.sent  # read, but not yet due
        h.check.ask("p-1")
        assert not h.check.pending  # read a moment ago: not again
        await asyncio.sleep(0.35)
        h.offer()  # due: the book is already checked, so the orders go at once
        assert h.trader.tasks
        await asyncio.gather(*h.trader.tasks)
    asyncio.run(go())
    assert h.ex.book_reads == ["p-1"] and len(h.ex.sent) == 2 and h.trader.stats["confirming the book"] == 0


def test_no_book_check_for_a_pair_that_cant_be_traded_anyway(tmp_path):
    h = LiveHarness(tmp_path, checked=True, paper_quiet_s=0.1)
    h.market()
    h.kbooks["K-1"].ladders()
    h.guard.series = set()  # its series has no clean record

    async def go():
        h.offer()
        await asyncio.sleep(0.15)
        h.offer()
    asyncio.run(go())
    assert not h.check.timers and not h.ex.book_reads and h.trader.stats["series without a clean record"] == 1


def test_a_check_that_fails_or_goes_stale_vouches_for_nothing(tmp_path):
    h = LiveHarness(tmp_path, checked=True)
    h.market()
    h.pbooks["p-1"].exch_ts = 1.0
    h.ex.fail_reads = True
    assert h.trade() is None and h.check.errors == 1 and not h.check.ok("p-1") and not h.ex.sent
    h.check.ask("p-1")
    assert not h.check.pending  # nor is the failed read repeated at once
    h.check.checked["p-1"] = time.time() - 5  # read five seconds ago
    assert not h.check.ok("p-1")
    h.check.checked["p-1"] = time.time()
    assert h.check.ok("p-1")
    h.check.forget("p-1")
    assert not h.check.ok("p-1")


def test_the_scanner_closes_a_window_that_stood_on_a_frozen_book(tmp_path):
    from arbscan.feeds import KalshiBook
    from test_feeds import _live, _pm

    sc, _ = _live(tmp_path, live_trading=True, paper_trading=False, dry_run=False)
    check = sc.live.check
    assert sc.live.trader.check is check and sc.live.snapshot()["execution"]["lead"] == "P"
    kb = sc.kfeed.books.setdefault("K-1", KalshiBook())
    kb.snapshot({"yes_dollars_fp": [["0.3500", "100"]], "no_dollars_fp": [["0.4000", "20"]]}, time.time())
    sc._on_kalshi("K-1")
    _pm(sc, bids=[(0.50, 15)], offers=[(0.52, 50)])  # YES 40c on Kalshi, NO 50c on Polymarket: a window
    key = ("K-1|p-1", "K:YES+P:NO")
    assert key in sc.episodes.open
    book = sc.pfeed.books["p-1"]
    book.exch_ts = book.recv_ts = time.time() - 120  # the stream said nothing more for two minutes

    async def read(slug):  # the exchange's book: the bid went to 30c long ago, so NO costs 70c
        return {"marketSlug": slug, "state": "MARKET_STATE_OPEN", "transactTime": "2099-01-01T00:00:00Z",
                "bids": [{"px": {"value": "0.30"}, "qty": "15"}], "offers": [{"px": {"value": "0.52"}, "qty": "50"}]}
    check.read = read

    async def go():
        check.ask("p-1")
        await asyncio.gather(*check.tasks)
        await sc.http.aclose()
    asyncio.run(go())
    assert check.frozen == 1 and key not in sc.episodes.open  # repaired, priced again, and the window is gone
    assert sc.pair_state["K-1|p-1"]["edges"]["K:YES+P:NO"] < 0
    assert sc.live.snapshot()["execution"]["book_check"]["frozen"] == 1


def test_a_window_that_isnt_worth_a_trade_isnt_read_over_and_over(tmp_path):
    h = LiveHarness(tmp_path, checked=True, live_min_profit_usd=5.0)  # the window's few cents never make a pick
    h.market()

    async def go():
        for _ in range(4):
            h.offer()
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)  # its re-check comes round
        h.offer()
    asyncio.run(go())
    assert not h.ex.book_reads and not h.check.timers and not h.check.pending and not h.ex.sent
    assert h.trader.stats["confirming the book"] == 0


def test_a_second_leg_that_never_reached_the_book_is_tried_again(tmp_path):
    h = LiveHarness(tmp_path)
    h.market()
    h.ex.script["K"] = ["reject"]  # Kalshi turns the order away (a 429, say): its book was never asked
    t = h.trade()
    assert [v for v, _ in h.ex.sent] == ["P", "K", "K"] and h.orders()[1]["status"] == "rejected"
    assert (t["status"], t["k_hold"], t["p_hold"], t["unwind_qty"]) == ("open", 10, 10, 0)  # not sold back

    # The same when its reply was lost and the position shows nothing arrived.
    h = LiveHarness(tmp_path / "b")
    h.market()
    seen = []

    def lose(venue, body):  # the first Kalshi order is dropped on the way: no fill, no reply
        if venue == "K" and not seen:
            seen.append(1)
            raise httpx.ReadTimeout("lost")
    h.ex.on_order = lose
    t = h.trade()
    assert [v for v, _ in h.ex.sent] == ["P", "K", "K"]
    assert (t["k_hold"], t["p_hold"], t["unwind_qty"]) == (10, 10, 0)


def test_part_of_a_contract_left_over_is_held_and_said(tmp_path):
    h = LiveHarness(tmp_path)
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 3.4)])  # Polymarket fills parts of contracts
    t = h.trade()
    assert h.ex.sent[1][1]["count"] == "3.00"  # Kalshi's leg is whole contracts
    assert (t["status"], t["k_hold"], t["p_hold"]) == ("open", 3, pytest.approx(3.4))
    assert t["note"] == "0.4 contracts unhedged" and len(h.ex.sent) == 2 and h.guard.halted is None

    h = LiveHarness(tmp_path / "b")
    h.market()
    h.ex.book("P", "p-1", "no", [(0.50, 0.4)])
    t = h.trade()
    # Less than a contract and nothing against it: still a position, kept open until it settles.
    assert (t["status"], t["k_hold"], t["p_hold"], t["note"]) == ("open", 0, pytest.approx(0.4), "0.4 contracts unhedged")
    assert len(h.ex.sent) == 1 and h.trader.open


def test_waiting_on_a_check_asks_once_not_on_every_update(tmp_path):
    h = LiveHarness(tmp_path, checked=True)
    h.market()
    h.pbooks["p-1"].exch_ts = 1.0
    h.check.on_checked = None  # the answer comes, but nothing prices the pair again yet

    async def go():
        h.offer()
        await asyncio.gather(*h.check.tasks)
        assert h.ex.book_reads == ["p-1"]
        h.check.checked["p-1"] = time.time() - 5  # ...until the answer has gone stale
        for _ in range(5):
            h.offer()  # updates arriving before the re-check is due: none of them asks again
        assert h.ex.book_reads == ["p-1"] and not h.check.pending and not h.ex.sent
    asyncio.run(go())


def test_an_answer_that_cant_vouch_for_the_book_is_an_error(tmp_path):
    h = LiveHarness(tmp_path, checked=True)
    h.market()
    h.pbooks["p-1"].exch_ts = 1.0

    async def read(slug):
        return {"marketSlug": slug, "bids": [], "offers": []}  # no transactTime
    h.check.read = read
    assert h.trade() is None and h.check.errors == 1 and not h.check.ok("p-1")
    h.check.ask("p-1")
    assert not h.check.pending  # and it isn't asked again at once

    async def hang(slug):
        await asyncio.sleep(60)
    h.check.read = hang
    h.check.asked.clear()
    from arbscan import confirm
    old, confirm.READ_TIMEOUT_S = confirm.READ_TIMEOUT_S, 0.05
    try:
        assert h.trade(window=2.0) is None and h.check.errors == 2 and not h.check.pending
    finally:
        confirm.READ_TIMEOUT_S = old
