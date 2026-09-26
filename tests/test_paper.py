"""Paper trading: fills at simulated order arrival, fees, unequal fills, settlement."""

import asyncio
import json
import random
import time

import pytest

from arbscan import paper
from arbscan.config import Config
from arbscan.feeds import KalshiBook, PMBook
from arbscan.fees import order_fee, per_contract
from arbscan.latency import PM_PREVIEW_PATH, LatencyModel, LatencyProbe
from arbscan.pairs import Pair
from arbscan.scanner import KMeta
from arbscan.store import connect

PAIR = Pair("K-1", "p-1", "same")
DAY = 86400


def kbook(no_bids: dict[float, float], yes_bids: dict[float, float] | None = None) -> KalshiBook:
    """A Kalshi book: YES asks are the complement of the NO bids, and vice versa."""
    b = KalshiBook()
    b.snapshot({"yes_dollars_fp": [[str(p), str(q)] for p, q in (yes_bids or {}).items()],
                "no_dollars_fp": [[str(p), str(q)] for p, q in no_bids.items()]}, time.time())
    return b


def pbook(bids: list[tuple[float, float]], offers=()) -> PMBook:
    b = PMBook()
    b.update({"bids": [{"px": {"value": str(p)}, "qty": str(q)} for p, q in bids],
              "offers": [{"px": {"value": str(p)}, "qty": str(q)} for p, q in offers],
              "state": "MARKET_STATUS_OPEN"}, time.time())
    b.state = "MARKET_STATE_OPEN"
    return b


def test_ioc_fill_respects_limit_budget_and_hidden_liquidity():
    ladder = [(0.40, 10), (0.41, 10), (0.45, 10)]
    assert paper.take(ladder, 25, 0.41, 0.07, 1e9) == [(0.40, 10.0), (0.41, 10.0)]  # limit
    assert paper.take(ladder, 25, 0.45, 0.07, 1e9, {0.40: 8}) == [(0.40, 2.0), (0.41, 10.0), (0.45, 10.0)]
    unit = 0.40 + per_contract(0.07, 0.40)
    assert paper.take(ladder, 25, 0.45, 0.07, 5 * unit + 0.001) == [(0.40, 5.0)]  # cash runs out
    assert paper.take([(0.40, 2.6)], 5, 0.5, 0.07, 1e9) == [(0.40, 2.0)]  # whole contracts only


def test_fees_round_up_per_order():
    assert order_fee("K", 0.07, [(0.5, 1)]) == pytest.approx(0.0175)
    assert order_fee("P", 0.0695, [(0.5, 1)]) == pytest.approx(0.02)  # 1.74c -> 2c
    assert order_fee("P", 0.0695, [(0.5, 4)]) == pytest.approx(0.07)  # 6.95c -> 7c


def test_lasting_liquidity_is_what_every_version_offered():
    steady = [(0.45, 100)]
    flicker = [(0.44, 5), (0.45, 100)]  # a better level that came and went
    thinner = [(0.45, 30), (0.46, 200)]
    assert paper.lasting([steady]) == steady
    assert paper.lasting([steady, flicker, steady]) == [(0.45, 100)]
    assert paper.lasting([steady, thinner]) == [(0.45, 30), (0.46, 70)]  # shrank to 30; 100 at <= 46c throughout
    assert paper.lasting([steady, []]) == []


def test_breakeven_price():
    p = paper.breakeven_price(0.07, 0.55)
    assert p + per_contract(0.07, p) == pytest.approx(0.55)
    assert paper.breakeven_price(0.0, 0.3) == pytest.approx(0.3)


class Harness:
    """A paper trader on in-memory books with a fixed, known latency."""

    def __init__(self, tmp_path, **cfg):
        cfg.setdefault("paper_lead_venue", "")  # both legs at once unless a test says otherwise
        cfg.setdefault("pick_min_window_s", 0.0)  # trade at first sight unless a test says otherwise
        self.cfg = Config(db_path=str(tmp_path / "p.db"), bankroll_usd=cfg.pop("bankroll", 300), **cfg)
        self.db = connect(self.cfg.db_path)
        self.lat = LatencyModel(lambda v: [0.02] if v == "K" else [0.05], random.Random(1))
        self.lat.rtt["K"].extend([0.04])
        self.lat.rtt["P"].extend([0.06])
        self.kbooks, self.pbooks = {}, {}
        self.kmeta = {"K-1": KMeta("active", time.time() + DAY / 2, 0.07)}
        self.woken = []  # (pair, delay) re-checks the trader asked the scanner for
        self.trader = self.new_trader()

    def new_trader(self):
        return paper.PaperTrader(self.cfg, self.db, self.db, self.lat, self.kbooks, self.pbooks, self.kmeta,
                                 wake=lambda pair, delay: self.woken.append((pair, delay)))

    def offer(self, days=0.5, window=1.0):
        """YES on Kalshi at 45c and NO on Polymarket at 50c (YES bid 50c): ~1.5c after fees."""
        kl, _ = self.kbooks["K-1"].ladders()
        pl = self.pbooks["p-1"].no_asks
        self.trader.consider(PAIR, "K:YES+P:NO", "yes", "no", kl, pl, 0.07, 0.0695, days, window=window,
                             seen_ts=time.time())


def test_both_legs_fill_and_settle(tmp_path):
    h = Harness(tmp_path)
    h.kbooks["K-1"] = kbook({0.55: 100})  # YES asks 45c x 100
    h.pbooks["p-1"] = pbook([(0.50, 100)])  # NO asks 50c x 100

    async def run():
        h.offer()
        assert h.trader.busy == {PAIR.id}
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = next(iter(h.trader.open.values()))
    assert t["k_qty"] == t["p_qty"] == t["planned_size"] == 100
    assert t["k_delay_ms"] == pytest.approx(20 + 20, abs=1)  # feed lag + half the round trip
    assert t["p_delay_ms"] == pytest.approx(50 + 30, abs=1)
    assert t["k_fees"] == pytest.approx(order_fee("K", 0.07, [(0.45, 100)]))
    assert t["locked_profit"] == pytest.approx(100 - 45 - 50 - t["k_fees"] - t["p_fees"])
    assert h.trader.cash["K"] == pytest.approx(150 - t["k_out"])

    results = {("K", "K-1"): 0.0, ("P", "p-1"): 0.0}  # both markets settled NO
    lookup = lambda keys: {k: results[k] for k in keys if k in results}  # noqa: E731
    assert h.trader.settle(lambda keys: {}, {PAIR.id}) == 0  # no results yet
    assert h.trader.settle(lookup, {PAIR.id}) == 1
    row = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    # Kalshi YES lost, Polymarket NO won: $100 lands on Polymarket.
    assert (row["status"], row["payout_k"], row["payout_p"]) == ("settled", 0.0, 100.0)
    assert row["pnl"] == pytest.approx(t["locked_profit"])
    assert h.trader.cash["P"] == pytest.approx(150 - t["p_out"] + 100)

    # A restart rebuilds the account from the table.
    again = h.new_trader()
    assert again.cash == pytest.approx(h.trader.cash) and not again.open


def test_liquidity_gone_by_arrival_is_chased_then_unwound(tmp_path):
    h = Harness(tmp_path)
    h.kbooks["K-1"] = kbook({0.55: 100, 0.40: 500}, yes_bids={0.40: 500})  # YES 45c x 100, then 60c; NO 60c
    h.pbooks["p-1"] = pbook([(0.50, 100), (0.30, 500)])  # NO 50c x 100, then 70c

    async def run():
        h.offer()
        await asyncio.sleep(0.06)  # Kalshi filled (~40 ms); Polymarket's order is still on its way (~80 ms)
        h.pbooks["p-1"].update({"bids": [{"px": {"value": "0.50"}, "qty": "30"}, {"px": {"value": "0.30"},
                                "qty": "500"}], "state": "MARKET_STATE_OPEN"}, time.time())
        h.trader.reserved["K"] += 1000  # no free cash on Kalshi: selling back what we hold needs none
        await asyncio.gather(*h.trader.tasks)
        h.trader.reserved["K"] -= 1000

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    # 30 filled on Polymarket; the other 70 would cost 70c there, past break-even,
    # so the 70 extra Kalshi YES were sold back (bought NO at 60c): ~8c lost on each.
    assert (t["k_qty"], t["p_qty"], t["k_hold"], t["p_hold"]) == (100, 30, 30, 30)
    assert (t["unwind_venue"], t["unwind_qty"]) == ("K", 70)
    entry_fee = order_fee("K", 0.07, [(0.45, 100)]) / 100
    assert t["unwind_loss"] == pytest.approx(70 * (0.45 + entry_fee + 0.60 - 1) + order_fee("K", 0.07, [(0.60, 70)]))
    assert t["status"] == "open"
    assert t["locked_profit"] < t["planned_profit"]
    books = json.loads(t["books"])  # what it saw when deciding, and what each order met on arrival
    assert books["seen"]["P"][0] == [0.5, 100.0] and books["met"]["P"][0] == [0.5, 30.0]


def test_polymarket_first_turns_a_stale_quote_into_a_miss(tmp_path):
    h = Harness(tmp_path, paper_lead_venue="P")
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        # Polymarket's quote is gone before our order lands (~80 ms): the Kalshi leg is never sent.
        h.pbooks["p-1"].update({"bids": [{"px": {"value": "0.46"}, "qty": "100"}], "state": "MARKET_STATE_OPEN"},
                               time.time())
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    assert (t["status"], t["k_qty"], t["p_qty"], t["k_delay_ms"]) == ("missed", 0, 0, None)
    assert h.trader.cash == {"K": 150, "P": 150}


def test_polymarket_first_sizes_kalshi_to_what_filled(tmp_path):
    h = Harness(tmp_path, paper_lead_venue="P")
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        h.pbooks["p-1"].update({"bids": [{"px": {"value": "0.50"}, "qty": "30"}], "state": "MARKET_STATE_OPEN"},
                               time.time())
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    assert (t["k_qty"], t["p_qty"], t["unwind_qty"]) == (30, 30, 0)
    # Kalshi's order goes out when Polymarket's report is back: 60 ms round trip + 40 ms to Kalshi's book.
    assert t["p_delay_ms"] == pytest.approx(80, abs=5) and t["k_delay_ms"] == pytest.approx(120, abs=10)


def test_nothing_filled_is_a_miss_and_frees_the_cash(tmp_path):
    h = Harness(tmp_path)
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        h.kbooks["K-1"].snapshot({"yes_dollars_fp": [], "no_dollars_fp": []}, time.time())  # all pulled
        h.pbooks["p-1"].update({"bids": [], "state": "MARKET_STATE_OPEN"}, time.time())
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    assert h.db.execute("SELECT status FROM paper_trades").fetchone()[0] == "missed"
    assert h.trader.cash == {"K": 150, "P": 150} and h.trader.reserved == pytest.approx({"K": 0, "P": 0})


def test_only_picks_are_traded_once_per_window(tmp_path):
    h = Harness(tmp_path)
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer(days=30, window=0.5)  # resolves too late to be a pick
        assert not h.trader.tasks
        h.offer()
        await asyncio.gather(*h.trader.tasks)
        h.offer()  # same window again
        assert not h.trader.tasks

    asyncio.run(run())
    assert h.db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


def test_a_window_is_traded_only_once_it_has_stayed_open(tmp_path):
    h = Harness(tmp_path, pick_min_window_s=0.2)
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        assert not h.trader.tasks  # just opened
        assert [(p, round(d, 2)) for p, d in h.woken] == [(PAIR, 0.21)]  # asked to look again when it's old enough
        h.offer()
        assert len(h.woken) == 1  # once per window
        await asyncio.sleep(0.21)
        h.offer()  # the re-check: still open
        assert h.trader.tasks
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    assert (t["k_qty"], t["p_qty"]) == (100, 100)


def test_a_level_that_came_and_went_is_not_counted(tmp_path):
    h = Harness(tmp_path, pick_min_window_s=0.1)
    h.kbooks["K-1"] = kbook({0.55: 20})  # YES 45c x 20
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        h.kbooks["K-1"].snapshot({"yes_dollars_fp": [], "no_dollars_fp": [["0.55", "20"], ["0.56", "80"]]},
                                 time.time())  # 80 more at 44c for a moment
        h.offer()
        h.kbooks["K-1"].snapshot({"yes_dollars_fp": [], "no_dollars_fp": [["0.55", "20"]]}, time.time())
        h.offer()
        await asyncio.sleep(0.11)
        h.kbooks["K-1"].snapshot({"yes_dollars_fp": [], "no_dollars_fp": [["0.55", "20"], ["0.56", "80"]]},
                                 time.time())  # back just as we look again
        h.offer()
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    assert (t["planned_size"], t["k_limit"]) == (20, 0.45)  # only the 20 that stayed the whole time
    assert (t["k_qty"], t["p_qty"], t["unwind_qty"]) == (20, 20, 0)


def test_a_new_window_starts_the_wait_again(tmp_path):
    h = Harness(tmp_path, pick_min_window_s=0.1)
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        await asyncio.sleep(0.11)
        h.offer(window=2.0)  # it closed and reopened
        assert not h.trader.tasks and len(h.woken) == 2

    asyncio.run(run())


def test_a_window_not_worth_trading_on_what_stayed_is_looked_at_again(tmp_path):
    h = Harness(tmp_path, pick_min_window_s=0.1)
    h.kbooks["K-1"] = kbook({0.55: 100})
    h.pbooks["p-1"] = pbook([(0.50, 100)])

    async def run():
        h.offer()
        h.pbooks["p-1"].update({"bids": [], "state": "MARKET_STATE_OPEN"}, time.time())  # pulled for a moment
        h.trader.watch[(PAIR.id, "K:YES+P:NO")].add(time.time(), h.kbooks["K-1"].ladders()[0], [], 0.1)
        h.pbooks["p-1"].update({"bids": [{"px": {"value": "0.50"}, "qty": "100"}], "state": "MARKET_STATE_OPEN"},
                               time.time())
        await asyncio.sleep(0.11)
        h.offer()  # old enough, but Polymarket's side didn't stay the whole time
        assert not h.trader.tasks
        assert round(h.woken[-1][1], 2) == 0.26  # look again once that has aged out
        h.offer()
        assert len(h.woken) == 2  # not before then
        await asyncio.sleep(0.26)
        h.offer()
        await asyncio.gather(*h.trader.tasks)

    asyncio.run(run())
    t = dict(h.db.execute("SELECT * FROM paper_trades").fetchone())
    assert (t["k_qty"], t["p_qty"]) == (100, 100)


def test_bankroll_change_moves_cash(tmp_path):
    h = Harness(tmp_path)
    h.cfg = Config(db_path=h.cfg.db_path, bankroll_usd=400)
    t = h.new_trader()
    assert t.cash == {"K": 200, "P": 200}
    assert json.loads(h.db.execute("SELECT value FROM settings WHERE key = 'paper_deposits'").fetchone()[0]) == \
        {"K": 200, "P": 200}


def test_probe_never_touches_the_order_endpoint():
    calls = []

    class Signer:
        def headers(self, method, path):
            calls.append((method, path))
            return {}

    probe = LatencyProbe(LatencyModel(), "https://k.example/trade-api/v2", Signer(), "https://p.example", Signer(),
                         lambda: "some-slug")
    assert probe.pm_url == "https://p.example" + PM_PREVIEW_PATH == "https://p.example/v1/order/preview"
    assert probe.kalshi_url.endswith("/portfolio/balance")
    assert "orders" not in probe.pm_url and "orders" not in probe.kalshi_url
