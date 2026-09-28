"""Live order clients and the order-test round trip, against a mocked exchange.
Nothing here reaches a venue."""

import asyncio
import json

import httpx
import pytest

from arbscan import ordertest, store
from arbscan.orders import KalshiTrading, Order, PMTrading, Result, record


class Signer:
    def __init__(self):
        self.signed = []

    def headers(self, method, path):
        self.signed.append((method, path))
        return {"X-Signed": path}


def client(handler):
    calls = []

    def h(request):
        calls.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(h)), calls


K_BASE = "https://k.example/trade-api/v2"
P_BASE = "https://p.example"


def test_kalshi_writes_every_order_as_a_yes_bid_or_ask():
    b = KalshiTrading.body
    assert b(Order("K", "T", "yes", "buy", 3, 0.45, client_id="c")) == {
        "ticker": "T", "client_order_id": "c", "side": "bid", "count": "3.00", "price": "0.4500",
        "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    assert (b(Order("K", "T", "no", "buy", 2, 0.08))["side"], b(Order("K", "T", "no", "buy", 2, 0.08))["price"]) \
        == ("ask", "0.9200")  # buying NO at 8c is selling YES at 92c
    sell_yes = b(Order("K", "T", "yes", "sell", 1, 0.55, reduce_only=True))
    assert (sell_yes["side"], sell_yes["price"], sell_yes["reduce_only"]) == ("ask", "0.5500", True)
    sell_no = b(Order("K", "T", "no", "sell", 1, 0.40))
    assert (sell_no["side"], sell_no["price"]) == ("bid", "0.6000") and "reduce_only" not in sell_no


def test_polymarket_prices_every_order_in_yes():
    b = PMTrading.body
    cases = {("yes", "buy", 0.45): ("ORDER_INTENT_BUY_LONG", "0.45"),
             ("no", "buy", 0.08): ("ORDER_INTENT_BUY_SHORT", "0.92"),
             ("yes", "sell", 0.555): ("ORDER_INTENT_SELL_LONG", "0.555"),
             ("no", "sell", 0.40): ("ORDER_INTENT_SELL_SHORT", "0.6")}
    for (side, action, limit), (intent, price) in cases.items():
        body = b(Order("P", "slug", side, action, 2, limit))
        assert (body["intent"], body["price"]) == (intent, {"value": price, "currency": "USD"})
        assert body["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and body["quantity"] == 2
        assert body["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC" and body["synchronousExecution"]


def test_kalshi_fill_is_read_back_in_the_side_bought():
    async def main():
        c, calls = client(lambda r: httpx.Response(201, json={
            "order_id": "o1", "fill_count": "2.00", "remaining_count": "1.00", "average_fill_price": "0.9100",
            "average_fee_paid": "0.0064", "ts_ms": 1700000000123}))
        signer = Signer()
        res = await KalshiTrading(c, K_BASE, signer).place(Order("K", "T", "no", "buy", 3, 0.09))
        assert len(calls) == 1 and calls[0].method == "POST"
        assert str(calls[0].url) == K_BASE + "/portfolio/events/orders"
        assert signer.signed == [("POST", "/trade-api/v2/portfolio/events/orders")]
        assert json.loads(calls[0].content)["side"] == "ask"
        return res

    res = asyncio.run(main())
    assert res.status == "partial" and res.filled == 2 and res.order_id == "o1"
    assert res.avg_price == pytest.approx(0.09)  # sold YES at 91c = bought NO at 9c
    assert res.fees == pytest.approx(0.0128) and res.exch_ts == pytest.approx(1700000000.123)


@pytest.mark.parametrize("reply, status", [
    (httpx.Response(400, json={"code": "invalid"}), "rejected"),
    (httpx.Response(429, text="slow down"), "rejected"),
    (httpx.Response(409, json={"code": "duplicate"}), "unknown"),  # that client order id was already used
    (httpx.Response(500, text="oops"), "unknown"),
    (httpx.ReadTimeout("slow"), "unknown"),  # it may have reached the exchange
    (httpx.ConnectError("refused"), "rejected"),  # it never left
])
def test_an_order_is_sent_once_and_an_unclear_outcome_stays_unknown(reply, status):
    def handler(request):
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def main():
        c, calls = client(handler)
        res = await KalshiTrading(c, K_BASE, Signer()).place(Order("K", "T", "yes", "buy", 1, 0.5))
        return res, len(calls)

    res, n = asyncio.run(main())
    assert n == 1 and res.status == status and res.filled == 0 and res.error


def _exec(kind, shares=None, px=None, fee=None, state=None):
    e = {"type": kind, "transactTime": "2026-09-28T12:00:00.250Z", "order": {"state": state} if state else {}}
    if shares is not None:
        e.update(lastShares=str(shares), lastPx={"value": str(px), "currency": "USD"},
                 commissionNotionalCollected={"value": str(fee), "currency": "USD"})
    return e


def test_polymarket_fills_are_summed_and_read_back_in_the_side_bought():
    reply = {"id": "p1", "executions": [
        _exec("EXECUTION_TYPE_PARTIAL_FILL", 1, 0.92, 0.01, "ORDER_STATE_PARTIALLY_FILLED"),
        _exec("EXECUTION_TYPE_FILL", 2, 0.91, 0.01, "ORDER_STATE_FILLED")]}
    o = Order("P", "s", "no", "buy", 3, 0.09)
    res = PMTrading.parse(o, reply, Result(o, "unknown"))
    assert res.status == "filled" and res.filled == 3 and res.fees == pytest.approx(0.02)
    assert res.avg_price == pytest.approx(1 - (0.92 + 2 * 0.91) / 3, abs=1e-6)
    assert res.exch_ts is not None

    part = {"id": "p2", "executions": [
        _exec("EXECUTION_TYPE_PARTIAL_FILL", 1, 0.40, 0.0, "ORDER_STATE_PARTIALLY_FILLED"),
        _exec("EXECUTION_TYPE_CANCELED", state="ORDER_STATE_CANCELED")]}
    o = Order("P", "s", "yes", "buy", 3, 0.40)
    res = PMTrading.parse(o, part, Result(o, "unknown"))
    assert (res.status, res.filled, res.avg_price) == ("partial", 1, 0.40)

    rej = {"id": "p3", "executions": [{"type": "EXECUTION_TYPE_REJECTED",
                                       "orderRejectReason": "ORD_REJECT_REASON_NO_LIQUIDITY",
                                       "order": {"state": "ORDER_STATE_REJECTED"}}]}
    res = PMTrading.parse(o, rej, Result(o, "unknown"))
    assert res.status == "rejected" and "NO_LIQUIDITY" in res.error


def test_polymarket_trusts_the_orders_own_tally_and_reads_back_an_unfinished_order():
    o = Order("P", "s", "yes", "buy", 2, 0.40)
    # The reply lists no fill, but the order says one filled: never report that as nothing.
    reply = {"id": "p1", "executions": [_exec("EXECUTION_TYPE_NEW", state="ORDER_STATE_NEW")]}
    reply["executions"][0]["order"].update(cumQuantity=1, avgPx={"value": "0.39"})
    res = PMTrading.parse(o, reply, Result(o, "unknown"))
    assert res.filled == 1 and res.avg_price == pytest.approx(0.39) and res.status == "unknown"

    async def main():
        c, calls = client(lambda r: httpx.Response(200, json={"order": {
            "state": "ORDER_STATE_CANCELED", "cumQuantity": 1, "avgPx": {"value": "0.39"},
            "commissionNotionalTotalCollected": {"value": "0.02"}}}))
        out = await PMTrading(c, P_BASE, Signer()).resolve(res)
        assert [(r.method, r.url.path) for r in calls] == [("GET", "/v1/order/p1")]
        return out

    res = asyncio.run(main())
    assert (res.status, res.filled, res.fees) == ("partial", 1, 0.02)


def test_polymarket_preview_validates_without_placing():
    async def main():
        c, calls = client(lambda r: httpx.Response(200, json={"order": {"id": ""}}))
        res = await PMTrading(c, P_BASE, Signer()).preview(Order("P", "s", "no", "buy", 1, 0.08))
        return res, calls

    res, calls = asyncio.run(main())
    assert res.status == "none" and [r.url.path for r in calls] == ["/v1/order/preview"]
    body = json.loads(calls[0].content)["request"]
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT" and "synchronousExecution" not in body


class FakeKalshi(KalshiTrading):
    """An exchange with one market and scripted fills."""

    def __init__(self, fills, cash=100.0):
        super().__init__(None, K_BASE, Signer())
        self.fills, self.cash, self.pos, self.sent = list(fills), cash, 0.0, []

    async def place(self, o):
        self.sent.append(o)
        n, px = self.fills.pop(0)
        res = Result(o, "filled" if n >= o.qty else "partial" if n else "none", filled=n, avg_price=px, fees=0.01 * n,
                     body=self.body(o))
        sign = 1 if (o.side == "yes") == (o.action == "buy") else -1
        self.pos += sign * n
        self.cash += (-1 if o.action == "buy" else 1) * (px or 0) * n - res.fees
        return res

    async def balance(self):
        return {"balance_dollars": f"{self.cash:.4f}"}

    async def position(self, ticker):
        return self.pos


def _tester(trader, yes_asks, no_asks, db):
    async def ladders(m):
        return yes_asks, no_asks
    lines = []
    return ordertest.Tester(trader, ladders, "K", db, out=lines.append, settle_s=0), lines


def test_order_test_buys_one_and_sells_back_what_filled(tmp_path):
    db = store.connect(str(tmp_path / "t.db"))
    ex = FakeKalshi([(1, 0.61), (1, 0.60)])
    t, lines = _tester(ex, [(0.61, 50)], [(0.40, 50)], db)  # YES ask 61c, YES bid 60c
    out = asyncio.run(t.round_trip("T", "yes", send=True))
    buy, sell = ex.sent
    assert (buy.action, buy.qty, buy.limit, buy.side) == ("buy", 1, 0.61, "yes")
    assert (sell.action, sell.qty, sell.limit, sell.reduce_only) == ("sell", 1, 0.60, True)
    assert ex.pos == 0 and [r.status for r in out] == ["filled", "filled"]
    rows = db.execute("SELECT mode, action, status FROM live_orders ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [("test", "buy", "filled"), ("test", "sell", "filled")]
    assert any("round trip cost 0.0300" in s for s in lines)


def test_order_test_sells_nothing_it_did_not_buy_and_skips_unfit_books(tmp_path):
    db = store.connect(str(tmp_path / "t.db"))
    ex = FakeKalshi([(0, None)])
    t, _ = _tester(ex, [(0.61, 50)], [(0.40, 50)], db)
    asyncio.run(t.round_trip("T", "no", send=True))
    assert [o.action for o in ex.sent] == ["buy"] and ex.sent[0].limit == 0.40  # NO ask 40c; nothing to sell

    for yes_asks, no_asks in (([(0.70, 5)], [(0.40, 5)]),  # YES 60c bid / 70c ask: spread 10c
                              ([(0.99, 5)], [(0.02, 5)]),  # too close to 1
                              ([(0.50, 5)], [(0.40, 5)]),  # crossed: YES bid 60c over its 50c ask
                              ([], [(0.40, 5)])):  # no YES ask
        ex = FakeKalshi([])
        t, lines = _tester(ex, yes_asks, no_asks, db)
        assert asyncio.run(t.round_trip("T", "yes", send=True)) == [] and not ex.sent and "skipped" in lines[0]


def test_order_test_without_send_places_nothing(tmp_path):
    db = store.connect(str(tmp_path / "t.db"))
    ex = FakeKalshi([])
    t, lines = _tester(ex, [(0.61, 50)], [(0.40, 50)], db)
    assert asyncio.run(t.round_trip("T", "yes", send=False)) == []
    assert not ex.sent and "would buy 1 at 0.61" in lines[0]
    assert db.execute("SELECT COUNT(*) FROM live_orders").fetchone()[0] == 0


def test_record_keeps_the_request_and_reply(tmp_path):
    db = store.connect(str(tmp_path / "t.db"))
    o = Order("P", "s", "no", "buy", 1, 0.08, client_id="c1")
    res = Result(o, "filled", filled=1, avg_price=0.08, fees=0.01, body=PMTrading.body(o), reply={"id": "x"})
    record(db, res, "test")
    r = dict(db.execute("SELECT * FROM live_orders").fetchone())
    assert (r["id"], r["mode"], r["venue"], r["side"], r["limit_price"]) == ("c1", "test", "P", "no", 0.08)
    assert r["status"] == "filled"
    assert json.loads(r["body"])["intent"] == "ORDER_INTENT_BUY_SHORT" and json.loads(r["reply"]) == {"id": "x"}
