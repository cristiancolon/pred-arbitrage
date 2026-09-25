"""Paper trading: what a bot on this machine would get, without placing any orders.

When the streaming scanner prices a pick (bankroll.PickRules; the time-open rule is
replaced by real latency here), the paper trader acts on it like a live bot would:

1. Size the pair from the cash on each venue (``bankroll_usd`` split in two, plus
   whatever settled trades returned there) and send both legs at once as
   immediate-or-cancel limit orders at the worst price it needs on each book.
2. Each leg fills against that venue's live book as it stood when the order would
   have arrived (latency.LatencyModel: our decision time + half a measured round
   trip + how far the feed runs behind the exchange). Whatever others took or
   pulled in the meantime is gone; a price that moved past the limit doesn't fill.
3. When the fill reports are back, an unequal fill leaves some contracts unhedged.
   The bot first tries to buy the missing leg at up to break-even, then sells any
   remainder back (buys the opposite side on the same venue, which nets out).
4. Positions are held until both markets resolve; each venue then pays $1 per
   winning contract into its own cash, using the venues' published results
   (results.py), so a pair that wasn't really the same bet shows up as a loss.

Costs: taker fees per order, rounded up to each venue's balance precision
(fees.order_fee), slippage from walking the book, anything lost unwinding a leg,
and cash tied up until resolution. Our own simulated fills hide the liquidity they
took for ``SHADOW_S`` so later trades can't take it again.
"""

import asyncio
import json
import logging
import math
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

from . import bankroll
from .arb import Leg, walk
from .fees import order_fee, per_contract
from .latency import LatencyModel

log = logging.getLogger(__name__)

SHADOW_S = 120.0
SETTLE_GRACE_S = 3600.0  # look for results this long after the expected resolution
EPS = 1e-9
VENUES = ("K", "P")


@dataclass
class Fill:
    levels: list[tuple[float, float]] = field(default_factory=list)  # (price, contracts)
    fees: float = 0.0

    @property
    def qty(self) -> float:
        return sum(q for _, q in self.levels)

    @property
    def cost(self) -> float:
        return sum(p * q for p, q in self.levels)

    @property
    def spent(self) -> float:
        return self.cost + self.fees


def take(ladder, qty: float, limit: float, coef: float, budget: float, hidden: dict | None = None) -> list:
    """Levels an immediate-or-cancel buy of ``qty`` whole contracts at up to ``limit``
    would fill, spending at most ``budget`` (fees included)."""
    out, left, spend = [], math.floor(qty + EPS), 0.0
    for p, q in ladder:
        if left <= 0 or p > limit + EPS:
            break
        avail = q - (hidden or {}).get(p, 0.0)
        unit = p + per_contract(coef, p)
        n = min(math.floor(avail + EPS), left, math.floor((budget - spend) / unit + EPS))
        if n <= 0:
            if avail >= 1 and left > 0:
                break  # out of cash
            continue
        out.append((p, float(n)))
        left -= n
        spend += n * unit
    return out


def limit_for(ladder, qty: float) -> float | None:
    """The worst price needed to buy ``qty`` contracts from ``ladder``."""
    got = 0.0
    for p, q in ladder:
        got += q
        if got >= qty - EPS:
            return p
    return None


def breakeven_price(coef: float, room: float) -> float:
    """Highest price p with p + fee(p) <= room: the most the missing leg can cost
    before the pair loses money."""
    if room <= 0:
        return 0.0
    if coef <= 0:
        return min(room, 0.99)
    disc = (1 + coef) ** 2 - 4 * coef * room
    p = ((1 + coef) - math.sqrt(max(disc, 0.0))) / (2 * coef)
    return max(0.0, min(p, 0.99))


def _opposite(side: str) -> str:
    return "no" if side == "yes" else "yes"


class PaperTrader:
    def __init__(self, cfg, db, out, latency: LatencyModel, kbooks: dict, pbooks: dict, kmeta: dict):
        self.cfg = cfg
        self.out = out  # DbWriter (or a connection in tests)
        self.latency = latency
        self.kbooks, self.pbooks, self.kmeta = kbooks, pbooks, kmeta
        self.rules = bankroll.PickRules.from_config(cfg)
        self.cash = {"K": 0.0, "P": 0.0}
        self.tied = {"K": 0.0, "P": 0.0}  # spent on positions not yet settled
        self.reserved = {"K": 0.0, "P": 0.0}
        self.deposits = {"K": 0.0, "P": 0.0}
        self.open: dict[str, dict] = {}  # trade id -> row, positions awaiting settlement
        self.busy: set[str] = set()  # pairs with orders in flight
        self.traded: dict[tuple[str, str], float] = {}  # (pair, direction) -> window already traded
        self.hidden: dict[tuple[str, str, str], dict[float, tuple[float, float]]] = {}
        self.stats: Counter[str] = Counter()
        self.realized = 0.0  # P&L of settled trades
        self.tasks: set[asyncio.Task] = set()
        self._load(db)

    # --- account ------------------------------------------------------------------

    def _load(self, db) -> None:
        want = {v: self.cfg.bankroll_usd / 2 for v in VENUES}
        row = db.execute("SELECT value FROM settings WHERE key = 'paper_deposits'").fetchone()
        self.deposits = json.loads(row[0]) if row else dict(want)
        for v in VENUES:
            self.cash[v] = self.deposits[v]
        for r in db.execute("SELECT * FROM paper_trades"):
            r = dict(r)
            for v in VENUES:
                self.cash[v] -= r[f"{v.lower()}_out"] or 0.0
                self.cash[v] += r[f"payout_{v.lower()}"] or 0.0
            if r["status"] == "open":
                self.open[r["id"]] = r
                for v in VENUES:
                    self.tied[v] += r[f"{v.lower()}_out"] or 0.0
            elif r["status"] == "settled":
                self.realized += r["pnl"] or 0.0
        if row is None or self.deposits != want:
            # A changed bankroll works like a deposit or withdrawal on each venue.
            if row is not None:
                log.info("paper account: bankroll changed; moving cash to $%g per venue deposited", want["K"])
            for v in VENUES:
                self.cash[v] += want[v] - self.deposits[v]
            self.deposits = want
            self.out.execute("INSERT OR REPLACE INTO settings VALUES ('paper_deposits', ?)", (json.dumps(want),))
            self.out.commit()

    def available(self, venue: str) -> float:
        return self.cash[venue] - self.reserved[venue]

    # --- liquidity our own fills took -------------------------------------------------

    def _hidden(self, venue: str, market: str, side: str) -> dict[float, float]:
        h = self.hidden.get((venue, market, side))
        if not h:
            return {}
        now = time.monotonic()
        for p in [p for p, (_, exp) in h.items() if exp <= now]:
            del h[p]
        return {p: q for p, (q, _) in h.items()}

    def _hide(self, venue: str, market: str, side: str, levels) -> None:
        h = self.hidden.setdefault((venue, market, side), {})
        exp = time.monotonic() + SHADOW_S
        for p, q in levels:
            h[p] = (h.get(p, (0.0, 0.0))[0] + q, exp)

    def _visible(self, ladder, hidden: dict[float, float]):
        if not hidden:
            return ladder
        return [(p, q - hidden.get(p, 0.0)) for p, q in ladder if q - hidden.get(p, 0.0) > EPS]

    def _ladder(self, venue: str, market: str, side: str):
        """Current ask ladder for buying ``side``, or None if we can't trade there now."""
        if venue == "K":
            km, book = self.kmeta.get(market), self.kbooks.get(market)
            if km is None or km.status != "active" or book is None or not book.ready:
                return None
            yes, no = book.ladders()
            return yes if side == "yes" else no
        book = self.pbooks.get(market)
        if book is None or not book.ready or not book.open:
            return None
        return book.yes_asks if side == "yes" else book.no_asks

    def _fill(self, venue: str, market: str, side: str, qty: float, limit: float, coef: float,
              budget: float) -> Fill:
        ladder = self._ladder(venue, market, side)
        if ladder is None or qty < 1:
            return Fill()
        levels = take(ladder, qty, limit, coef, budget, self._hidden(venue, market, side))
        self._hide(venue, market, side, levels)
        return Fill(levels, order_fee(venue, coef, levels) if levels else 0.0)

    # --- deciding (called from the scanner's hot path) ------------------------------------

    def consider(self, pair, label: str, k_side: str, p_side: str, kl, pl, k_coef: float, p_coef: float,
                 days: float | None, window: float, seen_ts: float) -> None:
        key = (pair.id, label)
        if pair.id in self.busy or self.traded.get(key) == window:
            return
        # Each venue's money is cash plus what's tied up in open positions; one pick gets
        # at most its stake cap of that, less the longer it locks the money up.
        cap = self.rules.stake_fraction(days)
        budget = {v: min(self.available(v), cap * (self.cash[v] + self.tied[v])) for v in VENUES}
        if min(budget.values()) < 1.0:
            self.stats["no cash"] += 1
            self.traded[key] = window
            return
        kv = self._visible(kl, self._hidden("K", pair.kalshi, k_side))
        pv = self._visible(pl, self._hidden("P", pair.pm, p_side))
        res = walk(Leg(kv, k_coef), Leg(pv, p_coef), self.cfg.min_edge, budget_a=budget["K"], budget_b=budget["P"])
        if not res.positive or res.profit < self.cfg.paper_min_profit_usd:
            return
        rate = bankroll.annualized(res.profit, res.cost, days)
        if self.rules.reason(res.top_edge, math.inf, days, rate):  # latency stands in for the time-open rule
            return
        n = res.size
        k_limit, p_limit = limit_for(kv, n), limit_for(pv, n)
        reserve = {"K": min(budget["K"], n * (k_limit + 0.25 * k_coef) + 0.01),
                   "P": min(budget["P"], n * (p_limit + 0.25 * p_coef) + 0.01)}
        for v in VENUES:
            self.reserved[v] += reserve[v]
        self.busy.add(pair.id)
        self.traded[key] = window
        self.stats["sent"] += 1
        now = time.time()
        trade = {"id": uuid.uuid4().hex[:16], "ts": now, "pair": pair.id, "direction": label,
                 "k_side": k_side, "p_side": p_side, "planned_size": n, "planned_edge": res.top_edge,
                 "planned_profit": res.profit, "planned_cost": res.cost, "k_limit": k_limit, "p_limit": p_limit,
                 "days": days, "resolve_ts": now + days * 86400 if days is not None else None}
        task = asyncio.get_running_loop().create_task(
            self._execute(trade, pair.kalshi, pair.pm, k_coef, p_coef, reserve, max(0.0, now - seen_ts)))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # --- executing ----------------------------------------------------------------------

    @staticmethod
    async def _at(t: float) -> None:
        dt = t - time.monotonic()
        if dt > 0:
            await asyncio.sleep(dt)

    async def _execute(self, t: dict, ticker: str, slug: str, k_coef: float, p_coef: float, reserve: dict,
                       decide_s: float) -> None:
        mk = {"K": ticker, "P": slug}
        side = {"K": t["k_side"], "P": t["p_side"]}
        coef = {"K": k_coef, "P": p_coef}
        try:
            t0 = time.monotonic()
            d = {v: self.latency.delay(v, decide_s) for v in VENUES}
            t["k_delay_ms"], t["p_delay_ms"] = 1000 * d["K"].look, 1000 * d["P"].look

            async def leg(v: str, limit: float) -> Fill:
                await self._at(t0 + d[v].look)
                return self._fill(v, mk[v], side[v], t["planned_size"], limit, coef[v], reserve[v])

            fills = dict(zip(VENUES, await asyncio.gather(leg("K", t["k_limit"]), leg("P", t["p_limit"]))))
            await self._at(t0 + max(d["K"].reply, d["P"].reply))  # both fill reports are back
            for v in VENUES:
                self.reserved[v] -= reserve[v]
            reserve = {"K": 0.0, "P": 0.0}
            qty = {v: fills[v].qty for v in VENUES}
            out = {v: fills[v].spent for v in VENUES}
            fees = {v: fills[v].fees for v in VENUES}
            for v in VENUES:
                self.cash[v] -= out[v]
            hold = dict(qty)
            t.update(unwind_venue=None, unwind_qty=0.0, unwind_loss=0.0)

            gap = qty["K"] - qty["P"]
            if abs(gap) >= 1:
                long_v, short_v = ("K", "P") if gap > 0 else ("P", "K")
                x = abs(gap)
                # Buy the missing leg, up to break-even against what the filled leg cost.
                unit_long = out[long_v] / qty[long_v]
                limit = breakeven_price(coef[short_v], 1.0 - unit_long)
                ds = self.latency.delay(short_v)
                t1 = time.monotonic()
                await self._at(t1 + ds.look)
                chase = self._fill(short_v, mk[short_v], side[short_v], x, limit, coef[short_v],
                                   self.available(short_v))
                await self._at(t1 + ds.reply)
                self.cash[short_v] -= chase.spent
                out[short_v] += chase.spent
                fees[short_v] += chase.fees
                qty[short_v] += chase.qty
                hold[short_v] += chase.qty
                x -= chase.qty
                if x >= 1:
                    # Sell the rest back: buying the other side on the same venue nets it
                    # out, returning $1 per contract pair.
                    dl = self.latency.delay(long_v)
                    await self._at(time.monotonic() + dl.look)
                    uw = self._fill(long_v, mk[long_v], _opposite(side[long_v]), x, 0.99, coef[long_v],
                                    self.available(long_v))
                    self.cash[long_v] += uw.qty - uw.spent
                    out[long_v] += uw.spent - uw.qty
                    fees[long_v] += uw.fees
                    hold[long_v] -= uw.qty
                    t.update(unwind_venue=long_v, unwind_qty=uw.qty,
                             unwind_loss=uw.qty * unit_long + uw.spent - uw.qty)

            t.update(k_qty=qty["K"], p_qty=qty["P"], k_fees=fees["K"], p_fees=fees["P"], k_hold=hold["K"],
                     p_hold=hold["P"], k_out=out["K"], p_out=out["P"],
                     locked_profit=min(hold["K"], hold["P"]) - out["K"] - out["P"])
            if hold["K"] < 1 and hold["P"] < 1:
                moved = abs(out["K"]) + abs(out["P"]) > EPS
                t.update(status="settled" if moved else "missed", settled_ts=time.time(), payout_k=0.0,
                         payout_p=0.0, pnl=-out["K"] - out["P"], note="unwound" if moved else "no fill")
                self.realized += t["pnl"]
            else:
                t["status"] = "open"
                if abs(hold["K"] - hold["P"]) >= 1:
                    t["note"] = f"{abs(hold['K'] - hold['P']):g} contracts unhedged"
                self.open[t["id"]] = t
                for v in VENUES:
                    self.tied[v] += out[v]
            self.stats[t["status"] if t["status"] != "settled" else "unwound"] += 1
            self._write(t)
            log.info("paper %s %s %s: planned %d for $%.2f, got K %g / P %g, locked $%.2f (K %.0f ms, P %.0f ms)",
                     t["status"], t["pair"], t["direction"], t["planned_size"], t["planned_profit"], qty["K"],
                     qty["P"], t["locked_profit"], t["k_delay_ms"], t["p_delay_ms"])
        except Exception:
            log.exception("paper trade failed")
            for v in VENUES:
                self.reserved[v] -= reserve[v]
        finally:
            self.busy.discard(t["pair"])

    COLS = ("id", "ts", "pair", "direction", "k_side", "p_side", "planned_size", "planned_edge", "planned_profit",
            "planned_cost", "k_limit", "p_limit", "k_delay_ms", "p_delay_ms", "k_qty", "p_qty", "k_fees", "p_fees",
            "unwind_venue", "unwind_qty", "unwind_loss", "k_hold", "p_hold", "k_out", "p_out", "locked_profit",
            "days", "resolve_ts", "status", "settled_ts", "payout_k", "payout_p", "pnl", "note")

    def _write(self, t: dict) -> None:
        self.out.execute(f"INSERT OR REPLACE INTO paper_trades ({', '.join(self.COLS)}) "
                         f"VALUES ({', '.join('?' * len(self.COLS))})", tuple(t.get(c) for c in self.COLS))

    # --- settling -----------------------------------------------------------------------

    def settle(self, lookup, finished: set[str]) -> int:
        """Pay out positions whose markets both have results (results.py records them).
        ``lookup(keys)`` gives what one YES contract paid per (venue, id). Returns how many."""
        now = time.time()
        due = [t for t in self.open.values()
               if t["pair"] in finished or (t["resolve_ts"] or now) + SETTLE_GRACE_S <= now]
        if not due:
            return 0
        values = lookup({(v, m) for t in due for v, m in zip(VENUES, t["pair"].split("|", 1))})
        n = 0
        for t in due:
            k, p = t["pair"].split("|", 1)
            yk, yp = values.get(("K", k)), values.get(("P", p))
            if yk is None or yp is None:
                continue
            pay_k = t["k_hold"] * (yk if t["k_side"] == "yes" else 1 - yk)
            pay_p = t["p_hold"] * (yp if t["p_side"] == "yes" else 1 - yp)
            self.cash["K"] += pay_k
            self.cash["P"] += pay_p
            self.tied["K"] -= t["k_out"]
            self.tied["P"] -= t["p_out"]
            t.update(status="settled", settled_ts=now, payout_k=pay_k, payout_p=pay_p,
                     pnl=pay_k + pay_p - t["k_out"] - t["p_out"])
            self.realized += t["pnl"]
            del self.open[t["id"]]
            self._write(t)
            n += 1
            log.info("paper settled %s: paid K $%.2f + P $%.2f, P&L $%.2f", t["pair"], pay_k, pay_p, t["pnl"])
        return n

    def snapshot(self) -> dict:
        tied = dict(self.tied)
        locked = sum(t["locked_profit"] for t in self.open.values())
        return {"deposits": self.deposits, "cash": dict(self.cash), "reserved": dict(self.reserved), "tied": tied,
                "open": len(self.open), "locked": locked, "realized": self.realized, "in_flight": len(self.busy),
                "stats": dict(self.stats), "latency": self.latency.snapshot()}

