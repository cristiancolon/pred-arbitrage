"""The polling loop.

Each sweep:
  1. Kalshi: full order books for every paired ticker (100 per request).
  2. Polymarket US: best bid/ask for every paired slug (100 per request).
  3. For each pair and direction, compute the top-of-book edge after fees.
  4. Where that edge is positive, fetch the Polymarket US book and walk both books
     to find how many contracts are profitable and for how much.
  5. Record quotes (on change), opportunities, and open/close episodes.

The scanner also keeps a live, in-memory view (latest quotes per pair, open
opportunities, sweep stats) that the dashboard streams.
"""

import asyncio
import json
import logging
import signal
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from .arb import ArbResult, Leg, directions, top_edge, walk
from .book import Level, kalshi_ladders, pmus_ladders, top_n
from .config import Config
from .fees import kalshi_taker_coef
from .http import Api, ApiError, make_client
from .pairs import Pair, PairFile
from .venues import Kalshi, PolymarketUS, pm_is_open, pm_quote

log = logging.getLogger(__name__)

PMUS_DEFAULT_COEF = 0.0695
MAX_DEPTH_FETCHES_PER_SWEEP = 6
# Kalshi statuses a market never comes back from ("inactive" is only paused).
KALSHI_FINISHED = {"closed", "determined", "finalized", "settled", "missing"}
SERIES_FEE_TTL_S = 24 * 3600


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class KMeta:
    status: str
    resolve_ts: float | None
    fee_coef: float


@dataclass
class Episode:
    start_ts: float
    last_ts: float
    n_obs: int
    max_top_edge: float
    max_profit: float
    max_size: int
    first_profit: float
    cost_at_max: float
    days: float | None
    # Latest observation, for the live view.
    edge: float = 0.0
    profit: float = 0.0
    size: int = 0
    cost: float = 0.0


class Episodes:
    """Tracks runs of consecutive positive observations per (pair, direction)."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.open: dict[tuple[str, str], Episode] = {}

    def observe(self, key: tuple[str, str], ts: float, res: ArbResult | None, days: float | None) -> None:
        if res is None or not res.positive:
            self.close(key, ts)
            return
        edge = res.top_edge or 0.0
        ep = self.open.get(key)
        if ep is None:
            self.open[key] = Episode(ts, ts, 1, edge, res.profit, res.size, res.profit, res.cost, days,
                                     edge, res.profit, res.size, res.cost)
            log.info(
                "OPEN  %s %s  edge %.2fc  size %d  profit $%.2f  resolves in %s",
                key[0], key[1], 100 * edge, res.size, res.profit,
                f"{days:.1f}d" if days is not None else "?",
            )
            return
        ep.last_ts = ts
        ep.n_obs += 1
        ep.max_top_edge = max(ep.max_top_edge, edge)
        if res.profit > ep.max_profit:
            ep.max_profit, ep.cost_at_max = res.profit, res.cost
        ep.max_size = max(ep.max_size, res.size)
        ep.edge, ep.profit, ep.size, ep.cost, ep.days = edge, res.profit, res.size, res.cost, days

    def close(self, key: tuple[str, str], ts: float) -> None:
        ep = self.open.pop(key, None)
        if ep is None:
            return
        self.db.execute(
            "INSERT INTO episodes (pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, "
            "max_size, first_profit, cost_at_max, days_to_resolve) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key[0], key[1], ep.start_ts, ts, ep.n_obs, ep.max_top_edge, ep.max_profit,
             ep.max_size, ep.first_profit, ep.cost_at_max, ep.days),
        )
        log.info("CLOSE %s %s  lasted <=%.0fs  max profit $%.2f", key[0], key[1], ts - ep.start_ts, ep.max_profit)

    def close_pair(self, pair: str, ts: float) -> None:
        for key in [k for k in self.open if k[0] == pair]:
            self.close(key, ts)

    def close_all(self) -> None:
        for key, ep in list(self.open.items()):
            self.close(key, ep.last_ts)

    def snapshot(self) -> list[dict]:
        return [{"pair": k[0], "direction": k[1], **asdict(ep)} for k, ep in self.open.items()]


@dataclass
class _DepthJob:
    pair: Pair
    label: str
    pm_side: str
    k_ladder: list[Level]
    k_coef: float
    p_coef: float
    edge: float
    days: float | None


class Scanner:
    def __init__(self, cfg: Config, db: sqlite3.Connection, kalshi: Kalshi, pm: PolymarketUS):
        self.cfg = cfg
        self.db = db
        self.kalshi = kalshi
        self.pm = pm
        self.pairs = PairFile(cfg.pairs_path)
        self.kmeta: dict[str, KMeta] = {}
        self.event_series: dict[str, str] = {}
        self.series_fee: dict[str, float] = {}
        self.series_fee_ts = 0.0
        self.meta_ts = 0.0
        self.last_quote: dict[str, tuple] = {}
        self.last_depth: dict[str, float] = {}
        self.episodes = Episodes(db)
        self.warned: set[str] = set()
        self.finished: set[str] = set()  # pair ids whose markets have closed
        # Live view for the dashboard.
        self.started = time.time()
        self.sweep_count = 0
        self.last_sweep: dict | None = None
        self.last_error: dict | None = None
        self.pair_state: dict[str, dict] = {}
        self.listeners: list[Callable[[], None]] = []

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)

    async def refresh_kalshi_meta(self, tickers: list[str]) -> None:
        if time.monotonic() - self.series_fee_ts > SERIES_FEE_TTL_S:
            self.series_fee.clear()
            self.series_fee_ts = time.monotonic()
        markets = await self.kalshi.markets(tickers)
        for t in tickers:
            m = markets.get(t)
            if m is None:
                self._warn_once(f"k-missing:{t}", "Kalshi market %s not found; check pairs.csv", t)
                self.kmeta[t] = KMeta("missing", None, 0.0)
                continue
            ev = m["event_ticker"]
            if ev not in self.event_series:
                # The catalog already knows most events' series; ask the API otherwise.
                row = self.db.execute(
                    "SELECT series FROM markets WHERE venue = 'K' AND event_id = ? AND series IS NOT NULL LIMIT 1",
                    (ev,),
                ).fetchone()
                self.event_series[ev] = row[0] if row else (await self.kalshi.event(ev))["series_ticker"]
            series = self.event_series[ev]
            if series not in self.series_fee:
                s = await self.kalshi.series(series)
                self.series_fee[series] = kalshi_taker_coef(s.get("fee_type"), s.get("fee_multiplier"))
            resolve = parse_ts(m.get("expected_expiration_time")) or parse_ts(m.get("close_time"))
            self.kmeta[t] = KMeta(m.get("status") or "", resolve, self.series_fee[series])
        self.meta_ts = time.monotonic()

    def _record_quote(self, pair: str, ts: float, k_yes, k_no, p_bid, p_ask, edges) -> None:
        def top(ladder):
            return (ladder[0][0], round(ladder[0][1], 2)) if ladder else (None, None)

        row = (*top(k_yes), *top(k_no), p_bid, p_ask,
               *(round(e, 6) if e is not None else None for e in edges))
        if self.last_quote.get(pair) == row:
            return
        self.last_quote[pair] = row
        self.db.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?)", (ts, pair, *row))

    def _set_state(self, pair: Pair, ts: float, status: str, **fields) -> None:
        st = self.pair_state.setdefault(pair.id, {})
        st.update(status=status, ts=ts, relation=pair.relation, **fields)

    async def sweep(self) -> None:
        t0 = time.monotonic()
        errors_before = self.kalshi.api.errors + self.pm.api.errors
        if self.pairs.refresh():
            known = {p.id for p in self.pairs.pairs}
            for pid in [p for p in self.pair_state if p not in known]:
                del self.pair_state[pid]
        pairs = [p for p in self.pairs.pairs if p.id not in self.finished]
        if not pairs:
            self._warn_once("no-pairs", "no open pairs in %s; run `arbscan review` or edit it", self.cfg.pairs_path)
            self._finish_sweep(time.time(), t0, 0, 0, errors_before, None, record=False)
            return
        self.warned.discard("no-pairs")

        tickers = sorted({p.kalshi for p in pairs})
        stale = time.monotonic() - self.meta_ts > self.cfg.meta_refresh_s
        new = [t for t in tickers if t not in self.kmeta]
        if stale or new:
            await self.refresh_kalshi_meta(tickers if stale else new)

        live = [t for t in tickers if self.kmeta[t].status == "active"]
        kbooks, pms = await asyncio.gather(
            self.kalshi.orderbooks(live), self.pm.markets(sorted({p.pm for p in pairs}))
        )
        ts = time.time()

        jobs: list[_DepthJob] = []
        newly_finished = 0
        best: tuple[float, str, str] | None = None
        for pair in pairs:
            km = self.kmeta.get(pair.kalshi)
            pm_m = pms.get(pair.pm)
            if pm_m is None:
                self._warn_once(f"p-missing:{pair.pm}", "Polymarket US market %s not found; check pairs.csv", pair.pm)
            if km is None or km.status != "active" or pm_m is None or not pm_is_open(pm_m):
                self.episodes.close_pair(pair.id, ts)
                if (km is not None and km.status in KALSHI_FINISHED) or pm_m is None or pm_m.get("closed") \
                        or "RESOLVED" in (pm_m.get("status") or ""):
                    self.finished.add(pair.id)
                    newly_finished += 1
                    self._set_state(pair, ts, "finished", edges={})
                else:
                    self._set_state(pair, ts, "paused", edges={})
                continue

            k_yes, k_no = kalshi_ladders(kbooks.get(pair.kalshi) or {})
            p_bid, p_ask = pm_quote(pm_m, "bestBidQuote"), pm_quote(pm_m, "bestAskQuote")
            p_coef = float(pm_m.get("feeCoefficient") or PMUS_DEFAULT_COEF) * (1 - self.cfg.pmus_taker_rebate)
            p_top = {"yes": p_ask, "no": (1 - p_bid) if p_bid is not None else None}
            days = (km.resolve_ts - ts) / 86400 if km.resolve_ts else None

            edges = []
            for label, k_side, p_side in directions(pair.relation):
                k_ladder = k_yes if k_side == "yes" else k_no
                e = top_edge(k_ladder[0][0] if k_ladder else None, km.fee_coef, p_top[p_side], p_coef)
                edges.append(e)
                if e is not None and (best is None or e > best[0]):
                    best = (e, pair.id, label)
                if e is not None and e > self.cfg.depth_trigger_edge:
                    jobs.append(_DepthJob(pair, label, p_side, k_ladder, km.fee_coef, p_coef, e, days))
                else:
                    self.episodes.observe((pair.id, label), ts, None, days)
            self._record_quote(pair.id, ts, k_yes, k_no, p_bid, p_ask, edges)
            self._set_state(
                pair, ts, "live",
                k_yes_ask=k_yes[0][0] if k_yes else None, k_no_ask=k_no[0][0] if k_no else None,
                p_bid=p_bid, p_ask=p_ask, days=days,
                edges={label: e for (label, _, _), e in zip(directions(pair.relation), edges)},
            )

        # Depth: least-recently-fetched slugs first so persistent gaps can't starve others.
        slugs = sorted({j.pair.pm for j in jobs}, key=lambda s: self.last_depth.get(s, 0.0))
        slugs = slugs[:MAX_DEPTH_FETCHES_PER_SWEEP]
        results = await asyncio.gather(*(self.pm.book(s) for s in slugs), return_exceptions=True)
        books = {}
        for s, r in zip(slugs, results):
            if isinstance(r, BaseException):
                log.warning("Polymarket US book %s: %s", s, r)
                continue
            books[s] = pmus_ladders(r)
            self.last_depth[s] = time.monotonic()

        for j in jobs:
            if j.pair.pm not in books:
                continue  # not fetched this sweep; keep any open episode as-is
            p_yes, p_no = books[j.pair.pm]
            p_ladder = p_yes if j.pm_side == "yes" else p_no
            res = walk(Leg(j.k_ladder, j.k_coef), Leg(p_ladder, j.p_coef), self.cfg.min_edge)
            if res.positive:
                n = self.cfg.book_levels_stored
                self.db.execute(
                    "INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, j.pair.id, j.label, res.top_edge, res.size, res.cost, res.profit, res.last_edge,
                     j.days, json.dumps(top_n(j.k_ladder, n)), json.dumps(top_n(p_ladder, n))),
                )
            self.episodes.observe((j.pair.id, j.label), ts, res, j.days)

        if newly_finished:
            log.info("%d pairs finished (a market closed); no longer polling them. "
                     "They can be deleted from %s.", newly_finished, self.cfg.pairs_path)
        self._finish_sweep(ts, t0, len(pairs), len(slugs), errors_before, best)

    def _finish_sweep(self, ts: float, t0: float, n_pairs: int, depth: int, errors_before: int,
                      best: tuple[float, str, str] | None, record: bool = True) -> None:
        errors = self.kalshi.api.errors + self.pm.api.errors - errors_before
        dur_ms = int(1000 * (time.monotonic() - t0))
        best_edge, best_pair, best_dir = best if best else (None, None, None)
        if record:  # empty sweeps (no pairs yet) only update the live view
            self.db.execute("INSERT INTO sweeps (ts, n_pairs, dur_ms, depth_fetches, errors, best_edge, best_pair, "
                            "best_dir) VALUES (?,?,?,?,?,?,?,?)",
                            (ts, n_pairs, dur_ms, depth, errors, best_edge, best_pair, best_dir))
        self.db.commit()
        self.sweep_count += 1
        self.last_sweep = {"ts": ts, "dur_ms": dur_ms, "n_pairs": n_pairs, "depth_fetches": depth,
                           "errors": errors, "best_edge": best_edge, "best_pair": best_pair, "best_dir": best_dir}
        for fn in self.listeners:
            try:
                fn()
            except Exception:
                log.exception("sweep listener failed")


def make_scanner(cfg: Config, db: sqlite3.Connection, client) -> Scanner:
    return Scanner(
        cfg, db,
        Kalshi(Api(client, cfg.kalshi_base, cfg.kalshi_rps, "kalshi")),
        PolymarketUS(Api(client, cfg.pmus_base, cfg.pmus_rps, "pmus")),
    )


async def run_loop(scanner: Scanner, stop: asyncio.Event, once: bool = False) -> None:
    cfg = scanner.cfg
    log.info("scanner started; polling every %.1fs", cfg.poll_interval_s)
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                await scanner.sweep()
            except ApiError as e:
                log.error("sweep failed: %s", e)
                scanner.last_error = {"ts": time.time(), "message": str(e)}
            except Exception as e:
                log.exception("sweep failed")
                scanner.last_error = {"ts": time.time(), "message": f"{type(e).__name__}: {e}"}
            if once:
                break
            wait = max(0.0, cfg.poll_interval_s - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
    finally:
        scanner.episodes.close_all()
        scanner.db.commit()
        log.info("scanner stopped")


async def run(cfg: Config, db: sqlite3.Connection, once: bool = False) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with make_client() as client:
        await run_loop(make_scanner(cfg, db, client), stop, once)
