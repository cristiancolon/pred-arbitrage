"""Streaming feeds and the streaming scanner, against local fake WebSocket servers."""

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from websockets.asyncio.server import serve

from arbscan.auth import KalshiSigner, PMSigner
from arbscan.config import Config
from arbscan.feeds import KalshiBook, KalshiFeed, PMFeed
from arbscan.live import LiveScanner
from arbscan.pairs import append_pair
from arbscan.scanner import KMeta
from arbscan.store import connect

from test_scanner import FakeKalshi, FakePM


def rsa_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def test_signers_produce_verifiable_signatures():
    pem = rsa_pem()
    k = KalshiSigner("kid", pem)
    h = k.headers("GET", "/trade-api/ws/v2?x=1")
    k.key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                              (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2").encode(),
                              padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                              hashes.SHA256())
    seed = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    p = PMSigner("pid", base64.b64encode(seed + b"\0" * 32).decode())  # portal secrets carry 64 bytes
    h = p.headers("GET", "/v1/ws/markets")
    p.key.public_key().verify(base64.b64decode(h["X-PM-Signature"]), (h["X-PM-Timestamp"] + "GET/v1/ws/markets").encode())
    assert h["X-PM-Access-Key"] == "pid"


def test_kalshi_book_applies_deltas():
    b = KalshiBook()
    # Kalshi quotes NO bids on the YES scale (use_yes_price): these are NO bids at 55c and 50c.
    b.snapshot({"yes_dollars_fp": [["0.4000", "10.00"]], "no_dollars_fp": [["0.4500", "5.00"], ["0.5000", "7.00"]]}, 0)
    assert b.no == {0.55: 5.0, 0.5: 7.0}
    assert b.ladders() == ([(0.45, 5.0), (0.5, 7.0)], [(0.6, 10.0)])  # YES asks from NO bids
    b.delta({"price_dollars": "0.4500", "delta_fp": "-5.00", "side": "no", "ts_ms": 1000}, 1.5)
    b.delta({"price_dollars": "0.4200", "delta_fp": "3.00", "side": "yes", "ts_ms": 1000}, 1.5)
    assert b.ladders() == ([(0.5, 7.0)], [(0.58, 3.0), (0.6, 10.0)])
    assert b.exch_ts == 1.0


async def _wait(cond, timeout=5.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


class FakeKalshiServer:
    """Speaks Kalshi's WS protocol as observed in production: the first orderbook
    subscribe creates a subscription, later ones are merged into it and acknowledged
    with ``ok`` (listing every market), and all of those messages share one sequence."""

    def __init__(self):
        self.commands: list[dict] = []
        self.conns = []
        self.headers = None

    async def handler(self, ws):
        self.headers = ws.request.headers
        self.conns.append(ws)
        state = {"sid": None, "seq": 0, "markets": []}
        ws.state_ = state
        async for raw in ws:
            cmd = json.loads(raw)
            self.commands.append(cmd)
            params = cmd.get("params") or {}
            if cmd["cmd"] == "subscribe" and params["channels"] == ["market_lifecycle_v2"]:
                await ws.send(json.dumps({"id": cmd["id"], "type": "subscribed",
                                          "msg": {"channel": "market_lifecycle_v2", "sid": 99}}))
            elif cmd["cmd"] == "subscribe":
                tickers = params["market_tickers"]
                state["markets"] += tickers
                if state["sid"] is None:
                    state["sid"] = 1
                    await ws.send(json.dumps({"id": cmd["id"], "type": "subscribed",
                                              "msg": {"channel": "orderbook_delta", "sid": 1}}))
                else:
                    await self.send(ws, "ok", {"market_tickers": state["markets"]}, cid=cmd["id"])
                for t in tickers:
                    await self.send(ws, "orderbook_snapshot", {"market_ticker": t, "yes_dollars_fp": [["0.3500", "100"]],
                                                               "no_dollars_fp": [["0.4000", "20"]]})  # NO bid at 60c
            elif cmd["cmd"] == "update_subscription" and params.get("action") == "delete_markets":
                state["markets"] = [t for t in state["markets"] if t not in params["market_tickers"]]
                await self.send(ws, "ok", {"market_tickers": state["markets"]}, cid=cmd["id"])

    async def send(self, ws, kind, msg, cid=None, skip=0):
        st = ws.state_
        st["seq"] += 1 + skip
        out = {"type": kind, "sid": st["sid"], "seq": st["seq"], "msg": msg}
        if cid is not None:
            out["id"] = cid
        await ws.send(json.dumps(out))


def test_kalshi_feed_merged_subscription_gap_and_delete():
    server = FakeKalshiServer()
    updates = []

    async def main():
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            feed = KalshiFeed(f"ws://127.0.0.1:{port}", KalshiSigner("kid", rsa_pem()), updates.append, lambda m: None)
            feed.CHUNK = 2  # several subscribes, merged by the server; their acks take sequence numbers
            feed.set_markets(["A", "B", "C", "D", "E"])
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            await _wait(lambda: all(t in feed.books and feed.books[t].ready for t in "ABCDE"))
            assert server.headers["KALSHI-ACCESS-KEY"] == "kid"
            assert feed.books["A"].ladders()[0] == [(0.4, 20.0)]
            assert feed.stats.reconnects == 0  # acks in the sequence are not gaps
            books = [c["params"] for c in server.commands if c["params"]["channels"] == ["orderbook_delta"]]
            assert books and all(p["use_yes_price"] is True for p in books)

            ws = server.conns[-1]
            await server.send(ws, "orderbook_delta", {"market_ticker": "A", "price_dollars": "0.4000", "delta_fp": "-20",
                                                      "side": "no", "ts_ms": int(time.time() * 1000)})
            await _wait(lambda: feed.books["A"].ladders()[0] == [])
            assert feed.stats.lags

            # Dropping a market removes it from the subscription.
            feed.set_markets(["A", "B", "C", "D"])
            await _wait(lambda: any(c.get("params", {}).get("action") == "delete_markets" for c in server.commands))
            assert "E" not in feed.books

            # A skipped sequence number means a lost message: reconnect for fresh books.
            await server.send(ws, "orderbook_delta", {"market_ticker": "A", "price_dollars": "0.5000", "delta_fp": "5",
                                                      "side": "no"}, skip=3)
            await _wait(lambda: len(server.conns) == 2)
            await _wait(lambda: all(feed.books[t].ready for t in "ABCD") and feed.books["A"].ladders()[0] == [(0.4, 20.0)])
            assert feed.stats.reconnects == 1
            stop.set()
            await task

    asyncio.run(asyncio.wait_for(main(), 20))
    assert "A" in updates


def test_pm_feed_books_state_and_reconnect():
    subs = []
    conns = []

    async def handler(ws):
        conns.append(ws)
        async for raw in ws:
            req = json.loads(raw)["subscribe"]
            subs.append(req)
            for slug in req["marketSlugs"]:
                await ws.send(json.dumps({"requestId": req["requestId"], "marketData": {
                    "marketSlug": slug, "state": "MARKET_STATE_OPEN", "transactTime": "2026-09-25T01:00:00.250Z",
                    "bids": [{"px": {"value": "0.500"}, "qty": "15"}],
                    "offers": [{"px": {"value": "0.520"}, "qty": "50"}]}}))

    updates = []

    async def main():
        async with serve(handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            seed = base64.b64encode(b"\1" * 64).decode()
            feed = PMFeed(f"ws://127.0.0.1:{port}", PMSigner("pid", seed), updates.append)
            feed.PER_CONN = 120  # the real limit is 10 x 100 markets per connection
            feed.set_markets([f"m-{i}" for i in range(150)])
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            await _wait(lambda: len(feed.books) == 150)
            assert sorted(len(s["marketSlugs"]) for s in subs) == [20, 30, 100]  # 100 per subscription
            assert len(conns) == 2 and feed.stats.snapshot()["connections"] == 2
            assert subs[0]["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"
            b = feed.books["m-0"]
            assert b.open and b.yes_asks == [(0.52, 50.0)] and b.no_asks == [(0.5, 15.0)]
            assert b.exch_ts == pytest.approx(1790298000.25)

            await conns[0].close()  # server drops the connection
            await _wait(lambda: not feed.books["m-0"].ready or feed.stats.reconnects >= 1)
            await _wait(lambda: feed.stats.reconnects >= 1 and feed.books["m-0"].ready, timeout=10)
            stop.set()
            await task

    asyncio.run(asyncio.wait_for(main(), 30))


def _live(tmp_path, relation="same", **settings):
    tmp_path.mkdir(parents=True, exist_ok=True)
    pairs = tmp_path / "pairs.csv"
    append_pair(str(pairs), "K-1", "p-1", relation)
    cfg = Config(db_path=str(tmp_path / "l.db"), pairs_path=str(pairs), **settings)
    db = connect(cfg.db_path)
    noop = lambda *_: None  # noqa: E731
    kfeed = KalshiFeed("ws://unused", KalshiSigner("kid", rsa_pem()), noop, noop)
    pfeed = PMFeed("ws://unused", PMSigner("pid", base64.b64encode(b"\1" * 64).decode()), noop)
    sc = LiveScanner(cfg, db, FakeKalshi({}), FakePM([], []), kfeed, pfeed)
    sc.pairs.refresh()
    sc._reindex()
    sc.kmeta["K-1"] = KMeta("active", time.time() + 86400, 0.07)
    sc.pm_coef["p-1"] = 0.0695
    return sc, db


def _pm(sc, bids, offers, state="MARKET_STATE_OPEN"):
    lv = lambda levels: [{"px": {"value": str(p)}, "qty": str(q)} for p, q in levels]  # noqa: E731
    sc.pfeed.books.setdefault("p-1", __import__("arbscan.feeds", fromlist=["PMBook"]).PMBook()).update(
        {"bids": lv(bids), "offers": lv(offers), "state": state}, time.time())
    sc._on_pm("p-1")


def test_live_scanner_prices_on_every_update(tmp_path):
    sc, db = _live(tmp_path)
    kb = sc.kfeed.books.setdefault("K-1", KalshiBook())
    kb.snapshot({"yes_dollars_fp": [["0.3500", "100"]], "no_dollars_fp": [["0.4000", "20"]]}, time.time())
    sc._on_kalshi("K-1")
    assert sc.pair_state["K-1|p-1"]["status"] == "paused"  # no Polymarket book yet

    # Kalshi YES ask 0.40 + Polymarket NO at 1 - 0.50: profitable after fees.
    _pm(sc, bids=[(0.50, 15)], offers=[(0.52, 50)])
    st = sc.pair_state["K-1|p-1"]
    assert st["status"] == "live" and st["edges"]["K:YES+P:NO"] > 0
    sc.out.flush()  # recordings are written by a background thread
    opp = db.execute("SELECT * FROM opportunities").fetchall()
    assert len(opp) == 1 and opp[0]["size"] == 15
    assert ("K-1|p-1", "K:YES+P:NO") in sc.episodes.open

    # Polymarket suspends trading (e.g. in-game): the pair pauses and the window closes.
    _pm(sc, bids=[(0.50, 15)], offers=[(0.52, 50)], state="MARKET_STATE_SUSPENDED")
    assert sc.pair_state["K-1|p-1"]["status"] == "paused"
    assert sc.pair_state["K-1|p-1"]["reason"] == "Polymarket suspended"
    assert not sc.episodes.open

    # Kalshi says the market settled: the pair is retired and unsubscribed.
    sc._on_lifecycle({"market_ticker": "K-1", "event_type": "determined"})
    assert "K-1|p-1" in sc.finished and "K-1" not in sc.kfeed.wanted

    sc._tick()
    sc.out.flush()
    row = db.execute("SELECT * FROM sweeps").fetchone()
    assert row["depth_fetches"] >= 1  # evaluations in the window
    assert sc.last_sweep["n_pairs"] == 0
    sc.out.close()


def test_lifecycle_events_for_the_live_accounts_markets_come_through():
    seen = []
    feed = KalshiFeed("ws://unused", KalshiSigner("kid", rsa_pem()), lambda t: None, seen.append)
    feed.set_markets(["A"])
    feed.also = {"HELD"}  # a market the live account holds, its pair no longer watched
    for t in ("A", "HELD", "OTHER"):
        feed._handle({"type": "market_lifecycle_v2", "msg": {"market_ticker": t, "event_type": "settled"}}, 0.0)
    assert [m["market_ticker"] for m in seen] == ["A", "HELD"]


def test_a_market_left_out_of_a_reply_isnt_retired(tmp_path):
    # 2026-10-01 06:16: a throttled metadata refresh retired ~5,000 pairs whose markets
    # were open on both venues. Only a venue saying a market is closed retires a pair.
    sc, _ = _live(tmp_path)

    async def nothing(_):
        return {}
    sc.pm.markets, sc.kalshi.markets = nothing, nothing
    asyncio.run(sc.refresh_meta(force=True))
    assert "K-1|p-1" not in sc.finished and sc.kmeta["K-1"].status == "active"  # what we knew still stands

    async def closed(slugs):
        return {s: {"slug": s, "status": "MARKET_STATUS_RESOLVED", "closed": True} for s in slugs}
    sc.pm.markets = closed
    asyncio.run(sc.refresh_meta(force=True))
    assert "K-1|p-1" in sc.finished
    asyncio.run(sc.http.aclose())


def test_a_kalshi_market_never_found_waits_paused_rather_than_finished(tmp_path):
    sc, _ = _live(tmp_path)
    del sc.kmeta["K-1"]

    async def nothing(_):
        return {}
    sc.kalshi.markets = nothing
    asyncio.run(sc.refresh_meta(force=True))
    assert sc.kmeta["K-1"].status == "missing" and "K-1|p-1" not in sc.finished
    asyncio.run(sc.http.aclose())
