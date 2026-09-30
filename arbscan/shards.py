"""Kalshi cash across exchange shards, kept where the trades are.

Kalshi fills an order only from the cash on its market's exchange shard (tennis,
baseball and basketball trade on one, most other sports on another), so money sitting
on the wrong shard can't be traded. The live trader moves it between shards with
Kalshi's intra-exchange transfers, the same way someone would by hand:

- A shard **in use** has an active paired market in a series cleared to trade. Each
  gets at least one full trade's worth (the per-trade cap), or an even share of the
  cash when there isn't that much, so no pick there is cut short for lack of cash.
- The rest goes by **demand**: what the live trader spent on each shard's Kalshi legs
  over the last ``DEMAND_DAYS`` days, plus what picks there were denied or cut short
  for lack of cash on their shard.
- A shard **not in use** is emptied into the others.

Money moves only between trades, and only once a shard has drifted well below its
target (``TOLERANCE``), so settlements landing on one shard don't set off a transfer
each; a shard that has just denied or cut short a pick is topped up at the next look.
"""

import logging
import time
from collections import defaultdict

import httpx

log = logging.getLogger(__name__)

DEMAND_DAYS = 3.0
TOLERANCE = (3.0, 0.15)  # act once a shard is this far below target: dollars, or a share of the target
MIN_MOVE = 1.0  # transfers smaller than this aren't worth making
EVERY_S = 300.0  # at most one round of transfers this often, unless a shard ran short
PRIOR = 0.1  # every shard in use gets this share of the demand on top, so a quiet one isn't starved


def series(ticker: str) -> str:
    return ticker.split("-")[0]


def targets(total: float, used: set[int], demand: dict[int, float], floor: float) -> dict[int, float]:
    """Where ``total`` dollars of Kalshi cash should sit: ``floor`` on each shard in use
    (or an even share, if there isn't that much), the rest by demand."""
    if not used:
        return {}
    floor = min(floor, total / len(used))
    spare = total - floor * len(used)
    d = {s: max(0.0, demand.get(s, 0.0)) for s in used}
    base = sum(d.values())
    w = {s: (d[s] + PRIOR * base / len(used)) / (base * (1 + PRIOR)) for s in used} if base > 0 else \
        {s: 1 / len(used) for s in used}
    return {s: floor + spare * w[s] for s in used}


def plan(cash: dict[int, float], want: dict[int, float], short: set[int] = frozenset(),
         tolerance: tuple[float, float] = TOLERANCE) -> list[tuple[int, int, float]]:
    """The transfers (from, to, dollars) that bring each shard's cash to what it
    should have, or none while every shard is close enough. Shards missing from
    ``want`` should hold nothing; those in ``short`` just ran out, so any gap counts."""
    want = {s: want.get(s, 0.0) for s in set(cash) | set(want)}
    gap = {s: want[s] - cash.get(s, 0.0) for s in want}
    behind = any(gap[s] >= MIN_MOVE and (s in short or gap[s] > max(tolerance[0], tolerance[1] * want[s]))
                 for s in want)
    idle = any(want[s] == 0 and cash.get(s, 0.0) >= MIN_MOVE for s in want)
    if not behind and not idle:
        return []
    need = sorted(((gap[s], s) for s in want if gap[s] >= MIN_MOVE), reverse=True)
    spare = sorted(([-gap[s], s] for s in want if -gap[s] >= MIN_MOVE), reverse=True)
    moves = []
    for n, dst in need:
        while spare and n >= MIN_MOVE:
            src = spare[0][1]
            amt = int(min(n, spare[0][0]) * 100) / 100  # whole cents, never more than is there
            if amt < MIN_MOVE:
                break
            moves.append((src, dst, amt))
            n -= amt
            spare[0][0] -= amt
            if spare[0][0] < MIN_MOVE:
                spare.pop(0)
    return moves


class Rebalancer:
    """Keeps the live account's Kalshi cash on the shards its trades need; see the module docstring."""

    def __init__(self, kalshi, guard, trader, kmeta: dict):
        self.kalshi, self.guard, self.trader, self.kmeta = kalshi, guard, trader, kmeta
        self.short: dict[int, list[tuple[float, float]]] = defaultdict(list)  # shard -> (when, dollars missing)
        self.urgent: set[int] = set()
        self.last = 0.0
        self.want: dict[int, float] = {}
        self.moves: list[dict] = []  # the latest transfers, newest first
        self.failed_until = 0.0
        self.allocation: dict[int, int] | None = None

    def ran_short(self, shard: int, dollars: float) -> None:
        """A pick on this shard was denied or cut short for lack of cash there."""
        now = time.time()
        marks = self.short[shard]
        if marks and now - marks[-1][0] < 60:  # the same window, priced again
            marks[-1] = (now, max(marks[-1][1], dollars))
        else:
            marks.append((now, dollars))
        if self.guard.shard_cash is not None:
            self.urgent.add(shard)

    def used(self) -> set[int]:
        cleared = self.guard.series
        return {m.shard for t, m in self.kmeta.items()
                if m.status == "active" and (cleared is None or series(t) in cleared)}

    def demand(self, db) -> dict[int, float]:
        """Dollars each shard's Kalshi legs needed lately: what trades spent, and what
        picks went without."""
        since = time.time() - DEMAND_DAYS * 86400
        where = defaultdict(set)  # a series' markets are on one shard; finished ones have left kmeta
        for t, m in self.kmeta.items():
            where[series(t)].add(m.shard)
        out = defaultdict(float)
        for pair, spent in db.execute(f"SELECT pair, k_out FROM {self.trader.table} WHERE ts >= ? AND k_out > 0",
                                      (since,)):
            ticker = pair.split("|")[0]
            m = self.kmeta.get(ticker)
            shards = {m.shard} if m is not None else where.get(series(ticker), set())
            if len(shards) == 1:
                out[next(iter(shards))] += spent
        for s, marks in self.short.items():
            marks[:] = [(ts, d) for ts, d in marks if ts >= since]
            out[s] += sum(d for _, d in marks)
        return dict(out)

    def due(self) -> bool:
        now = time.time()
        return now >= self.failed_until and (bool(self.urgent) or now - self.last >= EVERY_S)

    async def step(self, db) -> list[tuple[int, int, float]]:
        """Look once, and make whatever transfers are due. Call between trades only."""
        g = self.guard
        if g.shard_cash is None or self.trader.busy or not self.due():
            return []
        cap = self.trader.stake_cap() or 0.0
        self.want = targets(sum(g.shard_cash.values()), self.used(), self.demand(db), cap)
        moves = plan(g.shard_cash, self.want, self.urgent)
        self.urgent.clear()
        self.last = time.time()
        done = []
        for src, dst, amt in moves:
            if self.trader.busy:
                break  # a trade started meanwhile; look again after it
            g.epoch += 1  # a balance read taken during the transfer isn't kept
            g.shard_cash[src] = g.shard_cash.get(src, 0.0) - amt  # never spend what's on its way out
            try:
                reply = await self.kalshi.transfer(src, dst, amt)
            except Exception as e:
                if isinstance(e, (RuntimeError, httpx.ConnectError, httpx.ConnectTimeout)):
                    g.shard_cash[src] += amt  # refused, or never sent
                # Otherwise it may have gone through; either way the balances are read back.
                self.trader.account_stale = True
                self.failed_until = time.time() + 1800
                log.warning("live: moving $%.2f from Kalshi shard %d to %d failed: %s", amt, src, dst, e)
                break
            if (reply.get("status") or "complete") == "complete":
                g.shard_cash[dst] = g.shard_cash.get(dst, 0.0) + amt
            log.info("live: moved $%.2f from Kalshi shard %d to %d", amt, src, dst)
            self.moves.insert(0, {"ts": time.time(), "from": src, "to": dst, "amount": amt})
            done.append((src, dst, amt))
        del self.moves[10:]
        if done:
            self.trader.account_stale = True  # read the real balances back
            await self._allocate()
        return done

    async def _allocate(self) -> None:
        """Point Kalshi's own target allocation at the same split, so it never pulls the
        money back."""
        total = sum(self.want.values())
        if not total:
            return
        pct = {s: round(100 * w / total) for s, w in self.want.items()}
        top = max(pct, key=pct.get)
        pct[top] += 100 - sum(pct.values())
        if self.allocation and all(abs(pct.get(s, 0) - self.allocation.get(s, 0)) < 5 for s in pct | self.allocation):
            return
        try:
            await self.kalshi.allocate(pct)
            self.allocation = pct
        except Exception as e:
            log.warning("live: setting Kalshi's target allocation failed: %s", e)

    def snapshot(self) -> dict:
        return {"targets": self.want, "moves": self.moves}
