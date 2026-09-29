"""The dry run: a paper trader under a capped live run's limits, writing out the real
orders it would send. Nothing here reaches a venue."""

import asyncio
import json
import time

import httpx
import pytest

from arbscan import dryrun
from arbscan.dryrun import DryOrders, Guard, clean_series
from arbscan.orders import Order, PMTrading
from arbscan.paper import Fill
from arbscan.scanner import KMeta
from arbscan.store import connect

from test_paper import DAY, PAIR, Harness, kbook, pbook


def test_only_series_with_a_clean_settlement_record_pass():
    rows = [("KXNFLREC", 462, 0, 6), ("KXATPGSPREAD", 51, 2, 0), ("KXCS2GAME", 344, 0, 21),
            ("KXPRESCUP", 3, 0, 0), ("KXNCAAFSPREAD", 571, 0, 0)]
    assert clean_series(rows, 20, 0.02) == {"KXNFLREC", "KXNCAAFSPREAD"}


def ready_guard(cfg, kmeta):
    g = Guard(cfg, kmeta)
    g.series, g.shard_cash, g.attested_until = {"K"}, {0: 150.0}, time.time() + 4 * DAY
    return g


def test_the_guard_passes_nothing_until_it_has_read_the_records_and_the_account(tmp_path):
    h = Harness(tmp_path)
    g = Guard(h.cfg, h.kmeta)
    assert g(PAIR, 0.5) == "series without a clean record"
    g = ready_guard(h.cfg, h.kmeta)
    assert g(PAIR, 0.5) is None
    for change, why in ((lambda g: setattr(g, "held", {"p-1"}), "account holds a position"),
                        (lambda g: setattr(g, "lost_today", g.cfg.live_daily_loss_usd), "daily loss limit"),
                        (lambda g: setattr(g, "attested_until", time.time() + 3600), "Kalshi location check lapsing"),
                        (lambda g: setattr(g, "shard_cash", {0: 0.5, 3: 100.0}), "no cash on Kalshi shard 0"),
                        (lambda g: setattr(g, "series", {"KXNFLREC"}), "series without a clean record")):
        g = ready_guard(h.cfg, h.kmeta)
        change(g)
        assert g(PAIR, 0.5) == why
    h.kmeta["K-1"] = KMeta("active", time.time() + DAY / 2, 0.07, shard=3)  # a baseball market, say
    g = ready_guard(h.cfg, h.kmeta)
    assert g(PAIR, 0.5) == "no cash on Kalshi shard 3"


class DryHarness(Harness):
    """The dry-run trader on the paper test's books, recording its orders in the test DB."""

    def new_trader(self):
        self.orders = DryOrders(self.db, pm=None)
        self.guard = ready_guard(self.cfg, self.kmeta)
        from dataclasses import replace
        cfg = replace(self.cfg, bankroll_usd=self.cfg.live_bankroll_usd,
                      paper_min_profit_usd=self.cfg.live_min_profit_usd)
        from arbscan.paper import PaperTrader
        return PaperTrader(cfg, self.db, self.db, self.lat, self.kbooks, self.pbooks, self.kmeta,
                           wake=lambda pair, delay: self.woken.append((pair, delay)), name="dry",
                           guard=self.guard, max_stake=self.cfg.live_max_stake_usd, orders=self.orders)


def test_the_dry_run_trades_within_its_stake_and_writes_the_orders_it_would_send(tmp_path):
    h = DryHarness(tmp_path)
    h.kbooks["K-1"] = kbook({0.55: 100})  # YES asks 45c x 100
    h.pbooks["p-1"] = pbook([(0.50, 100)])  # NO asks 50c x 100

    async def run():
        h.offer()
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM dry_trades").fetchone())
    assert h.db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0  # its own table
    assert t["planned_size"] == 10 and t["k_out"] + t["p_out"] <= 10.0  # $10 a trade, not the 100 on offer
    orders = {r["venue"]: dict(r) for r in h.db.execute("SELECT * FROM live_orders")}
    assert set(orders) == {"K", "P"} and all(o["mode"] == "dry" and o["trade"] == t["id"] for o in orders.values())
    k, p = json.loads(orders["K"]["body"]), json.loads(orders["P"]["body"])
    assert (k["side"], k["price"], k["count"], k["time_in_force"]) == ("bid", "0.4500", "10.00", "immediate_or_cancel")
    # NO on Polymarket at 50c is written as its YES price.
    assert (p["intent"], p["price"]["value"], p["quantity"]) == ("ORDER_INTENT_BUY_SHORT", "0.5", 10)
    assert orders["P"]["status"] == "filled" and orders["P"]["avg_price"] == pytest.approx(0.50)


def test_a_pick_the_guard_refuses_is_not_traded(tmp_path):
    h = DryHarness(tmp_path)
    h.guard.held = {"K-1"}
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])
    h.offer()
    assert not h.trader.tasks and h.trader.stats["account holds a position"] == 1
    assert h.db.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0


def test_a_sale_is_written_as_a_reduce_only_sell_at_what_it_fetched(tmp_path):
    db = connect(str(tmp_path / "d.db"))
    rec = DryOrders(db, pm=None)
    # Paper sells 7 YES back by buying 7 NO at 60c: the order a live trader sends is a sale of YES.
    rec.leg("t1", Order("K", "K-1", "yes", "sell", 7, 0.01, reduce_only=True), Fill([(0.60, 7.0)], 0.12), sold=True)
    r = dict(db.execute("SELECT * FROM live_orders").fetchone())
    body = json.loads(r["body"])
    assert (r["action"], r["side"], r["avg_price"], r["status"]) == ("sell", "yes", pytest.approx(0.40), "filled")
    assert (body["side"], body["price"], body["reduce_only"]) == ("ask", "0.0100", True)


def test_polymarket_previews_the_buys_and_the_verdict_is_kept(tmp_path):
    db = connect(str(tmp_path / "d.db"))
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["request"]["intent"])
        return httpx.Response(200, json={"order": {}}) if len(seen) == 1 else httpx.Response(400, text="tick size")

    async def main():
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        pm = PMTrading(c, "https://p.example", type("S", (), {"headers": lambda self, m, p: {}})())
        rec = DryOrders(db, pm)
        stop = asyncio.Event()
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0)
        rec.leg("t1", Order("P", "p-1", "no", "buy", 5, 0.50, client_id="a"), Fill([(0.50, 5.0)], 0.1))
        rec.leg("t1", Order("P", "p-1", "yes", "buy", 5, 0.20, client_id="b"), Fill([], 0.0))
        sale = Order("P", "p-1", "no", "sell", 5, 0.01, client_id="c", reduce_only=True)
        rec.leg("t1", sale, Fill([(0.5, 5.0)], 0.1), sold=True)
        for _ in range(100):
            if rec.previews["ok"] + rec.previews["refused"] == 2:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await task
        return rec

    rec = asyncio.run(main())
    assert seen == ["ORDER_INTENT_BUY_SHORT", "ORDER_INTENT_BUY_LONG"]  # a sale needs the position it sells
    got = dict(db.execute("SELECT id, preview FROM live_orders").fetchall())
    assert got["a"] == "ok" and "tick size" in got["b"] and got["c"] is None
    assert rec.previews == {"ok": 1, "refused": 1}


def test_the_records_come_from_settled_pairs_and_todays_trades(tmp_path):
    h = DryHarness(tmp_path)
    db = h.db
    for i in range(25):  # 25 settled NFL pairs, all one bet
        db.execute("INSERT INTO decisions VALUES (?, ?, 'same', 0, 'jev', NULL)", (f"KXNFLREC-X{i}", f"p{i}"))
        db.execute("INSERT INTO results (venue, id, yes_value, checked_ts, first_final_ts) VALUES ('K', ?, 1, 0, 0)",
                   (f"KXNFLREC-X{i}",))
        db.execute("INSERT INTO results (venue, id, yes_value, checked_ts, first_final_ts) VALUES ('P', ?, 1, 0, 0)",
                   (f"p{i}",))
    db.execute("INSERT INTO dry_trades (id, ts, pair, direction, k_side, p_side, status, settled_ts, pnl) "
               "VALUES ('x', 0, 'K-1|p-1', 'K:YES+P:NO', 'yes', 'no', 'settled', ?, -1.25)", (time.time(),))
    db.commit()
    g = Guard(h.cfg, h.kmeta)
    g.read_records(h.cfg.db_path, "dry_trades")
    assert g.series == {"KXNFLREC"} and g.lost_today == pytest.approx(1.25)


def test_the_dry_run_starts_with_the_scanner_when_both_keys_are_set(tmp_path):
    from test_feeds import _live
    sc, _ = _live(tmp_path)
    assert sc.dry is not None and sc.dry.trader.table == "dry_trades" and sc.dry.trader.max_stake == 10.0
    assert sc.dry.trader.cfg.bankroll_usd == 100.0 and sc.dry.trader.cfg.paper_min_profit_usd == 0.05
    asyncio.run(sc.http.aclose())


def test_the_account_reads_find_positions_shard_cash_and_the_location_check(tmp_path):
    h = Harness(tmp_path)
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path, dict(request.url.params)))
        path = request.url.path
        if request.url.host == "p.example" and path == "/v1/portfolio/positions":
            return httpx.Response(200, json={"positions": {"aec-nfl-x": {"netPositionDecimal": "-3"},
                                                           "aec-nfl-y": {"netPositionDecimal": "0"}}, "eof": True})
        if request.url.host == "p.example" and path == "/v1/account/balances":
            return httpx.Response(200, json={"balances": [{"currency": "USD", "buyingPower": 142.50}]})
        if path.endswith("/portfolio/positions"):
            return httpx.Response(200, json={"market_positions": [{"ticker": "KXTEST-26-A", "position_fp": "37.5"},
                                                                  {"ticker": "KXOLD-1", "position_fp": "0.00"}],
                                             "cursor": ""})
        if path.endswith("/portfolio/balance"):
            return httpx.Response(200, json={"balance": 15106, "balance_breakdown": [
                {"balance": "120.5000", "exchange_index": 0}, {"balance": "0.0000", "exchange_index": 3}]})
        if path.endswith("/api_keys"):
            return httpx.Response(200, json={"api_keys": [], "api_key_region_expiration_ts": 1790933187})
        raise AssertionError(f"unexpected {request.method} {path}")

    async def main():
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        signer = type("S", (), {"headers": lambda self, m, p: {}})()
        g = Guard(h.cfg, h.kmeta)
        await g.read_account(dryrun.KalshiTrading(c, "https://k.example/trade-api/v2", signer),
                             PMTrading(c, "https://p.example", signer))
        return g

    g = asyncio.run(main())
    assert g.held == {"KXTEST-26-A", "aec-nfl-x"} and g.positions == {"KXTEST-26-A": 37.5, "aec-nfl-x": -3}
    assert g.shard_cash == {0: pytest.approx(120.5000), 3: 0.0} and g.attested_until == 1790933187
    assert g.pm_cash == pytest.approx(142.50)
    assert all(method == "GET" for method, _, _ in requests)  # reads only: no order endpoint is touched
