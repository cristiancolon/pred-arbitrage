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
    __slots__ = ("yes", "no", "ready", "exch_ts", "recv_ts", "_ladders")

    def __init__(self) -> None:
        self.yes: dict[float, float] = {}  # YES bids: price -> contracts
        self.no: dict[float, float] = {}
        self.ready = False
        self.exch_ts: float | None = None
        self.recv_ts = 0.0
        self._ladders: tuple[list[Level], list[Level]] | None = None

    def snapshot(self, msg: dict, recv: float) -> None:
        self.yes = _levels(msg.get("yes_dollars_fp"))
        self.no = _levels(msg.get("no_dollars_fp"))
        self.ready, self.recv_ts, self._ladders = True, recv, None

    def delta(self, msg: dict, recv: float) -> None:
        side = self.yes if msg.get("side") == "yes" else self.no
        p = round(float(msg["price_dollars"]), 4)
        q = side.get(p, 0.0) + float(msg["delta_fp"])
        if q > 1e-9:
            side[p] = q
        else:
            side.pop(p, None)
        ts_ms = msg.get("ts_ms")
        self.exch_ts = ts_ms / 1000 if ts_ms else self.exch_ts
        self.recv_ts, self._ladders = recv, None

    def ladders(self) -> tuple[list[Level], list[Level]]:
        """(yes_asks, no_asks): buying YES lifts NO bids and vice versa."""
        if self._ladders is None:
            self._ladders = (_complement(self.no), _complement(self.yes))
        return self._ladders


class PMBook:
    __slots__ = ("yes_asks", "no_asks", "state", "ready", "exch_ts", "recv_ts")

    def __init__(self) -> None:
        self.yes_asks: list[Level] = []
        self.no_asks: list[Level] = []
        self.state = ""
        self.ready = False
        self.exch_ts: float | None = None
        self.recv_ts = 0.0

    def update(self, md: dict, recv: float) -> None:
        self.yes_asks, self.no_asks = pmus_ladders(md)
        self.state = md.get("state") or self.state
        t = md.get("transactTime")
        if t:
            try:
                self.exch_ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
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
    name = "kalshi"
    CHUNK = 200  # markets per orderbook subscription

    def __init__(self, url: str, signer: KalshiSigner, on_update: Callable[[str], None],
                 on_lifecycle: Callable[[dict], None] | None = None):
        super().__init__(url, on_update)
        self.signer = signer
        self.on_lifecycle = on_lifecycle
        self.books: dict[str, KalshiBook] = {}
        self._id = 0
        self._pending: dict[int, tuple[str, list[str]]] = {}  # command id -> (kind, tickers)
        self._sid_markets: dict[int, set[str]] = {}
        self._ticker_sid: dict[str, int] = {}
        self._seq: dict[int, int] = {}
        self.rejected: set[str] = set()  # tickers Kalshi refused to subscribe to on their own

    def _headers(self) -> dict[str, str]:
        return self.signer.headers("GET", KALSHI_WS_PATH)

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _on_connect(self) -> None:
        self._pending.clear()
        self._sid_markets.clear()
        self._ticker_sid.clear()
        self._seq.clear()
        if self.on_lifecycle:
            cid = self._next_id()
            self._pending[cid] = ("lifecycle", [])
            await self._send({"id": cid, "cmd": "subscribe", "params": {"channels": ["market_lifecycle_v2"]}})
        await self._sync()

    async def _subscribe(self, tickers: list[str], size: int | None = None) -> None:
        size = size or self.CHUNK
        for i in range(0, len(tickers), size):
            chunk = tickers[i : i + size]
            cid = self._next_id()
            self._pending[cid] = ("book", chunk)
            for t in chunk:
                self._ticker_sid[t] = -cid  # subscribing
            await self._send({"id": cid, "cmd": "subscribe",
                              "params": {"channels": ["orderbook_delta"], "market_tickers": chunk}})

    async def _sync(self) -> None:
        if self.ws is None:
            return
        add = sorted(t for t in self.wanted if t not in self._ticker_sid and t not in self.rejected)
        drop = [t for t in self._ticker_sid if t not in self.wanted and self._ticker_sid[t] > 0]
        by_sid: dict[int, list[str]] = {}
        for t in drop:
            by_sid.setdefault(self._ticker_sid.pop(t), []).append(t)
            self.books.pop(t, None)
        for sid, ts in by_sid.items():
            self._sid_markets[sid] -= set(ts)
            await self._send({"id": self._next_id(), "cmd": "update_subscription",
                              "params": {"sids": [sid], "market_tickers": ts, "action": "delete_markets"}})
        if add:
            await self._subscribe(add)

    async def _resubscribe(self, sid: int) -> None:
        tickers = sorted(self._sid_markets.pop(sid, set()))
        self._seq.pop(sid, None)
        for t in tickers:
            self._ticker_sid.pop(t, None)
            if t in self.books:
                self.books[t].ready = False
                self.on_update(t)
        await self._send({"id": self._next_id(), "cmd": "unsubscribe", "params": {"sids": [sid]}})
        await self._subscribe([t for t in tickers if t in self.wanted])

    def _check_seq(self, msg: dict) -> bool:
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or seq is None:
            return True
        last = self._seq.get(sid)
        self._seq[sid] = seq
        if last is not None and seq != last + 1:
            log.warning("kalshi feed: sequence gap on subscription %s (%s -> %s); resubscribing", sid, last, seq)
            asyncio.get_running_loop().create_task(self._resubscribe(sid))
            return False
        return True

    def _handle(self, msg: dict, recv: float) -> None:
        kind = msg.get("type")
        if kind == "orderbook_delta":
            if not self._check_seq(msg):
                return
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
            self._check_seq(msg)
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
            kind_, tickers = self._pending.pop(msg.get("id"), ("", []))
            sid = (msg.get("msg") or {}).get("sid")
            if kind_ == "book" and sid is not None:
                self._sid_markets[sid] = set(tickers)
                for t in tickers:
                    self._ticker_sid[t] = sid
        elif kind == "error":
            err = msg.get("msg") or {}
            kind_, tickers = self._pending.pop(msg.get("id"), ("", []))
            if err.get("code") == 26 and kind_ == "book" and len(tickers) > 1:
                # Too many markets for one subscription: split it.
                self.CHUNK = max(1, len(tickers) // 2)
                log.info("kalshi feed: subscription market limit hit; using %d per subscription", self.CHUNK)
                for t in tickers:
                    self._ticker_sid.pop(t, None)
                self._changed.set()
            elif kind_ == "book" and tickers:
                for t in tickers:
                    self._ticker_sid.pop(t, None)
                if len(tickers) == 1:
                    self.rejected.add(tickers[0])
                    log.warning("kalshi feed: can't subscribe to %s (%s: %s)", tickers[0], err.get("code"), err.get("msg"))
                else:
                    # Find the market(s) Kalshi objects to by splitting the batch.
                    half = len(tickers) // 2
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._subscribe(tickers[:half], half))
                    loop.create_task(self._subscribe(tickers[half:], len(tickers) - half))
            else:
                log.warning("kalshi feed error %s: %s", err.get("code"), err.get("msg"))

    def _disconnected(self) -> None:
        for t, book in self.books.items():
            if book.ready:
                book.ready = False
                self.on_update(t)


class PMFeed(_Feed):
    name = "pmus"
    CHUNK = 100  # the documented per-subscription maximum

    def __init__(self, url: str, signer: PMSigner, on_update: Callable[[str], None]):
        super().__init__(url, on_update)
        self.signer = signer
        self.books: dict[str, PMBook] = {}
        self._subs: dict[str, list[str]] = {}  # request id -> slugs
        self._slug_sub: dict[str, str] = {}
        self._n = 0

    def _headers(self) -> dict[str, str]:
        return self.signer.headers("GET", PM_WS_PATH)

    async def _on_connect(self) -> None:
        self._subs.clear()
        self._slug_sub.clear()
        await self._sync()

    async def _sync(self) -> None:
        if self.ws is None:
            return
        # Subscriptions can't be edited, so drop any that hold unwanted slugs and
        # re-add their wanted ones alongside new slugs.
        stale = [rid for rid, slugs in self._subs.items() if any(s not in self.wanted for s in slugs)]
        readd = []
        for rid in stale:
            for s in self._subs.pop(rid):
                self._slug_sub.pop(s, None)
                if s in self.wanted:
                    readd.append(s)
                else:
                    self.books.pop(s, None)
            await self._send({"unsubscribe": {"requestId": rid}})
        add = sorted(set(readd) | {s for s in self.wanted if s not in self._slug_sub})
        for i in range(0, len(add), self.CHUNK):
            chunk = add[i : i + self.CHUNK]
            self._n += 1
            rid = f"md-{self._n}"
            self._subs[rid] = chunk
            for s in chunk:
                self._slug_sub[s] = rid
            await self._send({"subscribe": {"requestId": rid, "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                            "marketSlugs": chunk}})

    def _handle(self, msg: dict, recv: float) -> None:
        md = msg.get("marketData")
        if md is not None:
            slug = md.get("marketSlug")
            if slug not in self.wanted:
                return
            book = self.books.setdefault(slug, PMBook())
            book.update(md, recv)
            if book.exch_ts:
                self.stats.lags.append(recv - book.exch_ts)
            self.on_update(slug)
        elif "error" in msg:
            log.warning("pmus feed error for %s: %s", msg.get("requestId") or msg.get("request_id"), msg["error"])

    def _disconnected(self) -> None:
        for s, book in self.books.items():
            if book.ready:
                book.ready = False
                self.on_update(s)
