"""Live discovery: new markets matched, reviewed and paired within seconds of listing.

The hourly refresh (catalog -> match -> review) rebuilds everything from scratch.
Between refreshes this process keeps both venues' markets in a resident index
(``match.LiveIndex``) and asks each venue for markets created since its last check:
Kalshi every ``kalshi_discovery_s`` (``GET /markets?min_created_ts=``) and Polymarket
US every ``pmus_discovery_s`` (``GET /v1/markets?startDateMin=``; a market's
``startDate`` is when it was listed). Each new market goes into the catalog, is
matched against the other venue, and its suggestions go through the auto-approve
rules and Jev like any others. Approved pairs are appended to pairs.csv, which the
scanner picks up within a second.

It runs as its own low-priority process (``arbscan discover``, kept alive by
``arbscan serve``), so building the index and matching never compete with the
price feeds. After each full catalog refresh it rebuilds the index.
"""

import asyncio
import logging
import signal
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone

from . import jev
from .catalog import ROW_SQL, kalshi_row, pm_row
from .config import Config
from .fees import kalshi_taker_coef
from .http import Api, ApiError, make_client
from .match import Candidate, LiveIndex, auto_approve
from .scanner import parse_ts
from .venues import Kalshi, PolymarketUS

log = logging.getLogger(__name__)

# Column names of a ``markets`` row, in table order (see catalog.ROW_SQL).
COLS = ("venue", "id", "event_id", "series", "category", "title", "yes_label", "no_label", "market_type",
        "start_ts", "close_ts", "rules", "fee_coef", "yes_bid", "yes_ask", "volume", "updated")
OVERLAP_S = 120  # re-ask a little before the last listing seen, in case of clock or indexing lag
KALSHI_LISTABLE = ("active", "initialized")  # "initialized": listed, opens for trading later
SUMMARY_EVERY_S = 600


class Discovery:
    def __init__(self, cfg: Config, db: sqlite3.Connection, kalshi: Kalshi, pm: PolymarketUS):
        self.cfg, self.db, self.kalshi, self.pm = cfg, db, kalshi, pm
        self.index: LiveIndex | None = None
        self.marks = self._catalog_marks()
        self._pending_marks = self.marks
        built = min((m for m in self.marks if m), default=None)
        # Start just before the last full catalog: anything listed since is new to us.
        self.k_since = self.p_since = (built or time.time() - 3600) - 600
        self.known: dict[str, set[str]] = {"K": set(), "P": set()}
        self.events: dict[str, dict] = {}
        self.series_fee: dict[str, float] = {}
        self.window: Counter[str] = Counter()

    def _catalog_marks(self) -> tuple:
        """When each venue's catalog was last rebuilt: every row a refresh writes gets
        the same timestamp and older rows are deleted, so the minimum marks it."""
        return tuple(self.db.execute("SELECT MIN(updated) FROM markets WHERE venue = ?", (v,)).fetchone()[0]
                     for v in ("K", "P"))

    def build(self) -> None:
        self.index = LiveIndex(self.db)
        for v in ("K", "P"):
            self.known[v] = {r[0] for r in self.db.execute("SELECT id FROM markets WHERE venue = ?", (v,))}

    # --- Kalshi ------------------------------------------------------------------------

    async def _event(self, ticker: str) -> dict | None:
        if ticker not in self.events:
            try:
                self.events[ticker] = await self.kalshi.event(ticker)
            except ApiError as e:
                log.warning("Kalshi event %s: %s", ticker, e)
                return None
        return self.events[ticker]

    async def _fee(self, series: str | None) -> float:
        if series not in self.series_fee:
            row = self.db.execute("SELECT fee_coef FROM markets WHERE venue = 'K' AND series = ? "
                                  "AND fee_coef IS NOT NULL LIMIT 1", (series,)).fetchone()
            if row:
                self.series_fee[series] = row[0]
            else:
                try:
                    s = await self.kalshi.series(series) if series else {}
                except ApiError:
                    s = {}
                self.series_fee[series] = kalshi_taker_coef(s.get("fee_type"), s.get("fee_multiplier"))
        return self.series_fee[series]

    async def poll_kalshi(self) -> list[tuple]:
        ms = await self.kalshi.created_markets(int(self.k_since) - OVERLAP_S)
        now = time.time()
        horizon = now + self.cfg.catalog_horizon_days * 86400
        rows = []
        for m in ms:
            created = parse_ts(m.get("created_time"))
            if created:
                self.k_since = max(self.k_since, created)
            if m["ticker"] in self.known["K"] or m.get("status") not in KALSHI_LISTABLE:
                continue
            ev = await self._event(m["event_ticker"])
            if ev is None:
                continue
            row = kalshi_row(ev, m, await self._fee(ev.get("series_ticker")), now)
            self.known["K"].add(m["ticker"])
            if row[10] is None or row[10] <= horizon:
                rows.append((row, created))
        return rows

    # --- Polymarket US -----------------------------------------------------------------

    async def poll_pm(self) -> list[tuple]:
        since = datetime.fromtimestamp(self.p_since - OVERLAP_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ms = await self.pm.listed_since(since)
        now = time.time()
        horizon = now + self.cfg.catalog_horizon_days * 86400
        rows = []
        for m in ms:
            listed = parse_ts(m.get("startDate"))
            if listed:
                self.p_since = max(self.p_since, listed)
            if m["slug"] in self.known["P"] or m.get("closed"):
                continue
            row = pm_row(m, now)
            self.known["P"].add(m["slug"])
            if row[10] is None or row[10] <= horizon:
                rows.append((row, listed))
        return rows

    # --- matching and review -----------------------------------------------------------

    def match(self, venue: str, rows: list[tuple]) -> list[Candidate]:
        """Add new markets to the catalog and index; store their suggestions."""
        if not rows:
            return []
        self.db.executemany(ROW_SQL, [r for r, _ in rows])
        now = time.time()
        cands: list[Candidate] = []
        for row, listed in rows:
            r = dict(zip(COLS, row))
            found = (self.index.add_kalshi(r, self.cfg.match_min_score) if venue == "K"
                     else self.index.add_pm(r, self.cfg.match_min_score))
            cands += found
            self.db.execute("INSERT OR REPLACE INTO discovered VALUES (?,?,?,?,?)",
                            (venue, r["id"], now, listed, len(found)))
            for c in found:
                log.info("new %s market %s ~ %s (%s, score %.2f)", "Kalshi" if venue == "K" else "Polymarket US",
                         r["id"], c.pm if venue == "K" else c.kalshi, c.relation, c.score)
        self.db.executemany("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?)",
                            [(c.kalshi, c.pm, round(c.score, 4), c.relation, int(c.confident), now) for c in cands])
        self.db.commit()
        if cands and self.cfg.auto_approve:
            ks = {c.kalshi for c in cands}
            series = dict(self.db.execute(f"SELECT id, series FROM markets WHERE venue = 'K' AND id IN "
                                          f"({','.join('?' * len(ks))})", sorted(ks)).fetchall())
            auto_approve(self.cfg, self.db, sorted(cands, key=lambda c: -c.score), series)
        self.window[f"{venue}_markets"] += len(rows)
        self.window["suggestions"] += len(cands)
        return cands

    async def review(self, cands: list[Candidate]) -> None:
        if cands and jev.api_key(self.cfg):
            stats = await jev.review(self.cfg, self.db, only={(c.kalshi, c.pm) for c in cands})
            self.window["approved"] += stats["approve"]

    async def step(self, venue: str) -> None:
        rows = await (self.poll_kalshi() if venue == "K" else self.poll_pm())
        await self.review(self.match(venue, rows))

    def maybe_rebuild(self) -> None:
        """Rebuild the index once a full catalog refresh has finished (its marks moved
        and then held still for a check)."""
        marks = self._catalog_marks()
        if marks != self.marks and marks == self._pending_marks:
            log.info("catalog refreshed; rebuilding the live index")
            self.build()
            self.marks = marks
        self._pending_marks = marks

    def summary(self) -> None:
        w = self.window
        log.info("last %d min: %d new Kalshi and %d new Polymarket US markets, %d suggestions, %d approved",
                 SUMMARY_EVERY_S // 60, w["K_markets"], w["P_markets"], w["suggestions"], w["approved"])
        self.window = Counter()

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.build()
        log.info("watching for new markets: Kalshi every %.0fs, Polymarket US every %.0fs",
                 self.cfg.kalshi_discovery_s, self.cfg.pmus_discovery_s)
        due = {"K": 0.0, "P": 0.0, "rebuild": time.monotonic() + 60, "summary": time.monotonic() + SUMMARY_EVERY_S}
        period = {"K": self.cfg.kalshi_discovery_s, "P": self.cfg.pmus_discovery_s}
        while not stop.is_set():
            now = time.monotonic()
            for venue in ("K", "P"):
                if now >= due[venue]:
                    try:
                        await self.step(venue)
                    except (ApiError, OSError) as e:
                        log.warning("%s discovery: %s", "Kalshi" if venue == "K" else "Polymarket US", e)
                    due[venue] = time.monotonic() + period[venue]
            if now >= due["rebuild"]:
                self.maybe_rebuild()
                due["rebuild"] = time.monotonic() + 60
            if now >= due["summary"]:
                self.summary()
                due["summary"] = time.monotonic() + SUMMARY_EVERY_S
            wait = max(0.05, min(due.values()) - time.monotonic())
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass


async def run(cfg: Config, db: sqlite3.Connection, once: bool = False) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with make_client() as client:
        d = Discovery(cfg, db, Kalshi(Api(client, cfg.kalshi_base, cfg.kalshi_rps, "kalshi")),
                      PolymarketUS(Api(client, cfg.pmus_base, cfg.pmus_rps, "pmus")))
        if once:
            d.build()
            for venue in ("K", "P"):
                await d.step(venue)
            d.summary()
            return
        await d.run_forever(stop)
