"""The streaming scanner: event-driven pricing on live order books.

Both venues' WebSocket feeds (feeds.py) keep every paired market's book in memory.
Whenever a message changes a book, the pairs that use that market are re-priced on
the spot: top-of-book edge in both directions, and when it's positive a walk of both
full ladders, which are already local, so no depth request is needed. The time from
the exchange's own timestamp to the finished computation is recorded as the
end-to-end latency.

Market metadata the feeds don't carry (Kalshi fee multipliers and resolution times,
Polymarket US fee coefficients and closed flags) still comes from REST every
``meta_refresh_s``; Kalshi's lifecycle channel reports pauses and settlements as they
happen, and Polymarket US reports its trading state in every book message.

Once a second the scanner commits to SQLite, writes a summary row to ``sweeps``,
and notifies the dashboard, so neither slows the feed.

With ``paper_trading`` on, every pick is also handed to the paper trader (paper.py),
which simulates acting on it with this machine's measured order latency.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse

from .arb import Leg, directions, top_edge, walk
from .book import top_n
from .config import Config
from .dbwriter import DbWriter
from .feeds import KalshiFeed, PMFeed
from .latency import LatencyModel, LatencyProbe
from .paper import PaperTrader
from .results import UPSERT, kalshi_lifecycle_row, lookup
from .pairs import Pair
from .scanner import KALSHI_FINISHED, PMUS_DEFAULT_COEF, Episodes, Scanner
from .venues import Kalshi, PolymarketUS

log = logging.getLogger(__name__)

TICK_S = 1.0
SETTLE_EVERY_S = 60.0  # how often paper positions check for published results
OPPORTUNITY_EVERY_S = 1.0  # at most one stored depth snapshot per pair/direction per second
LIFECYCLE_STATUS = {"deactivated": "inactive", "activated": "active", "determined": "determined",
                    "settled": "settled"}


class LiveScanner(Scanner):
    mode = "stream"

    def __init__(self, cfg: Config, db, kalshi: Kalshi, pm: PolymarketUS, kfeed: KalshiFeed, pfeed: PMFeed):
        super().__init__(cfg, db, kalshi, pm)
        # Recordings go through a writer thread so SQLite never blocks the feeds;
        # ``db`` stays for reads.
        self.out = DbWriter(cfg.db_path)
        self.episodes = Episodes(self.out, reader=db)
        self.kfeed, self.pfeed = kfeed, pfeed
        kfeed.on_update, kfeed.on_lifecycle, pfeed.on_update = self._on_kalshi, self._on_lifecycle, self._on_pm
        self.by_ticker: dict[str, list[Pair]] = defaultdict(list)
        self.by_slug: dict[str, list[Pair]] = defaultdict(list)
        self.pm_coef: dict[str, float] = {}
        self.pm_closed: set[str] = set()
        self.pm_meta_ts = 0.0
        self.last_opp: dict[tuple[str, str], float] = {}
        self._errors_seen = 0
        self._meta_pending = False  # pairs changed while a metadata refresh was running
        self.latency = LatencyModel(self._feed_lags)
        self.paper = (PaperTrader(cfg, db, self.out, self.latency, kfeed.books, pfeed.books, self.kmeta)
                      if cfg.paper_trading else None)
        self._reset_window()

    def _feed_lags(self, venue: str):
        if venue == "K":
            return self.kfeed.stats.lags
        return [x for c in self.pfeed.conns for x in c.stats.lags]

    # --- bookkeeping ------------------------------------------------------------

    def _reset_window(self) -> None:
        self.w_evals = 0
        self.w_lags: list[float] = []
        self.w_best: tuple[float, str, str] | None = None
        self.w_t0 = time.monotonic()

    def _active_pairs(self) -> list[Pair]:
        return [p for p in self.pairs.pairs if p.id not in self.finished]

    def _reindex(self) -> None:
        self.by_ticker.clear()
        self.by_slug.clear()
        for p in self._active_pairs():
            self.by_ticker[p.kalshi].append(p)
            self.by_slug[p.pm].append(p)
        known = {p.id for p in self.pairs.pairs}
        for pid in [p for p in self.pair_state if p not in known]:
            del self.pair_state[pid]
        self.kfeed.set_markets(self.by_ticker)
        self.pfeed.set_markets(self.by_slug)

    async def refresh_meta(self, force: bool = False) -> None:
        tickers = sorted(self.by_ticker)
        stale = force or time.monotonic() - self.meta_ts > self.cfg.meta_refresh_s
        new = [t for t in tickers if t not in self.kmeta]
        if tickers and (stale or new):
            await self.refresh_kalshi_meta(tickers if stale else new)
        slugs = sorted(self.by_slug)
        stale_p = force or time.monotonic() - self.pm_meta_ts > self.cfg.meta_refresh_s
        todo = slugs if stale_p else [s for s in slugs if s not in self.pm_coef]
        if todo:
            ms = await self.pm.markets(todo)
            for s in todo:
                m = ms.get(s)
                if m is None or m.get("closed") or "RESOLVED" in (m.get("status") or ""):
                    self.pm_closed.add(s)
                else:
                    self.pm_closed.discard(s)
                if m is not None:
                    self.pm_coef[s] = float(m.get("feeCoefficient") or PMUS_DEFAULT_COEF)
        if stale_p:
            self.pm_meta_ts = time.monotonic()
        self._retire_finished()

    def _retire_finished(self) -> None:
        ts = time.time()
        done = [p for p in self._active_pairs()
                if (self.kmeta.get(p.kalshi) and self.kmeta[p.kalshi].status in KALSHI_FINISHED)
                or p.pm in self.pm_closed]
        for p in done:
            self.finished.add(p.id)
            self.episodes.close_pair(p.id, ts)
            self._set_state(p, ts, "finished", edges={})
        if done:
            log.info("%d pairs finished (a market closed); unsubscribing", len(done))
            self._reindex()

    # --- feed callbacks (the hot path) --------------------------------------------

    def _on_kalshi(self, ticker: str) -> None:
        book = self.kfeed.books.get(ticker)
        seen = book.recv_ts if book is not None else None
        for p in self.by_ticker.get(ticker, ()):
            self._evaluate(p, seen)
        self._lag(book)

    def _on_pm(self, slug: str) -> None:
        book = self.pfeed.books.get(slug)
        seen = book.recv_ts if book is not None else None
        for p in self.by_slug.get(slug, ()):
            self._evaluate(p, seen)
        self._lag(book)

    def _lag(self, book) -> None:
        """Exchange timestamp of the change just handled -> its pairs finished pricing."""
        if book is not None and book.stamped and book.exch_ts:
            self.w_lags.append(time.time() - book.exch_ts)

    def _on_lifecycle(self, msg: dict) -> None:
        t = msg.get("market_ticker")
        row = kalshi_lifecycle_row(msg, time.time())
        if row is not None:  # a result, for judging trades later (results.py)
            self.out.execute(UPSERT, row)
        km = self.kmeta.get(t)
        if km is None:
            return
        status = LIFECYCLE_STATUS.get(msg.get("event_type") or "")
        if status:
            km.status = status
        if msg.get("event_type") == "close_date_updated" and msg.get("close_ts"):
            km.resolve_ts = float(msg["close_ts"])
        if km.status in KALSHI_FINISHED:
            self._retire_finished()
        else:
            self._on_kalshi(t)

    def _evaluate(self, pair: Pair, seen: float | None = None) -> None:
        """Re-price a pair. ``seen``: when the update that triggered this arrived (None
        when nothing new arrived, e.g. after a metadata refresh)."""
        ts = time.time()
        km = self.kmeta.get(pair.kalshi)
        kb = self.kfeed.books.get(pair.kalshi)
        pb = self.pfeed.books.get(pair.pm)
        if (km is None or km.status != "active" or kb is None or not kb.ready or pb is None or not pb.ready
                or not pb.open or pair.id in self.finished):
            self.episodes.close_pair(pair.id, ts)
            if pair.id not in self.finished:
                reason = self._pause_reason(km, kb, pb)
                st = self.pair_state.get(pair.id, {})
                if st.get("status") != "paused" or st.get("reason") != reason:
                    self._set_state(pair, ts, "paused", edges={}, reason=reason)
            return

        k_yes, k_no = kb.ladders()
        p_yes, p_no = pb.yes_asks, pb.no_asks
        p_coef = self.pm_coef.get(pair.pm, PMUS_DEFAULT_COEF) * (1 - self.cfg.pmus_taker_rebate)
        days = (km.resolve_ts - ts) / 86400 if km.resolve_ts else None
        edges = []
        for label, k_side, p_side in directions(pair.relation):
            kl = k_yes if k_side == "yes" else k_no
            pl = p_yes if p_side == "yes" else p_no
            e = top_edge(kl[0][0] if kl else None, km.fee_coef, pl[0][0] if pl else None, p_coef)
            edges.append(e)
            key = (pair.id, label)
            if e is not None and (self.w_best is None or e > self.w_best[0]):
                self.w_best = (e, pair.id, label)
            if e is None or e <= self.cfg.depth_trigger_edge:
                self.episodes.observe(key, ts, None, days)
                continue
            res = walk(Leg(kl, km.fee_coef), Leg(pl, p_coef), self.cfg.min_edge,
                       budget_a=self.cfg.leg_budget, budget_b=self.cfg.leg_budget)
            if res.positive and ts - self.last_opp.get(key, 0.0) >= OPPORTUNITY_EVERY_S:
                self.last_opp[key] = ts
                n = self.cfg.book_levels_stored
                self.out.execute(
                    "INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, pair.id, label, res.top_edge, res.size, res.cost, res.profit, res.last_edge,
                     days, json.dumps(top_n(kl, n)), json.dumps(top_n(pl, n))),
                )
            self.episodes.observe(key, ts, res, days)
            if self.paper is not None and res.positive:
                ep = self.episodes.open.get(key)
                self.paper.consider(pair, label, k_side, p_side, kl, pl, km.fee_coef, p_coef, days,
                                    ep.start_ts if ep else ts, seen if seen is not None else ts, res)

        p_bid = round(1 - p_no[0][0], 4) if p_no else None
        p_ask = p_yes[0][0] if p_yes else None
        self._record_quote(pair.id, ts, k_yes, k_no, p_bid, p_ask, edges)
        self._set_state(pair, ts, "live", k_yes_ask=k_yes[0][0] if k_yes else None,
                        k_no_ask=k_no[0][0] if k_no else None, p_bid=p_bid, p_ask=p_ask, days=days,
                        edges={label: e for (label, _, _), e in zip(directions(pair.relation), edges)},
                        reason=None)
        self.w_evals += 1

    @staticmethod
    def _pause_reason(km, kb, pb) -> str:
        if km is None:
            return "loading Kalshi market"
        if km.status != "active":
            return f"Kalshi market {km.status}"
        if kb is None or not kb.ready:
            return "waiting for Kalshi book"
        if pb is None or not pb.ready:
            return "waiting for Polymarket book"
        if not pb.open:
            return f"Polymarket {pb.state.replace('MARKET_STATE_', '').lower()}"
        return ""

    # --- periodic work ---------------------------------------------------------------

    def _tick(self) -> None:
        """Once a second: persist, summarize, and notify the dashboard."""
        ts = time.time()
        lags = sorted(self.w_lags)
        lag_ms = int(1000 * lags[len(lags) // 2]) if lags else None
        live = sum(1 for s in self.pair_state.values() if s.get("status") == "live")
        best = self.w_best
        errors = self.kfeed.stats.reconnects + self.pfeed.stats.reconnects
        new_errors = errors - self._errors_seen
        self._errors_seen = errors
        self.out.execute("INSERT INTO sweeps (ts, n_pairs, dur_ms, depth_fetches, errors, best_edge, best_pair, "
                         "best_dir) VALUES (?,?,?,?,?,?,?,?)",
                         (ts, live, lag_ms, self.w_evals, new_errors, *(best if best else (None, None, None))))
        self.out.commit()
        self.sweep_count += 1
        self.last_sweep = {"ts": ts, "dur_ms": lag_ms, "n_pairs": live, "depth_fetches": self.w_evals,
                           "errors": new_errors, "best_edge": best[0] if best else None,
                           "best_pair": best[1] if best else None, "best_dir": best[2] if best else None,
                           "lag_p90_ms": int(1000 * lags[int(0.9 * (len(lags) - 1))]) if lags else None,
                           "db_backlog": self.out.backlog}
        self._reset_window()
        for fn in self.listeners:
            try:
                fn()
            except Exception:
                log.exception("tick listener failed")

    async def _refresh_meta_then_price(self) -> None:
        try:
            await self.refresh_meta()
            for p in self._active_pairs():
                self._evaluate(p)  # new pairs can be priced once their metadata is in
        except Exception as e:
            log.warning("metadata refresh failed: %s", e)
            self.last_error = {"ts": time.time(), "message": f"metadata refresh: {e}"}
            self.meta_ts = self.pm_meta_ts = time.monotonic()  # retry after the usual interval
        finally:
            self._meta_task = None

    def _probe_slug(self) -> str | None:
        """An open Polymarket US market to preview orders on (for the latency probe)."""
        for slug, book in self.pfeed.books.items():
            if book.ready and book.open:
                return slug
        return None

    async def _settle_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=SETTLE_EVERY_S)
            except asyncio.TimeoutError:
                pass
            try:
                self.paper.settle(lambda keys: lookup(self.db, keys), self.finished)
            except Exception as e:
                log.warning("paper settlement check failed: %s", e)

    def feed_state(self) -> dict:
        return {"kalshi": self.kfeed.stats.snapshot(), "pmus": self.pfeed.stats.snapshot()}

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("streaming scanner started")
        self.pairs.refresh()
        self._reindex()
        tasks = [asyncio.create_task(self.kfeed.run(stop)), asyncio.create_task(self.pfeed.run(stop))]
        if self.paper is not None:
            # Orders go to the same host as the authenticated feed (api.polymarket.us).
            pm_api = "https://" + urlparse(self.cfg.pmus_ws_url).netloc
            probe = LatencyProbe(self.latency, self.cfg.kalshi_base, self.kfeed.signer, pm_api, self.pfeed.signer,
                                 self._probe_slug, self.cfg.latency_probe_s)
            tasks.append(asyncio.create_task(probe.run(stop)))
            tasks.append(asyncio.create_task(self._settle_loop(stop)))
        try:
            await self.refresh_meta(force=True)
        except Exception as e:
            log.exception("initial metadata refresh failed")
            self.last_error = {"ts": time.time(), "message": f"{type(e).__name__}: {e}"}
        # Books that arrived before their metadata can be priced now.
        for p in self._active_pairs():
            self._evaluate(p)
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=TICK_S)
                except asyncio.TimeoutError:
                    pass
                try:
                    changed = self.pairs.refresh()
                    if changed:
                        self._reindex()
                    stale = time.monotonic() - min(self.meta_ts, self.pm_meta_ts) > self.cfg.meta_refresh_s
                    self._meta_pending |= changed
                    if (self._meta_pending or stale) and self._meta_task is None:
                        self._meta_pending = False
                        # REST metadata in the background: the tick (commits, dashboard)
                        # must keep its one-second rhythm.
                        self._meta_task = asyncio.create_task(self._refresh_meta_then_price())
                    self._tick()
                except Exception as e:
                    log.exception("scanner housekeeping failed")
                    self.last_error = {"ts": time.time(), "message": f"{type(e).__name__}: {e}"}
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            self.episodes.close_all()
            self.out.close()
            log.info("streaming scanner stopped")
