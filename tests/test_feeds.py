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
    b.snapshot({"yes_dollars_fp": [["0.4000", "10.00"]], "no_dollars_fp": [["0.5500", "5.00"], ["0.5000", "7.00"]]}, 0)
    assert b.ladders() == ([(0.45, 5.0), (0.5, 7.0)], [(0.6, 10.0)])  # YES asks from NO bids
    b.delta({"price_dollars": "0.5500", "delta_fp": "-5.00", "side": "no", "ts_ms": 1000}, 1.5)
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
    """Speaks enough of Kalshi's WS protocol: subscribe/unsubscribe, snapshots, deltas."""

    def __init__(self, max_markets=1000):
        self.max_markets = max_markets
        self.commands: list[dict] = []
        self.conns = []
        self.headers = None
        self.next_sid = 1
        self.seq: dict[int, int] = {}
        self.sid_of: dict[str, int] = {}

    async def handler(self, ws):
        self.headers = ws.request.headers
        self.conns.append(ws)
        async for raw in ws:
            cmd = json.loads(raw)
            self.commands.append(cmd)
            params = cmd.get("params") or {}
            if cmd["cmd"] == "subscribe":
                tickers = params.get("market_tickers") or []
                if len(tickers) > self.max_markets:
                    await ws.send(json.dumps({"id": cmd["id"], "type": "error", "msg": {"code": 26, "msg": "limit"}}))
                    continue
                sid = self.next_sid
                self.next_sid += 1
                await ws.send(json.dumps({"id": cmd["id"], "type": "subscribed",
                                          "msg": {"channel": params["channels"][0], "sid": sid}}))
                for t in tickers:
                    self.sid_of[t] = sid
                    await self.send(ws, sid, "orderbook_snapshot",
                                    {"market_ticker": t, "yes_dollars_fp": [["0.3500", "100"]],
                                     "no_dollars_fp": [["0.6000", "20"]]})

    async def send(self, ws, sid, kind, msg, seq=None):
        self.seq[sid] = seq if seq is not None else self.seq.get(sid, 0) + 1
        await ws.send(json.dumps({"type": kind, "sid": sid, "seq": self.seq[sid], "msg": msg}))


def test_kalshi_feed_snapshot_delta_gap_and_split():
    server = FakeKalshiServer(max_markets=2)
    updates = []

    async def main():
        async with serve(server.handler, "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            feed = KalshiFeed(f"ws://127.0.0.1:{port}", KalshiSigner("kid", rsa_pem()), updates.append, lambda m: None)
            feed.set_markets(["A", "B", "C", "D"])
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            # 4 markets exceed the fake's per-subscription limit of 2: the feed splits.
            await _wait(lambda: all(t in feed.books and feed.books[t].ready for t in "ABCD"))
            assert server.headers["KALSHI-ACCESS-KEY"] == "kid"
            assert feed.books["A"].ladders()[0] == [(0.4, 20.0)]

            ws, sid = server.conns[-1], server.sid_of["A"]
            await server.send(ws, sid, "orderbook_delta",
                              {"market_ticker": "A", "price_dollars": "0.6000", "delta_fp": "-20", "side": "no",
                               "ts_ms": int(time.time() * 1000)})
            await _wait(lambda: feed.books["A"].ladders()[0] == [])
            assert feed.stats.lags

            # A skipped sequence number means a lost message: resubscribe for fresh books.
            n = len(server.commands)
            await server.send(ws, sid, "orderbook_delta",
                              {"market_ticker": "A", "price_dollars": "0.5000", "delta_fp": "5", "side": "no"},
                              seq=server.seq[sid] + 5)
            await _wait(lambda: any(c["cmd"] == "unsubscribe" for c in server.commands[n:]))
            await _wait(lambda: feed.books["A"].ready and feed.books["A"].ladders()[0] == [(0.4, 20.0)])

            # Dropping a market removes it from its subscription.
            feed.set_markets(["A", "B", "C"])
            await _wait(lambda: any(c.get("params", {}).get("action") == "delete_markets" for c in server.commands))
            assert "D" not in feed.books
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
            feed.set_markets([f"m-{i}" for i in range(150)])
            stop = asyncio.Event()
            task = asyncio.create_task(feed.run(stop))
            await _wait(lambda: len(feed.books) == 150)
            assert sorted(len(s["marketSlugs"]) for s in subs) == [50, 100]  # 100 per subscription
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


def _live(tmp_path, relation="same"):
    pairs = tmp_path / "pairs.csv"
    append_pair(str(pairs), "K-1", "p-1", relation)
    cfg = Config(db_path=str(tmp_path / "l.db"), pairs_path=str(pairs))
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
    kb.snapshot({"yes_dollars_fp": [["0.3500", "100"]], "no_dollars_fp": [["0.6000", "20"]]}, time.time())
    sc._on_kalshi("K-1")
    assert sc.pair_state["K-1|p-1"]["status"] == "paused"  # no Polymarket book yet

    # Kalshi YES ask 0.40 + Polymarket NO at 1 - 0.50: profitable after fees.
    _pm(sc, bids=[(0.50, 15)], offers=[(0.52, 50)])
    st = sc.pair_state["K-1|p-1"]
    assert st["status"] == "live" and st["edges"]["K:YES+P:NO"] > 0
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
    row = db.execute("SELECT * FROM sweeps").fetchone()
    assert row["depth_fetches"] >= 1  # evaluations in the window
    assert sc.last_sweep["n_pairs"] == 0
