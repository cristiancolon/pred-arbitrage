"""Your own money moves (moves.py): what each venue's ledger entries did to its cash, and
reading the ledgers again and again without counting anything twice. Nothing here
reaches a venue."""

import asyncio
import json
import random
import time
from datetime import datetime, timezone

import httpx
import pytest

from arbscan import livetrade, moves
from arbscan.config import Config
from arbscan.latency import LatencyModel
from arbscan.livetrade import Journal, LiveRun
from arbscan.moves import Move, Moves, kalshi_cash, kalshi_markets, pm_cash, pm_markets
from arbscan.orders import KalshiTrading, Order, PMTrading
from arbscan.scanner import KMeta
from arbscan.store import connect

from test_livetrade import K_BASE, P_BASE, SIGNER, Exchange

DAY = 86400
SINCE = 1_790_000_000.0


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(moves, "PAUSE_S", 0.0)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def kfill(fid, ticker, ts, n, yes, book_side, fee=0.0):
    # Kalshi's other side/action fields don't say whether a fill opened or closed a position.
    return {"fill_id": fid, "ticker": ticker, "ts": int(ts), "count_fp": f"{n:.2f}", "yes_price_dollars": f"{yes:.4f}",
            "fee_cost": f"{fee:.4f}", "book_side": book_side, "action": "buy" if book_side == "bid" else "sell",
            "side": "yes" if book_side == "bid" else "no"}


def ksettle(ticker, ts, yes_count=0.0, no_count=0.0, revenue_cents=0):
    return {"ticker": ticker, "settled_time": iso(ts), "yes_count_fp": f"{yes_count:.2f}",
            "no_count_fp": f"{no_count:.2f}", "revenue": revenue_cents, "market_result": "yes"}


def kmoney(mid, ts, cents, fee_cents=0, status="applied", kind="debit"):
    return {"id": mid, "amount_cents": cents, "fee_cents": fee_cents, "created_ts": int(ts), "finalized_ts": int(ts),
            "status": status, "type": kind}


def pcash(kind, tid, ts, amount, status="COMPLETED"):
    return {"type": f"ACTIVITY_TYPE_{kind}", "accountBalanceChange": {
        "transactionId": tid, "status": f"ACCOUNT_BALANCE_CHANGE_STATUS_{status}", "amount": {"value": str(amount)},
        "createTime": iso(ts), "updateTime": iso(ts + 60), "description": kind.lower()}}


def ptrade(tid, slug, ts, intent, shares, yes_px, fee, aggressor=True, state="TRADE_STATE_NEW"):
    mine = {"order": {"intent": intent}, "lastShares": f"{shares:.4f}", "lastPx": {"value": f"{yes_px:.4f}"},
            "commissionNotionalCollected": {"value": f"{fee:.4f}"}}
    theirs = {"order": {"intent": "ORDER_INTENT_UNDEFINED"}, "lastShares": f"{shares:.4f}",
              "lastPx": {"value": f"{yes_px:.4f}"}, "commissionNotionalCollected": {"value": "-0.0100"}}
    return {"type": moves.PM_TRADE, "trade": {
        "id": tid, "marketSlug": slug, "state": state, "createTime": iso(ts), "isAggressor": aggressor,
        "aggressorExecution": mine if aggressor else theirs, "passiveExecution": theirs if aggressor else mine}}


def presolve(slug, ts, cash_value):
    return {"type": moves.PM_RESOLUTION, "positionResolution": {
        "marketSlug": slug, "updateTime": iso(ts), "beforePosition": {"cashValue": {"value": str(cash_value)}},
        "afterPosition": {"cashValue": {"value": "0"}}}}


def amounts(ms):
    return {m.id: pytest.approx(m.amount, abs=1e-9) for m in ms}


NOT_OURS = lambda market: False  # noqa: E731


# --- what each entry moved -------------------------------------------------------------

def test_kalshi_deposits_arrive_less_their_fee_and_withdrawals_take_the_whole_amount():
    got = kalshi_cash(
        [kmoney("a", SINCE + 10, 10204, 204), kmoney("b", SINCE + 20, 25000, 0, kind="ach"),
         kmoney("c", SINCE + 30, 5000, 100, status="pending"), kmoney("d", SINCE + 40, 3000, 60, status="failed"),
         kmoney("old", SINCE - 10, 9900, 0)],
        [kmoney("w", SINCE + 50, 4210, 200), kmoney("x", SINCE + 60, 1000, 0, status="returned")], SINCE)
    assert amounts(got) == {"K:deposit:a": 100.00, "K:deposit:b": 250.0, "K:deposit:c": 0.0, "K:deposit:d": 0.0,
                            "K:withdrawal:w": -42.10, "K:withdrawal:x": 0.0}  # nothing from before live trading
    assert {m.kind for m in got} == {"deposit", "withdrawal"} and all(m.venue == "K" for m in got)


def test_a_kalshi_fill_moves_cash_by_the_position_it_changed():
    t0 = SINCE + 100
    got = kalshi_markets([
        # Held 100 YES, sold 40 at 92c: 40 x 92c in, less the fee.
        kfill("sale", "K-YES", t0, 40, 0.92, "ask", fee=0.20),
        # Held nothing, then met the YES bid: that's buying NO, at 1 - 30c.
        kfill("no", "K-NO", t0, 10, 0.30, "ask", fee=0.10),
        # Held 10 NO, bought 10 YES at 25c: each pair pays out $1, so 75c a contract in.
        kfill("close", "K-CLOSE", t0, 10, 0.25, "bid"),
        # Held 5 NO, bought 10 YES at 40c: 5 close (+60c each), 5 open (-40c each).
        kfill("cross", "K-CROSS", t0, 10, 0.40, "bid"),
    ], [], {"K-YES": 60, "K-NO": -10, "K-CROSS": 5}, NOT_OURS)
    assert amounts(got) == {"K:fill:sale": 40 * 0.92 - 0.20, "K:fill:no": -10 * 0.70 - 0.10,
                            "K:fill:close": 10 * 0.75, "K:fill:cross": 5 * 0.60 - 5 * 0.40}
    assert {m.kind for m in got} == {"trade"} and got[0].market == "K-YES"


def test_a_kalshi_settlement_pays_its_revenue_and_restores_the_position_held_before_it():
    t0 = SINCE + 100
    got = kalshi_markets(
        [kfill("buy", "K-S", t0, 20, 0.30, "bid", fee=0.05),
         kfill("same-second", "K-T", t0 + 50, 5, 0.40, "bid")],
        [ksettle("K-S", t0 + 50, yes_count=20, revenue_cents=2000),
         ksettle("K-T", t0 + 50, yes_count=5, revenue_cents=500)],
        {}, NOT_OURS)  # both settled, so nothing is held now
    assert amounts(got) == {"K:fill:buy": -20 * 0.30 - 0.05, "K:settlement:K-S": 20.0,
                            "K:fill:same-second": -5 * 0.40, "K:settlement:K-T": 5.0}
    assert {m.kind for m in got if m.id.startswith("K:settlement")} == {"payout"}


def test_kalshi_fills_and_settlements_in_the_traders_markets_are_trading():
    got = kalshi_markets([kfill("f", "K-1", SINCE + 1, 10, 0.45, "bid"), kfill("g", "K-OWN", SINCE + 1, 1, 0.5, "bid")],
                         [ksettle("K-1", SINCE + 9, yes_count=10, revenue_cents=1000)], {"K-OWN": 1},
                         lambda m: m == "K-1")
    assert [m.id for m in got] == ["K:fill:g"]


def test_a_kalshi_fill_without_a_book_side_is_left_out():
    f = kfill("odd", "K-OWN", SINCE + 1, 1, 0.5, "bid")
    del f["book_side"]
    assert kalshi_markets([f], [], {"K-OWN": 1}, NOT_OURS) == []


def test_walking_kalshi_fills_back_from_the_position_now_recovers_each_fills_cash():
    """Against a simulated Kalshi account that nets YES against NO, over random trading."""
    for seed in range(200):
        rng = random.Random(seed)
        pos, fills, want = 0.0, [], {}
        for i in range(rng.randint(1, 12)):
            n, yes, fee = rng.randint(1, 30), rng.randint(1, 99) / 100, rng.randint(0, 50) / 100
            bid = rng.random() < 0.5
            if bid:  # buy YES: closes NO first
                closed = min(n, max(0.0, -pos))
                cash, pos = closed * (1 - yes) - (n - closed) * yes, pos + n
            else:  # sell YES: closes YES first, else buys NO
                closed = min(n, max(0.0, pos))
                cash, pos = closed * yes - (n - closed) * (1 - yes), pos - n
            fills.append(kfill(f"f{i}", "K-R", SINCE + i, n, yes, "bid" if bid else "ask", fee))
            want[f"K:fill:f{i}"] = cash - fee
        rng.shuffle(fills)  # the ledger's order doesn't matter
        assert amounts(kalshi_markets(fills, [], {"K-R": pos}, NOT_OURS)) == \
            {k: pytest.approx(v, abs=1e-9) for k, v in want.items()}, seed


def test_polymarket_deposits_withdrawals_and_bonuses():
    t0 = SINCE + 100
    got = pm_cash([
        pcash("ACCOUNT_DEPOSIT", "card", t0, 100, "PENDING"),  # spendable at once, though still pending
        pcash("ACCOUNT_DEPOSIT", "done", t0 + 10, 40),
        pcash("ACCOUNT_DEPOSIT", "bounced", t0 + 20, 30, "REJECTED"),
        pcash("ACCOUNT_WITHDRAWAL", "out", t0 + 30, 25),
        pcash("ACCOUNT_WITHDRAWAL", "refused", t0 + 40, 15, "REJECTED"),
        pcash("REFERRAL_BONUS", "bonus", t0 + 50, 5),
        pcash("TAKER_FEE_REBATE", "rebate", t0 + 60, 0.5),  # earned by trading
        pcash("LIQUIDITY_PROGRAM", "lp", t0 + 70, 0.25),  # earned by trading
        pcash("TRANSFER", "transfer", t0 + 80, 7),  # which way? not said
        pcash("ACCOUNT_DEPOSIT", "old", SINCE - 50, 150),  # before live trading started
    ], SINCE)
    assert amounts(got) == {"P:card": 100.0, "P:done": 40.0, "P:bounced": 0.0, "P:out": -25.0, "P:refused": 0.0,
                            "P:bonus": 5.0}
    assert {m.id: m.kind for m in got}["P:bonus"] == "bonus"


def test_a_polymarket_advance_counts_until_its_deposit_clears():
    t0 = SINCE + 100
    pending = [pcash("ACCOUNT_DEPOSIT", "ach", t0, 500, "PENDING"), pcash("ACCOUNT_ADVANCED_DEPOSIT", "adv", t0 + 5, 100)]
    assert amounts(pm_cash(pending, SINCE)) == {"P:ach": 0.0, "P:adv": 100.0}  # spendable so far: the advance
    cleared = [pcash("ACCOUNT_DEPOSIT", "ach", t0, 500), pcash("ACCOUNT_ADVANCED_DEPOSIT", "adv", t0 + 5, 100)]
    assert amounts(pm_cash(cleared, SINCE)) == {"P:ach": 500.0, "P:adv": 0.0}  # the whole deposit, once
    returned = [pcash("ACCOUNT_DEPOSIT", "ach", t0, 500, "REJECTED"), pcash("ACCOUNT_ADVANCED_DEPOSIT", "adv", t0 + 5, 100)]
    assert amounts(pm_cash(returned, SINCE)) == {"P:ach": 0.0, "P:adv": 0.0}
    alone = [pcash("ACCOUNT_ADVANCED_DEPOSIT", "adv", t0, 100)]  # no deposit to tie it to: as it says
    assert amounts(pm_cash(alone, SINCE)) == {"P:adv": 100.0}


def test_polymarket_trades_and_resolutions_outside_the_traders_markets():
    t0 = SINCE + 100
    got = pm_markets([
        ptrade("a", "p-own", t0, "ORDER_INTENT_BUY_LONG", 10, 0.40, 0.10),
        ptrade("b", "p-own", t0, "ORDER_INTENT_BUY_SHORT", 10, 0.40, 0.10, aggressor=False),  # NO at 1 - 40c
        ptrade("c", "p-own", t0, "ORDER_INTENT_SELL_LONG", 4, 0.70, 0.05),
        ptrade("d", "p-own", t0, "ORDER_INTENT_SELL_SHORT", 4, 0.70, 0.05),  # NO sold at 1 - 70c
        ptrade("e", "p-own", t0, "ORDER_INTENT_BUY_LONG", 10, 0.40, 0.10, state="TRADE_STATE_BUSTED"),
        ptrade("f", "p-1", t0, "ORDER_INTENT_BUY_LONG", 10, 0.40, 0.10),  # the trader's market
        presolve("p-own", t0 + 900, 6),
        presolve("p-1", t0 + 900, 10),
    ], lambda m: m == "p-1")
    assert amounts(got) == {"P:trade:a": -4.10, "P:trade:b": -6.10, "P:trade:c": 2.75, "P:trade:d": 1.15,
                            "P:trade:e": 0.0, f"P:resolution:p-own:{iso(t0 + 900)}": 6.0}
    assert [m.kind for m in got][-1] == "payout"


# --- reading the ledgers -----------------------------------------------------------------

class Ledger:
    """Both venues' ledger endpoints, paged like the real ones (Polymarket's newest first)."""

    def __init__(self):
        self.k = {"deposits": [], "withdrawals": [], "fills": [], "settlements": []}
        self.k_pos: dict[str, float] = {}
        self.p: list[dict] = []
        self.down: set[str] = set()
        self.pos_reads = 0
        self.on_positions = None
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        venue = "K" if request.url.host == "k.example" else "P"
        path, params = request.url.path, request.url.params
        self.requests.append((venue, path))
        if venue in self.down:
            return httpx.Response(503, text="down")
        offset, limit = int(params.get("cursor") or 0), int(params.get("limit") or 100)
        if venue == "K":
            name = path.rsplit("/", 1)[1]
            if name == "positions":
                self.pos_reads += 1
                if self.on_positions is not None:
                    self.on_positions(self.pos_reads)
                return httpx.Response(200, json={"market_positions": [
                    {"ticker": t, "position_fp": f"{v:.2f}"} for t, v in self.k_pos.items()], "cursor": ""})
            rows = self.k[name]
            if params.get("min_ts"):
                when = (lambda r: r["ts"]) if name == "fills" else (lambda r: moves._iso(r["settled_time"]))
                rows = [r for r in rows if when(r) >= int(params["min_ts"])]
            page = rows[offset:offset + limit]
            more = offset + limit < len(rows)
            return httpx.Response(200, json={name: page, "cursor": str(offset + limit) if more else ""})
        assert path == "/v1/portfolio/activities"
        types = params.get_list("types")
        rows = sorted((a for a in self.p if a["type"] in types), key=moves._activity_ts, reverse=True)
        page = rows[offset:offset + limit]
        more = offset + limit < len(rows)
        return httpx.Response(200, json={"activities": page, "nextCursor": str(offset + limit) if more else "",
                                         "eof": not more})

    def venues(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return KalshiTrading(http, K_BASE, SIGNER), PMTrading(http, P_BASE, SIGNER)


def _db(tmp_path):
    return connect(str(tmp_path / "m.db"))


def _ledger(now):
    led = Ledger()
    led.k["deposits"] = [kmoney("dep", now - 500, 10204, 204), kmoney("before", SINCE - 100, 5000)]
    led.k["fills"] = [kfill("sale", "K-OWN", now - 400, 40, 0.92, "ask", 0.20),
                      kfill("bot", "K-BOT", now - 300, 10, 0.45, "bid", 0.02)]
    led.k["settlements"] = [ksettle("K-BOT", now - 200, yes_count=10, revenue_cents=1000)]
    led.k_pos = {"K-OWN": 60.0}
    led.p = [pcash("ACCOUNT_WITHDRAWAL", "out", now - 450, 25), pcash("REFERRAL_BONUS", "gift", now - 440, 5),
             pcash("TAKER_FEE_REBATE", "rebate", now - 430, 0.5),
             ptrade("own", "p-own", now - 350, "ORDER_INTENT_BUY_SHORT", 10, 0.30, 0.10),
             ptrade("bot", "p-bot", now - 340, "ORDER_INTENT_BUY_LONG", 10, 0.30, 0.10),
             presolve("p-own", now - 100, 10)]
    return led


OWN = {"K": 100.0 + 40 * 0.92 - 0.20, "P": -25 + 5 - (10 * 0.70 + 0.10) + 10}


def test_the_ledgers_are_read_into_your_own_moves_once_and_kept(tmp_path):
    now = time.time()
    led = _ledger(now)
    db = _db(tmp_path)
    ours = lambda m: m in ("K-BOT", "p-bot")  # noqa: E731
    m = Moves(db, db, ours)
    k, p = led.venues()
    asyncio.run(m.refresh(k, p, SINCE))
    assert m.total(SINCE) == {"K": pytest.approx(OWN["K"]), "P": pytest.approx(OWN["P"])}
    snap = m.snapshot(SINCE)
    assert snap["read"] is not None and snap["errors"] is None and len(snap["recent"]) == 6
    assert [r["ts"] for r in snap["recent"]] == sorted((r["ts"] for r in snap["recent"]), reverse=True)
    rows = db.execute("SELECT COUNT(*) FROM live_moves").fetchone()[0]
    # Read again: the same entries, nothing counted twice.
    asyncio.run(m.refresh(k, p, SINCE))
    assert db.execute("SELECT COUNT(*) FROM live_moves").fetchone()[0] == rows
    assert m.total(SINCE) == {"K": pytest.approx(OWN["K"]), "P": pytest.approx(OWN["P"])}
    # A restart starts from what's kept, and from where the last read got to.
    again = Moves(db, db, ours)
    assert again.total(SINCE) == m.total(SINCE) and again.read_to == m.read_to and set(again.read_to) == {"K", "P"}
    assert again.snapshot(None) is None
    # Counted from when live trading started: a later start leaves out what came before it.
    assert m.total(now - 420)["K"] == pytest.approx(40 * 0.92 - 0.20)


def test_a_later_read_goes_back_only_a_little_past_the_last_one(tmp_path):
    now = time.time()
    led = Ledger()
    # Two days of trades in a market the trader never traded, one every 10 minutes.
    led.p = [ptrade(f"t{i}", "p-own", now - 600 * i - 60, "ORDER_INTENT_BUY_LONG", 1, 0.5, 0.0) for i in range(300)]
    db = _db(tmp_path)
    m = Moves(db, db, NOT_OURS)
    k, p = led.venues()
    asyncio.run(m.refresh(k, p, now - 2 * DAY))
    assert m.total(0)["P"] == pytest.approx(-0.5 * 288)  # the trades since the start, and none before
    reads = led.requests.count(("P", "/v1/portfolio/activities"))
    led.requests.clear()
    led.p.insert(0, ptrade("new", "p-own", time.time(), "ORDER_INTENT_SELL_LONG", 1, 0.6, 0.0))
    asyncio.run(m.refresh(k, p, now - 2 * DAY))
    assert m.total(0)["P"] == pytest.approx(-0.5 * 288 + 0.6)
    later = led.requests.count(("P", "/v1/portfolio/activities"))
    assert reads == 1 + 29 and later <= 3  # the cash types, then trades back to the start; later, to the overlap
    fills = [r for r in led.requests if r == ("K", "/trade-api/v2/portfolio/fills")]
    assert len(fills) == 1
    assert ("K", "/trade-api/v2/portfolio/positions") not in led.requests  # nothing of yours on Kalshi to walk


def test_a_position_that_moves_while_the_ledger_is_read_waits_for_the_next_read(tmp_path):
    now = time.time()
    led = Ledger()
    led.k_pos = {"K-OWN": 60.0}
    led.k["fills"] = [kfill("first", "K-OWN", now - 100, 40, 0.92, "ask")]

    def sell_more(n):
        if n == 2:  # between the two position reads: a fill the ledger read missed
            led.k_pos["K-OWN"] = 50.0
            led.k["fills"].append(kfill("second", "K-OWN", now - 1, 10, 0.90, "ask"))
    led.on_positions = sell_more
    db = _db(tmp_path)
    m = Moves(db, db, NOT_OURS)
    k, p = led.venues()
    asyncio.run(m.refresh(k, p, SINCE))
    assert m.total(SINCE)["K"] == 0.0 and "K" not in m.read_to and m.read_ts is None
    led.on_positions = None
    asyncio.run(m.refresh(k, p, SINCE))
    assert m.total(SINCE)["K"] == pytest.approx(40 * 0.92 + 10 * 0.90) and "K" in m.read_to

    # The trader's own positions moving (it traded meanwhile) hold nothing up.
    def trade(n):
        if n % 2 == 0:
            led.k_pos["K-BOT"] = led.k_pos.get("K-BOT", 0.0) + 10
    led.on_positions = trade
    mine = Moves(db, db, lambda x: x == "K-BOT")
    before = dict(mine.read_to)
    asyncio.run(mine.refresh(k, p, SINCE))
    assert mine.read_to["K"] > before["K"] and mine.read_ts is not None


def test_one_venue_down_doesnt_hold_up_the_other(tmp_path):
    now = time.time()
    led = _ledger(now)
    led.down = {"P"}
    db = _db(tmp_path)
    m = Moves(db, db, lambda x: x in ("K-BOT", "p-bot"))
    k, p = led.venues()
    asyncio.run(m.refresh(k, p, SINCE))
    snap = m.snapshot(SINCE)
    assert snap["K"] == pytest.approx(OWN["K"]) and snap["P"] == 0.0
    assert set(snap["errors"]) == {"P"} and snap["read"] is None and "P" not in m.read_to
    led.down = set()
    asyncio.run(m.refresh(k, p, SINCE))
    snap = m.snapshot(SINCE)
    assert snap["P"] == pytest.approx(OWN["P"]) and snap["errors"] is None and snap["read"] is not None


def test_no_ledger_request_goes_while_a_trade_is_in_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(moves, "PAUSE_S", 0.01)
    now = time.time()
    led = _ledger(now)
    db = _db(tmp_path)
    busy = {"trading": True}
    m = Moves(db, db, lambda x: x in ("K-BOT", "p-bot"), idle=lambda: not busy["trading"])
    k, p = led.venues()

    async def go():
        task = asyncio.create_task(m.refresh(k, p, SINCE))
        await asyncio.sleep(0.2)
        assert led.requests == [] and not task.done()  # held while the trade is on
        busy["trading"] = False
        await task
        # Then one at a time, a pause apart.
        stamps = []
        led.handler, real = (lambda r: (stamps.append(time.monotonic()), real(r))[1]), led.handler
        await m.refresh(*led.venues(), SINCE)
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert len(stamps) >= 8 and min(gaps) >= 0.009

    asyncio.run(go())
    assert m.total(SINCE) == {"K": pytest.approx(OWN["K"]), "P": pytest.approx(OWN["P"])}


def test_the_journal_knows_every_market_an_order_was_sent_to(tmp_path):
    path = tmp_path / livetrade.JOURNAL_FILE
    j = Journal(path)
    assert j.markets() == set()
    j.send("t1", Order("K", "K-1", "yes", "buy", 10, 0.45, client_id="c1"), 0.0)
    assert j.markets() == {"K-1"}
    with open(path, "a") as f:
        f.write('{"event": "send", "market": "cut sh\n')  # a line that can't be read
    j.send("t1", Order("P", "p-1", "no", "buy", 10, 0.50, client_id="c2"), 0.0)
    assert j.markets() == {"K-1", "p-1"} and Journal(path).markets() == {"K-1", "p-1"}


# --- the live run ------------------------------------------------------------------------

class Venues(Exchange):
    """The live trader's simulated exchanges, plus their ledgers: those must only be read
    on the ledger client."""

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self.ledger_reads_here = 0

    def handler(self, request):
        if request.url.path.rsplit("/", 1)[1] in ("deposits", "withdrawals", "fills", "settlements", "activities"):
            self.ledger_reads_here += 1
        return super().handler(request)


def test_the_live_run_reads_your_moves_on_their_own_connections(tmp_path):
    now = time.time()
    led = _ledger(now)
    led.k_pos = {"K-OWN": 60.0}
    cfg = Config(db_path=str(tmp_path / "l.db"), live_trading=True, live_shard_rebalance=False)
    db = connect(cfg.db_path)
    start = {"K": 100.0, "P": 100.0, "ts": SINCE}
    db.execute("INSERT INTO settings VALUES ('live_start', ?)", (json.dumps(start),))
    db.commit()
    ex = Venues(led)
    trading = httpx.AsyncClient(transport=httpx.MockTransport(ex.handler))
    run = LiveRun(cfg, db, db, LatencyModel(), {}, {}, {"K-1": KMeta("active", time.time() + DAY, 0.07)},
                  KalshiTrading(trading, K_BASE, SIGNER), PMTrading(trading, P_BASE, SIGNER))
    run.journal.send("t1", Order("K", "K-BOT", "yes", "buy", 10, 0.45, client_id="c1"), 0.0)  # the trader's
    db.execute("INSERT INTO live_trades (id, ts, pair, direction, k_side, p_side, planned_size, status) "
               "VALUES ('t1', ?, 'K-BOT|p-bot', 'K:YES+P:NO', 'yes', 'no', 10, 'settled')", (now - 360,))
    db.commit()
    ledger = httpx.AsyncClient(transport=httpx.MockTransport(led.handler))
    run.ledger_http = ledger

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(run.run(stop))
        for _ in range(200):
            await asyncio.sleep(0.02)
            if run.moves.read_ts is not None:
                break
        stop.set()
        await task

    asyncio.run(go())
    own = run.snapshot()["own"]
    assert own["K"] == pytest.approx(OWN["K"]) and own["P"] == pytest.approx(OWN["P"]) and own["errors"] is None
    assert ex.ledger_reads_here == 0 and led.requests  # never on the order client's connections
    assert run.ledger_http is None and ledger.is_closed and run.trader.start == start
    asyncio.run(trading.aclose())


def test_no_moves_are_read_before_live_trading_has_a_start(tmp_path):
    cfg = Config(db_path=str(tmp_path / "l.db"), live_trading=True)
    db = connect(cfg.db_path)
    run = LiveRun(cfg, db, db, LatencyModel(), {}, {}, {}, KalshiTrading(None, K_BASE, SIGNER),
                  PMTrading(None, P_BASE, SIGNER))
    assert run.trader.start is None and run.snapshot()["own"] is None
