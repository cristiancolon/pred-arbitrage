"""How long a real order would take, measured without placing one.

An order decided at local time T reaches venue v at exchange time T + s, where s is
the one-way trip to v's trading API (about half its round trip). The book it meets
there is the one we will have *seen* at local time T + s + f, where f is how far our
feed runs behind the exchange (exchange timestamp -> received, measured on every
update). So the paper trader waits ``look = c + s + f`` after deciding (c is the time
we took to decide) and fills against the live book at that moment; it learns the
result a full round trip after sending (``reply = c + rtt``).

Both inputs are measured continuously from this machine:

- f per venue, from the feeds' own timestamps (median of the recent updates);
- rtt per venue, by ``LatencyProbe`` timing signed, read-only requests on the same
  host and keep-alive connection an order would use: Kalshi's balance endpoint, and
  Polymarket US's order *preview*, which runs an order through validation and returns
  the would-be order without creating it. Each simulated order draws a recent round
  trip at random, so network jitter shows up in the results.

Neither probe can place an order: they only ever call ``KALSHI_PROBE_PATH`` (GET)
and ``PM_PREVIEW_PATH``.
"""

import asyncio
import logging
import random
import statistics
import time
from collections import deque
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

KALSHI_PROBE_PATH = "/portfolio/balance"
PM_PREVIEW_PATH = "/v1/order/preview"
# Until the first measurements arrive (measured from a home connection on the East Coast).
DEFAULT_RTT_S = {"K": 0.085, "P": 0.11}
DEFAULT_FEED_S = {"K": 0.05, "P": 0.13}


@dataclass(frozen=True)
class OrderDelay:
    look: float  # seconds after deciding: fill against the book as seen now
    reply: float  # seconds after deciding: the fill report is back


class LatencyModel:
    FEED_CACHE_S = 5.0

    def __init__(self, feed_lags=None, rng: random.Random | None = None):
        # feed_lags(venue) -> recent exchange-to-received lags in seconds (the feeds' FeedStats)
        self.feed_lags = feed_lags or (lambda venue: ())
        self.rtt: dict[str, deque[float]] = {"K": deque(maxlen=300), "P": deque(maxlen=300)}
        self.rng = rng or random.Random()
        self._feed: dict[str, tuple[float, float]] = {}  # venue -> (computed at, median)

    def feed_s(self, venue: str) -> float:
        now = time.monotonic()
        hit = self._feed.get(venue)
        if hit is None or now - hit[0] > self.FEED_CACHE_S:
            lags = list(self.feed_lags(venue))
            hit = (now, statistics.median(lags) if lags else DEFAULT_FEED_S[venue])
            self._feed[venue] = hit
        return hit[1]

    def rtt_s(self, venue: str) -> float:
        xs = self.rtt[venue]
        return self.rng.choice(xs) if xs else DEFAULT_RTT_S[venue]

    def delay(self, venue: str, decide_s: float = 0.0) -> OrderDelay:
        rtt = self.rtt_s(venue)
        return OrderDelay(look=decide_s + rtt / 2 + max(self.feed_s(venue), 0.0), reply=decide_s + rtt)

    def snapshot(self) -> dict:
        out = {}
        for v in ("K", "P"):
            xs = sorted(self.rtt[v])
            rtt50 = xs[len(xs) // 2] if xs else None
            rtt90 = xs[int(0.9 * (len(xs) - 1))] if xs else None
            f = self.feed_s(v)
            out[v] = {"feed_ms": 1000 * f, "rtt_p50_ms": rtt50 and 1000 * rtt50, "rtt_p90_ms": rtt90 and 1000 * rtt90,
                      "samples": len(xs), "order_ms": 1000 * (f + (rtt50 or DEFAULT_RTT_S[v]) / 2)}
        return out


class LatencyProbe:
    """Times signed read-only requests to each venue's trading API every ``every_s``."""

    def __init__(self, model: LatencyModel, kalshi_base: str, kalshi_signer, pm_base: str, pm_signer,
                 slug_fn, every_s: float = 15.0):
        self.model = model
        self.kalshi_url = kalshi_base.rstrip("/") + KALSHI_PROBE_PATH
        self.kalshi_path = httpx.URL(self.kalshi_url).path
        self.pm_url = pm_base.rstrip("/") + PM_PREVIEW_PATH
        self.ks, self.ps = kalshi_signer, pm_signer
        self.slug_fn = slug_fn  # any open Polymarket US market slug, or None
        self.every_s = every_s
        self.errors = 0

    def preview_body(self, slug: str) -> dict:
        # A 1-contract buy at 1c, immediate-or-cancel: the preview endpoint only
        # validates it. The request goes to PM_PREVIEW_PATH and nowhere else.
        return {"request": {"marketSlug": slug, "type": "ORDER_TYPE_LIMIT",
                            "price": {"value": "0.01", "currency": "USD"}, "quantity": 1,
                            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "intent": "ORDER_INTENT_BUY_LONG",
                            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}}

    async def _time(self, client: httpx.AsyncClient, venue: str, method: str, url: str, headers: dict,
                    body: dict | None = None) -> None:
        t = time.perf_counter()
        try:
            r = await client.request(method, url, headers=headers, json=body)
        except httpx.HTTPError as e:
            self.errors += 1
            log.debug("latency probe %s failed: %s", venue, e)
            return
        dt = time.perf_counter() - t
        if r.status_code < 500:  # a 4xx still made the full trip through the gateway
            self.model.rtt[venue].append(dt)
        else:
            self.errors += 1

    async def run(self, stop: asyncio.Event) -> None:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "arbscan/0.1"}) as client:
            first = True
            while not stop.is_set():
                slug = self.slug_fn()
                calls = [self._time(client, "K", "GET", self.kalshi_url, self.ks.headers("GET", self.kalshi_path))]
                if slug:
                    calls.append(self._time(client, "P", "POST", self.pm_url, self.ps.headers("POST", PM_PREVIEW_PATH),
                                            self.preview_body(slug)))
                await asyncio.gather(*calls)
                if first:  # the first request paid for the TLS handshake; a live bot keeps connections warm
                    for v in ("K", "P"):
                        if self.model.rtt[v]:
                            self.model.rtt[v].pop()
                    first = False
                    continue
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.every_s)
                except asyncio.TimeoutError:
                    pass
