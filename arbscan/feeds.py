"""Streaming order books from both venues' WebSocket feeds.

Each feed keeps an in-memory book per market and calls ``on_update(market_id)`` as
soon as a message changes it, so the scanner can re-price the affected pairs within
a millisecond of the data arriving instead of waiting for the next poll.

- Kalshi (``orderbook_delta``): a snapshot per market, then signed quantity deltas.
  Every message carries a per-subscription sequence number; a gap means a message
  was lost, so that subscription is re-created to get fresh snapshots.
  ``market_lifecycle_v2`` reports markets pausing, closing and settling.
- Polymarket US (``SUBSCRIPTION_TYPE_MARKET_DATA``): every message is the market's
  full top-of-book ladder plus its trading state, so there is nothing to sequence.

A book is only ``ready`` between its snapshot and the next disconnect or gap; the
scanner ignores pairs whose books aren't ready.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime

import orjson
import websockets

from .auth import KalshiSigner, PMSigner
from .book import Level, pmus_ladders

log = logging.getLogger(__name__)

KALSHI_WS_PATH = "/trade-api/ws/v2"
PM_WS_PATH = "/v1/ws/markets"
RECONNECT_MAX_S = 30.0


def _levels(raw: Iterable) -> dict[float, float]:
    out = {}
    for p, q in raw or ():
        q = float(q)
        if q > 0:
            out[round(float(p), 4)] = q
    return out


def _complement(bids: dict[float, float]) -> list[Level]:
    # A bid for one side at p is an offer of the other side at 1 - p.
    return sorted(((round(1.0 - p, 4), q) for p, q in bids.items()), key=lambda lv: lv[0])


class KalshiBook:
    __slots__ = ("yes", "no", "ready", "exch_ts", "recv_ts", "stamped", "_ladders", "_tops", "top_ts")

    def __init__(self) -> None:
        self.yes: dict[float, float] = {}  # YES bids: price -> contracts
        self.no: dict[float, float] = {}
        self.ready = False
        self.exch_ts: float | None = None
        self.recv_ts = 0.0
        self.stamped = False  # the last change carried the exchange's own timestamp
        self._ladders: tuple[list[Level], list[Level]] | None = None
        self._tops: tuple[float | None, float | None] = (None, None)
        self.top_ts = [0.0, 0.0]  # when the best YES / NO ask last moved to its current price

    def snapshot(self, msg: dict, recv: float) -> None:
        self.yes = _levels(msg.get("yes_dollars_fp"))
        self.no = _levels(msg.get("no_dollars_fp"))
        self.ready, self.recv_ts, self.stamped, self._ladders = True, recv, False, None
        self._tops = (None, None)  # after a (re)subscribe, count steadiness from the snapshot

    def delta(self, msg: dict, recv: float) -> None:
        side = self.yes if msg.get("side") == "yes" else self.no
        p = round(float(msg["price_dollars"]), 4)
        q = side.get(p, 0.0) + float(msg["delta_fp"])
        if q > 1e-9:
            side[p] = q
        else:
            side.pop(p, None)
        ts_ms = msg.get("ts_ms")
        self.stamped = bool(ts_ms)
        self.exch_ts = ts_ms / 1000 if ts_ms else self.exch_ts
        self.recv_ts, self._ladders = recv, None

    def ladders(self) -> tuple[list[Level], list[Level]]:
        """(yes_asks, no_asks): buying YES lifts NO bids and vice versa."""
        if self._ladders is None:
            self._ladders = (_complement(self.no), _complement(self.yes))
            tops = tuple(lad[0][0] if lad else None for lad in self._ladders)
            for i in (0, 1):
                if tops[i] != self._tops[i]:
                    self.top_ts[i] = self.recv_ts
            self._tops = tops
        return self._ladders


class PMBook:
    __slots__ = ("yes_asks", "no_asks", "state", "ready", "exch_ts", "recv_ts", "stamped", "top_ts")

    def __init__(self) -> None:
        self.yes_asks: list[Level] = []
        self.no_asks: list[Level] = []
        self.state = ""
        self.ready = False
        self.exch_ts: float | None = None
        self.recv_ts = 0.0
        self.stamped = False
        self.top_ts = [0.0, 0.0]  # when the best YES / NO ask last moved to its current price

    def update(self, md: dict, recv: float) -> None:
        old = (self.yes_asks[0][0] if self.yes_asks else None, self.no_asks[0][0] if self.no_asks else None)
        self.yes_asks, self.no_asks = pmus_ladders(md)
        new = (self.yes_asks[0][0] if self.yes_asks else None, self.no_asks[0][0] if self.no_asks else None)
        for i in (0, 1):
            if new[i] != old[i] or not self.ready:
                self.top_ts[i] = recv
        self.state = md.get("state") or self.state
        self.stamped = False
        t = md.get("transactTime")
        if t:
            try:
                self.exch_ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                self.stamped = self.ready  # the first message after subscribing is a snapshot
            except ValueError:
                pass
        self.ready, self.recv_ts = True, recv

    @property
    def open(self) -> bool:
        return self.state in ("", "MARKET_STATE_OPEN")


class FeedStats:
    """Message counts and exchange-to-receive lag, for the dashboard."""

    def __init__(self) -> None:
        self.messages = 0
        self.reconnects = 0
        self.connected_since: float | None = None
        self.last_message = 0.0
        self.lags: deque[float] = deque(maxlen=2000)  # seconds, exchange timestamp -> received

    def lag_ms(self, q: float = 0.5) -> float | None:
        if not self.lags:
            return None
        xs = sorted(self.lags)
        return 1000 * xs[min(len(xs) - 1, int(q * len(xs)))]

    def snapshot(self) -> dict:
        return {"connected": self.connected_since is not None, "connected_since": self.connected_since,
                "messages": self.messages, "reconnects": self.reconnects, "last_message": self.last_message,
                "lag_p50_ms": self.lag_ms(0.5), "lag_p90_ms": self.lag_ms(0.9)}


class _Feed:
    """Reconnect loop shared by both venues."""

    name = "feed"

    def __init__(self, url: str, on_update: Callable[[str], None]):
        self.url = url
        self.on_update = on_update
        self.wanted: set[str] = set()
        self.stats = FeedStats()
        self.ws = None
        self._changed = asyncio.Event()

    def set_markets(self, markets: Iterable[str]) -> None:
        new = set(markets)
        if new != self.wanted:
            self.wanted = new
            self._changed.set()

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def _on_connect(self) -> None:
        raise NotImplementedError

    async def _sync(self) -> None:
        """Bring subscriptions in line with ``wanted``."""
        raise NotImplementedError

    def _handle(self, msg: dict, recv: float) -> None:
        raise NotImplementedError

    def _disconnected(self) -> None:
        raise NotImplementedError

    async def _send(self, obj: dict) -> None:
        await self.ws.send(orjson.dumps(obj).decode())

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, additional_headers=self._headers(), max_size=2**24,
                                              ping_interval=20, ping_timeout=20, open_timeout=15,
                                              compression=None) as ws:
                    self.ws = ws
                    self.stats.connected_since = time.time()
                    log.info("%s feed connected", self.name)
                    await self._on_connect()
                    delay = 1.0
                    await self._pump(ws, stop)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                log.warning("%s feed: %s; reconnecting in %.0fs", self.name, e, delay)
            except Exception:
                log.exception("%s feed failed; reconnecting in %.0fs", self.name, delay)
            finally:
                self.ws = None
                if self.stats.connected_since is not None:
                    self.stats.reconnects += 1
                self.stats.connected_since = None
                self._disconnected()
            if stop.is_set():
                break
            if getattr(self, "_gap", False):
                delay = 0.2  # we closed on purpose to resync; come straight back
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(RECONNECT_MAX_S, delay * 2)

    async def _pump(self, ws, stop: asyncio.Event) -> None:
        async def close_on_stop():
            await stop.wait()
            await ws.close()

        async def resync():
            while True:
                await self._changed.wait()
                self._changed.clear()
                await self._sync()

        helpers = [asyncio.create_task(close_on_stop()), asyncio.create_task(resync())]
        try:
            async for raw in ws:  # ends when the connection closes; pings detect dead peers
                now = time.time()
                self.stats.messages += 1
                self.stats.last_message = now
                self._handle(orjson.loads(raw), now)
        finally:
            for t in helpers:
                t.cancel()


class KalshiFeed(_Feed):
    """Kalshi merges every orderbook subscribe on a connection into one subscription
    (later subscribes are acknowledged with ``ok`` and the full market list), and
    every message on it, acknowledgements included, takes the next sequence number.
    One subscription held 6,000 markets in testing, with all snapshots in ~1 s, so on
    a sequence gap the feed simply reconnects for fresh snapshots of everything."""

    name = "kalshi"
    CHUNK = 500  # markets per subscribe command

    def __init__(self, url: str, signer: KalshiSigner, on_update: Callable[[str], None],
                 on_lifecycle: Callable[[dict], None] | None = None):
        super().__init__(url, on_update)
        self.signer = signer
        self.on_lifecycle = on_lifecycle
        self.books: dict[str, KalshiBook] = {}
        self.rejected: set[str] = set()  # tickers Kalshi refused to subscribe to on their own
        self._id = 0
        self._pending: dict[int, list[str]] = {}  # orderbook subscribe command id -> tickers
        self._lifecycle_id: int | None = None
        self._book_sid: int | None = None
        self._requested: set[str] = set()
        self._last_seq: int | None = None
        self._gap = False

    def _headers(self) -> dict[str, str]:
        return self.signer.headers("GET", KALSHI_WS_PATH)

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _on_connect(self) -> None:
        self._pending.clear()
        self._requested.clear()
        self._book_sid = self._last_seq = None
        self._gap = False
        if self.on_lifecycle:
            self._lifecycle_id = self._next_id()
            await self._send({"id": self._lifecycle_id, "cmd": "subscribe",
                              "params": {"channels": ["market_lifecycle_v2"]}})
        await self._sync()

    async def _subscribe(self, tickers: list[str], size: int | None = None) -> None:
        size = size or self.CHUNK
        for i in range(0, len(tickers), size):
            chunk = tickers[i : i + size]
            cid = self._next_id()
            self._pending[cid] = chunk
            self._requested.update(chunk)
            await self._send({"id": cid, "cmd": "subscribe",
                              "params": {"channels": ["orderbook_delta"], "market_tickers": chunk}})

    async def _sync(self) -> None:
        if self.ws is None:
            return
        drop = sorted(t for t in self._requested if t not in self.wanted)
        if drop and self._book_sid is not None:
            self._requested.difference_update(drop)
            for t in drop:
                self.books.pop(t, None)
            for i in range(0, len(drop), self.CHUNK):
                await self._send({"id": self._next_id(), "cmd": "update_subscription",
                                  "params": {"sids": [self._book_sid], "market_tickers": drop[i : i + self.CHUNK],
                                             "action": "delete_markets"}})
        add = sorted(t for t in self.wanted if t not in self._requested and t not in self.rejected)
        if add:
            await self._subscribe(add)

    def _gap_detected(self, last: int, seq: int) -> None:
        if self._gap:
            return
        self._gap = True
        log.warning("kalshi feed: sequence gap (%s -> %s); reconnecting for fresh books", last, seq)
        for t, book in self.books.items():
            if book.ready:
                book.ready = False
                self.on_update(t)
        if self.ws is not None:
            asyncio.get_running_loop().create_task(self.ws.close())

    def _handle(self, msg: dict, recv: float) -> None:
        kind = msg.get("type")
        sid, seq = msg.get("sid"), msg.get("seq")
        if self._book_sid is None and kind in ("orderbook_snapshot", "orderbook_delta"):
            self._book_sid = sid
        if sid is not None and sid == self._book_sid and seq is not None:
            if self._last_seq is not None and seq != self._last_seq + 1:
                self._gap_detected(self._last_seq, seq)
            self._last_seq = seq
        if self._gap:
            return
        if kind == "orderbook_delta":
            body = msg["msg"]
            t = body["market_ticker"]
            book = self.books.get(t)
            if book is None or not book.ready:
                return
            book.delta(body, recv)
            if book.exch_ts:
                self.stats.lags.append(recv - book.exch_ts)
            self.on_update(t)
        elif kind == "orderbook_snapshot":
            body = msg["msg"]
            t = body["market_ticker"]
            if t not in self.wanted:
                return
            self.books.setdefault(t, KalshiBook()).snapshot(body, recv)
            self.on_update(t)
        elif kind == "market_lifecycle_v2":
            body = msg.get("msg") or {}
            if self.on_lifecycle and body.get("market_ticker") in self.wanted:
                self.on_lifecycle(body)
        elif kind == "subscribed":
            if msg.get("id") in self._pending:
                self._pending.pop(msg.get("id"))
                self._book_sid = (msg.get("msg") or {}).get("sid", self._book_sid)
        elif kind == "ok":
            self._pending.pop(msg.get("id"), None)
        elif kind == "error":
            err = msg.get("msg") or {}
            tickers = self._pending.pop(msg.get("id"), [])
            if not tickers:
                log.warning("kalshi feed error %s: %s", err.get("code"), err.get("msg"))
                return
            self._requested.difference_update(tickers)
            if len(tickers) == 1:
                self.rejected.add(tickers[0])
                log.warning("kalshi feed: can't subscribe to %s (%s: %s)", tickers[0], err.get("code"), err.get("msg"))
            else:
                # Find the market(s) Kalshi objects to by splitting the batch.
                half = len(tickers) // 2
                loop = asyncio.get_running_loop()
                loop.create_task(self._subscribe(tickers[:half], half))
                loop.create_task(self._subscribe(tickers[half:], len(tickers) - half))

    def _disconnected(self) -> None:
        for t, book in self.books.items():
            if book.ready:
                book.ready = False
                self.on_update(t)


class _PMConn(_Feed):
    """One Polymarket US connection. The server allows 10 subscriptions of up to 100
    markets each per connection ("max subscriptions per connection reached")."""

    name = "pmus"
    CHUNK = 100
    MAX_SUBS = 10

    def __init__(self, url: str, signer: PMSigner, books: dict[str, "PMBook"], on_update: Callable[[str], None],
                 index: int):
        super().__init__(url, on_update)
        self.signer = signer
        self.books = books  # shared across connections
        self.index = index
        self._subs: dict[str, list[str]] = {}  # request id -> slugs
        self._slug_sub: dict[str, str] = {}
        self._owned: set[str] = set()
        self._n = 0

    def _headers(self) -> dict[str, str]:
        return self.signer.headers("GET", PM_WS_PATH)

    async def _on_connect(self) -> None:
        self._subs.clear()
        self._slug_sub.clear()
        await self._sync()

    async def _unsubscribe(self, rid: str) -> None:
        for s in self._subs.pop(rid):
            self._slug_sub.pop(s, None)
        await self._send({"unsubscribe": {"requestId": rid}})

    async def _sync(self) -> None:
        if self.ws is None:
            return
        # Subscriptions can't be edited: drop those holding unwanted markets, then
        # subscribe what's missing. Repack everything if that would need more than
        # the server's subscription limit.
        for rid in [r for r, slugs in self._subs.items() if any(s not in self.wanted for s in slugs)]:
            await self._unsubscribe(rid)
        for s in [s for s in self.books if s not in self.wanted and s in self._owned]:
            self.books.pop(s, None)
        self._owned = set(self.wanted)
        add = sorted(s for s in self.wanted if s not in self._slug_sub)
        if len(self._subs) + -(-len(add) // self.CHUNK) > self.MAX_SUBS:
            for rid in list(self._subs):
                await self._unsubscribe(rid)
            add = sorted(self.wanted)
        for i in range(0, len(add), self.CHUNK):
            chunk = add[i : i + self.CHUNK]
            self._n += 1
            rid = f"md-{self.index}-{self._n}"
            self._subs[rid] = chunk
            for s in chunk:
                self._slug_sub[s] = rid
            await self._send({"subscribe": {"requestId": rid, "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                            "marketSlugs": chunk}})

    def _handle(self, msg: dict, recv: float) -> None:
        md = msg.get("marketData")
        if md is not None:
            slug = md.get("marketSlug")
            if slug not in self.wanted or msg.get("requestId") not in self._subs:
                return  # e.g. in flight after an unsubscribe
            book = self.books.get(slug)
            if book is None:
                book = self.books[slug] = PMBook()
            book.update(md, recv)
            if book.stamped:
                self.stats.lags.append(recv - book.exch_ts)
            self.on_update(slug)
        elif "error" in msg:
            log.warning("pmus feed error for %s: %s", msg.get("requestId"), msg["error"])

    def _disconnected(self) -> None:
        for s in self.wanted:
            book = self.books.get(s)
            if book is not None and book.ready:
                book.ready = False
                self.on_update(s)


class _Combined:
    """FeedStats-like view over several connections."""

    def __init__(self, conns: list[_PMConn]):
        self.conns = conns

    @property
    def reconnects(self) -> int:
        return sum(c.stats.reconnects for c in self.conns)

    def snapshot(self) -> dict:
        lags: list[float] = sorted(x for c in self.conns for x in c.stats.lags)

        def q(v: float) -> float | None:
            return 1000 * lags[min(len(lags) - 1, int(v * len(lags)))] if lags else None

        since = [c.stats.connected_since for c in self.conns]
        return {"connected": bool(self.conns) and all(t is not None for t in since),
                "connected_since": max((t for t in since if t), default=None),
                "messages": sum(c.stats.messages for c in self.conns), "reconnects": self.reconnects,
                "last_message": max((c.stats.last_message for c in self.conns), default=0.0),
                "lag_p50_ms": q(0.5), "lag_p90_ms": q(0.9), "connections": len(self.conns)}


class PMFeed:
    """Polymarket US books over as many connections as needed, ~1,000 markets each."""

    name = "pmus"
    PER_CONN = _PMConn.CHUNK * _PMConn.MAX_SUBS

    def __init__(self, url: str, signer: PMSigner, on_update: Callable[[str], None]):
        self.url, self.signer, self.on_update = url, signer, on_update
        self.books: dict[str, PMBook] = {}
        self.wanted: set[str] = set()
        self.conns: list[_PMConn] = []
        self.stats = _Combined(self.conns)
        self._added = asyncio.Event()

    def _new_conn(self) -> _PMConn:
        conn = _PMConn(self.url, self.signer, self.books, lambda slug: self.on_update(slug), len(self.conns))
        self.conns.append(conn)
        self._added.set()
        return conn

    def set_markets(self, markets: Iterable[str]) -> None:
        new = set(markets)
        if new == self.wanted:
            return
        self.wanted = new
        todo = sorted(new - {s for c in self.conns for s in c.wanted})
        for c in self.conns:
            keep = c.wanted & new
            space = self.PER_CONN - len(keep)
            c.set_markets(keep | set(todo[:space]))
            todo = todo[space:]
        while todo:
            self._new_conn().set_markets(todo[: self.PER_CONN])
            todo = todo[self.PER_CONN :]

    async def run(self, stop: asyncio.Event) -> None:
        tasks: dict[_PMConn, asyncio.Task] = {}
        stopping = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                for c in self.conns:
                    if c not in tasks:
                        tasks[c] = asyncio.create_task(c.run(stop))
                self._added.clear()
                added = asyncio.create_task(self._added.wait())
                await asyncio.wait({added, stopping}, return_when=asyncio.FIRST_COMPLETED)
                added.cancel()
        finally:
            stopping.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
