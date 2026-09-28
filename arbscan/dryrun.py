"""The dry run: what a first, capped live run would do, with its real orders built
and never sent.

A second paper trader (paper.py) acts on the same picks as the paper trader, in its
own account (``dry_trades``; ``live_bankroll_usd``, half on each venue), under the
limits a first live run would have:

- **Series with a clean record.** A pair's Kalshi series must have settled at least
  ``live_series_min_settled`` approved pairs, none conflicting and at most
  ``live_series_max_void`` of them voided. A mismatched pair looks like an arb, so it
  gets picked far more often than it occurs; this keeps to pairings that have
  already settled as one bet many times.
- **Stakes.** At most ``live_max_stake_usd`` a trade, both legs together, and only
  picks expected to make ``live_min_profit_usd``.
- **A daily loss limit.** No new trade once trades settled today have lost
  ``live_daily_loss_usd``.
- **The account.** No market the account already holds a position in (it may be one
  taken by hand). No Kalshi market on an exchange shard without cash: Kalshi only
  fills an order from the cash on its market's shard. Nothing within
  ``ATTEST_MARGIN_S`` of the Kalshi API key's location check lapsing, after which
  Kalshi takes no API orders on sports, elections or entertainment.

Every leg the dry-run trader simulates is also written out as the order a live trader
would send (``live_orders``, mode ``dry``): the request body the order clients in
orders.py would post. Polymarket US checks each buy with its order preview, which
validates price, size, market state and buying power without placing anything (a
sale can't be checked without the position it sells). Kalshi has no such check.
"""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime

from .orders import KalshiTrading, Order, PMTrading, Result, record, status_of
from .paper import PaperTrader
from .store import open_db

log = logging.getLogger(__name__)

ATTEST_MARGIN_S = 24 * 3600.0
ACCOUNT_EVERY_S = 3600.0  # re-read positions, shard cash and the location check this often
RECORD_EVERY_S = 600.0  # re-read the series' settlement records and today's losses
PREVIEW_QUEUE = 50  # previews waiting at most; any more are counted as dropped

SERIES_SQL = """SELECT CASE WHEN instr(kalshi, '-') > 0 THEN substr(kalshi, 1, instr(kalshi, '-') - 1) ELSE kalshi END,
       COUNT(*), SUM(outcome = 'conflict'), SUM(outcome = 'void')
FROM pair_outcomes GROUP BY 1"""


def clean_series(rows, min_settled: int, max_void: float) -> set[str]:
    """Series whose settled pairs were all one bet or, now and then, voided."""
    return {s for s, n, conflicts, voids in rows
            if n >= min_settled and not conflicts and (voids or 0) <= max_void * n}


class Guard:
    """The live limits, called as ``guard(pair, days)`` by the dry-run trader: why a
    first live run wouldn't take a pick, or None. Until the settlement records and the
    account have been read, nothing passes."""

    def __init__(self, cfg, kmeta: dict):
        self.cfg, self.kmeta = cfg, kmeta
        self.series: set[str] | None = None
        self.held: set[str] = set()  # markets the account holds a position in, on either venue
        self.shard_cash: dict[int, float] | None = None
        self.attested_until = 0.0  # 0: not read yet, or never attested
        self.lost_today = 0.0

    def __call__(self, pair, days) -> str | None:
        if self.series is None or pair.kalshi.split("-")[0] not in self.series:
            return "series without a clean record"
        if pair.kalshi in self.held or pair.pm in self.held:
            return "account holds a position"
        if self.lost_today >= self.cfg.live_daily_loss_usd:
            return "daily loss limit"
        if self.attested_until - time.time() < ATTEST_MARGIN_S:
            return "Kalshi location check lapsing"
        shard = getattr(self.kmeta.get(pair.kalshi), "shard", 0)
        if self.shard_cash is None or self.shard_cash.get(shard, 0.0) < 1.0:
            return f"no cash on Kalshi shard {shard}"
        return None

    def read_records(self, db_path: str, table: str) -> None:
        """Settlement records per series, and what today's settled trades lost (runs in a thread)."""
        db = open_db(db_path)
        try:
            self.series = clean_series(db.execute(SERIES_SQL).fetchall(), self.cfg.live_series_min_settled,
                                       self.cfg.live_series_max_void)
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            pnl = db.execute(f"SELECT COALESCE(SUM(pnl), 0) FROM {table} WHERE settled_ts >= ?",
                             (midnight,)).fetchone()[0]
            self.lost_today = max(0.0, -pnl)
        finally:
            db.close()

    async def read_account(self, kalshi: KalshiTrading, pm: PMTrading) -> None:
        held = set(await kalshi.positions()) | set(await pm.positions())
        self.held = held
        self.shard_cash = await kalshi.shard_cash()
        self.attested_until = float(await kalshi.attested_until() or 0.0)


class DryOrders:
    """Writes each leg the dry-run trader simulates as the order a live trader would
    send, and has Polymarket US preview the buys."""

    def __init__(self, out, pm: PMTrading | None):
        self.out, self.pm = out, pm
        self.queue: asyncio.Queue | None = None
        self.previews: Counter[str] = Counter()

    def leg(self, trade_id: str, o: Order, fill, sold: bool = False) -> None:
        avg = None
        if fill.qty >= 1:
            avg = fill.cost / fill.qty
            avg = round(1.0 - avg, 6) if sold else avg  # a sale was simulated as buying the other side
        body = KalshiTrading.body(o) if o.venue == "K" else PMTrading.body(o)
        record(self.out, Result(o, status_of(fill.qty, o.qty), filled=fill.qty, avg_price=avg, fees=fill.fees,
                                sent=time.time(), body=body), "dry", trade_id)
        if o.venue == "P" and o.action == "buy" and self.pm is not None and self.queue is not None:
            try:
                self.queue.put_nowait(o)
            except asyncio.QueueFull:
                self.previews["dropped"] += 1

    async def run(self, stop: asyncio.Event) -> None:
        self.queue = asyncio.Queue(PREVIEW_QUEUE)
        while not stop.is_set():
            try:
                o = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            res = await self.pm.preview(o)
            verdict = "ok" if res.status == "none" else (res.error or res.status)
            self.previews["ok" if verdict == "ok" else "refused"] += 1
            self.out.execute("UPDATE live_orders SET preview = ? WHERE id = ?", (verdict[:300], o.client_id))


class DryRun:
    """The dry-run trader, its limits and its order log, for the streaming scanner."""

    def __init__(self, cfg, db, out, latency, kbooks, pbooks, kmeta, kalshi: KalshiTrading, pm: PMTrading, wake=None):
        self.cfg, self.kalshi, self.pm = cfg, kalshi, pm
        self.guard = Guard(cfg, kmeta)
        self.orders = DryOrders(out, pm)
        dry_cfg = replace(cfg, bankroll_usd=cfg.live_bankroll_usd, paper_min_profit_usd=cfg.live_min_profit_usd)
        self.trader = PaperTrader(dry_cfg, db, out, latency, kbooks, pbooks, kmeta, wake=wake, name="dry",
                                  guard=self.guard, max_stake=cfg.live_max_stake_usd, orders=self.orders)

    async def run(self, stop: asyncio.Event) -> None:
        """Keep the limits' inputs current and the previews flowing."""
        previews = asyncio.create_task(self.orders.run(stop))
        next_records = next_account = 0.0
        try:
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_records:
                    next_records = now + RECORD_EVERY_S
                    try:
                        await asyncio.to_thread(self.guard.read_records, self.cfg.db_path, self.trader.table)
                    except Exception as e:
                        log.warning("dry run: reading settlement records failed: %s", e)
                if now >= next_account:
                    next_account = now + ACCOUNT_EVERY_S
                    try:
                        await self.guard.read_account(self.kalshi, self.pm)
                    except Exception as e:
                        log.warning("dry run: reading the accounts failed: %s", e)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            previews.cancel()

    def snapshot(self) -> dict:
        g = self.guard
        return {**self.trader.snapshot(), "previews": dict(self.orders.previews),
                "limits": {"series": None if g.series is None else len(g.series), "held": len(g.held),
                           "shard_cash": g.shard_cash, "attested_until": g.attested_until or None,
                           "lost_today": g.lost_today, "max_stake": self.cfg.live_max_stake_usd,
                           "min_profit": self.cfg.live_min_profit_usd, "daily_loss": self.cfg.live_daily_loss_usd}}
