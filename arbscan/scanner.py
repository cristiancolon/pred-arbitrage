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
    mode = "poll"

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
        self._depth_budget = MAX_DEPTH_FETCHES_PER_SWEEP
        self._meta_task: asyncio.Task | None = None

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)

    async def refresh_kalshi_meta(self, tickers: list[str]) -> None:
        if time.monotonic() - self.series_fee_ts > SERIES_FEE_TTL_S:
            self.series_fee.clear()
            self.series_fee_ts = time.monotonic()
        markets = await self.kalshi.markets(tickers)
        # Fee rates: the hourly catalog already has them per market. For markets it
        # doesn't know, find the series (fee multiplier) per event: the catalog knows
        # most events too; ask the API for the rest, concurrently.
        known_fee: dict[str, float] = {}
        for i in range(0, len(tickers), 500):
            chunk = tickers[i : i + 500]
            known_fee.update(self.db.execute(
                f"SELECT id, fee_coef FROM markets WHERE venue = 'K' AND fee_coef IS NOT NULL "
                f"AND id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        unknown = {t: m for t, m in markets.items() if t not in known_fee}
        events = sorted({m["event_ticker"] for m in unknown.values()} - set(self.event_series))
        for i in range(0, len(events), 500):
            chunk = events[i : i + 500]
            for ev, series in self.db.execute(
                    f"SELECT event_id, series FROM markets WHERE venue = 'K' AND series IS NOT NULL "
                    f"AND event_id IN ({','.join('?' * len(chunk))})", chunk):
                self.event_series[ev] = series
        missing = [ev for ev in events if ev not in self.event_series]
        for ev, e in zip(missing, await asyncio.gather(*(self.kalshi.event(ev) for ev in missing))):
            self.event_series[ev] = e["series_ticker"]
        need = sorted({self.event_series[m["event_ticker"]] for m in unknown.values()} - set(self.series_fee))
        for series, sd in zip(need, await asyncio.gather(*(self.kalshi.series(x) for x in need))):
            self.series_fee[series] = kalshi_taker_coef(sd.get("fee_type"), sd.get("fee_multiplier"))
        for t in tickers:
            m = markets.get(t)
            if m is None:
                self._warn_once(f"k-missing:{t}", "Kalshi market %s not found; check pairs.csv", t)
                self.kmeta[t] = KMeta("missing", None, 0.0)
                continue
            fee = known_fee[t] if t in known_fee else self.series_fee[self.event_series[m["event_ticker"]]]
            resolve = parse_ts(m.get("expected_expiration_time")) or parse_ts(m.get("close_time"))
            self.kmeta[t] = KMeta(m.get("status") or "", resolve, fee)
        self.meta_ts = time.monotonic()

    async def _refresh_meta_bg(self, tickers: list[str]) -> None:
        try:
            await self.refresh_kalshi_meta(tickers)
        except Exception as e:
            log.warning("Kalshi metadata refresh failed: %s", e)
            self.meta_ts = time.monotonic()  # try again after the usual interval
        finally:
            self._meta_task = None

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

    def _chunks(self, pairs: list[Pair]) -> list[list[Pair]]:
        """Groups of pairs needing at most one request per venue each. Pairs that share
        a Polymarket market (a game's "same" and "inverse" pairs) stay together."""
        out: list[list[Pair]] = []
        cur: list[Pair] = []
        tickers: set[str] = set()
        slugs: set[str] = set()
        for p in sorted(pairs, key=lambda p: (p.pm, p.kalshi)):
            if cur and ((p.kalshi not in tickers and len(tickers) >= Kalshi.ORDERBOOK_BATCH)
                        or (p.pm not in slugs and len(slugs) >= PolymarketUS.SLUG_BATCH)):
                out.append(cur)
                cur, tickers, slugs = [], set(), set()
            cur.append(p)
            tickers.add(p.kalshi)
            slugs.add(p.pm)
        if cur:
            out.append(cur)
        return out

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
        new = [t for t in tickers if t not in self.kmeta]
        if new:  # can't price these without their fee and status
            await self.refresh_kalshi_meta(new)
        elif time.monotonic() - self.meta_ts > self.cfg.meta_refresh_s and self._meta_task is None:
            # Routine refresh in the background, so pricing never waits on it.
            self._meta_task = asyncio.create_task(self._refresh_meta_bg(tickers))

        # Each chunk fetches both venues at once and is priced as soon as both answer,
        # so the two sides of a pair are never more than a request apart in time.
        self._depth_budget = MAX_DEPTH_FETCHES_PER_SWEEP
        outs = await asyncio.gather(*(self._sweep_chunk(c) for c in self._chunks(pairs)))
        best = max((o[0] for o in outs if o[0] is not None), key=lambda b: b[0], default=None)
        newly_finished = sum(o[1] for o in outs)
        if newly_finished:
            log.info("%d pairs finished (a market closed); no longer polling them. "
                     "They can be deleted from %s.", newly_finished, self.cfg.pairs_path)
        self._finish_sweep(time.time(), t0, len(pairs), sum(o[2] for o in outs), errors_before, best)

    async def _sweep_chunk(self, pairs: list[Pair]) -> tuple[tuple[float, str, str] | None, int, int]:
        live = sorted({p.kalshi for p in pairs if self.kmeta[p.kalshi].status == "active"})
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
        slugs = slugs[: max(0, self._depth_budget)]
        self._depth_budget -= len(slugs)
        results = await asyncio.gather(*(self.pm.book(s) for s in slugs), return_exceptions=True)
        books = {}
        for s, r in zip(slugs, results):
            if isinstance(r, BaseException):
                log.warning("Polymarket US book %s: %s", s, r)
                continue
            books[s] = pmus_ladders(r)
            self.last_depth[s] = time.monotonic()

        ts = time.time()
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
        return best, newly_finished, len(slugs)

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


def make_scanner(cfg: Config, db: sqlite3.Connection, client, stream: bool | None = None) -> Scanner:
    """The streaming scanner when both venues' API keys are configured, else polling."""
    kalshi = Kalshi(Api(client, cfg.kalshi_base, cfg.kalshi_rps, "kalshi"))
    pm = PolymarketUS(Api(client, cfg.pmus_base, cfg.pmus_rps, "pmus"))
    if cfg.can_stream if stream is None else stream:
        from .auth import KalshiSigner, PMSigner
        from .feeds import KalshiFeed, PMFeed
        from .live import LiveScanner

        noop = lambda *_: None  # noqa: E731 (LiveScanner installs the real callbacks)
        kfeed = KalshiFeed(cfg.kalshi_ws_url, KalshiSigner.from_file(cfg.kalshi_key_id, cfg.kalshi_private_key_path),
                           noop, noop)
        pfeed = PMFeed(cfg.pmus_ws_url, PMSigner(cfg.pmus_key_id, cfg.pmus_secret_key), noop)
        return LiveScanner(cfg, db, kalshi, pm, kfeed, pfeed)
    return Scanner(cfg, db, kalshi, pm)


async def run_loop(scanner: Scanner, stop: asyncio.Event, once: bool = False) -> None:
    if hasattr(scanner, "run_forever") and not once:
        await scanner.run_forever(stop)
        return
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
        await run_loop(make_scanner(cfg, db, client, stream=False if once else None), stop, once)
