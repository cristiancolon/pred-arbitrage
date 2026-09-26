"""Paper trading: what a bot on this machine would get, without placing any orders.

When the streaming scanner prices a pick (bankroll.PickRules), the paper trader acts
on it like a live bot would:

1. Wait until the window has been open ``pick_min_window_s``, and until both legs'
   best prices have held still for ``paper_quiet_s``. Most windows close within a few
   hundred milliseconds: one venue's price moves and the other venue's stale quote is
   taken or pulled before an order from here could reach it. And a price that has
   just moved, even in a window that has been open a while, tends to move again
   before the second leg arrives.
2. Size the pair from the liquidity that stayed on both books for the last
   ``pick_min_window_s`` (a level that came and went doesn't count) and the cash on
   each venue (``bankroll_usd`` split in two, plus whatever settled trades returned
   there), and send immediate-or-cancel limit orders at the worst price it needs on
   each book: one leg first, and the other for what that filled once its report is
   back. ``paper_lead_venue = "auto"`` leads with the leg whose price moved most
   recently, the likelier to be gone, so a miss there costs nothing ("P" or "K" fix
   the order, "" sends both at once).
3. Each leg fills against that venue's live book as it stood when the order would
   have arrived (latency.LatencyModel: our decision time + half a measured round
   trip + how far the feed runs behind the exchange). Whatever others took or
   pulled in the meantime is gone; a price that moved past the limit doesn't fill.
4. When the fill reports are back, an unequal fill leaves some contracts unhedged.
   The bot first tries to buy the missing leg at up to break-even, then sells any
   remainder back (buys the opposite side on the same venue, which nets out).
5. Positions are held until both markets resolve; each venue then pays $1 per
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
from collections import Counter, deque
from dataclasses import dataclass, field

from . import bankroll
from .arb import Leg, walk
from .fees import order_fee, per_contract
from .latency import LatencyModel

log = logging.getLogger(__name__)

SHADOW_S = 120.0
BOOK_LEVELS = 5  # ask levels saved with each trade, as seen and as met
WATCH_LEVELS = 10  # ask levels remembered per book version while a window is watched
RECHECK_S = 0.25  # an old-enough window that isn't worth trading is looked at again this often
IDLE_WATCH_S = 60.0  # forget a window's books once it hasn't been priced for this long
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
        room = (budget - spend) / unit
        n = min(math.floor(avail + EPS), left, math.floor(room + EPS) if math.isfinite(room) else left)
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


def lasting(ladders) -> list:
    """The liquidity every one of ``ladders`` offered: at each price, the fewest
    contracts any of them had at that price or better. A level that came and went
    doesn't count; one that shrank counts at its smallest."""
    prices = sorted({p for ladder in ladders for p, _ in ladder})
    least = [math.inf] * len(prices)
    for ladder in ladders:
        got, i = 0.0, 0
        for k, p in enumerate(prices):
            while i < len(ladder) and ladder[i][0] <= p + EPS:
                got += ladder[i][1]
                i += 1
            least[k] = min(least[k], got)
    out, prev = [], 0.0
    for p, n in zip(prices, least):
        if n - prev > EPS:
            out.append((p, n - prev))
            prev = n
    return out


@dataclass
class Watch:
    """The books one open window has shown during the last ``keep`` seconds."""
    window: float  # the scanner's id for the window (when it opened)
    since: float  # when we first saw it
    due: float  # when it may next be considered for a trade
    seen: deque = field(default_factory=deque)  # (ts, Kalshi ladder, Polymarket ladder)
    woken: float = 0.0  # when the re-check already scheduled fires

    def add(self, ts: float, kl, pl, keep: float) -> None:
        self.seen.append((ts, kl[:WATCH_LEVELS], pl[:WATCH_LEVELS]))
        # Keep the version that was showing ``keep`` seconds ago and everything since.
        while len(self.seen) > 1 and self.seen[1][0] <= ts - keep:
            self.seen.popleft()

    def lasting(self) -> tuple[list, list]:
        return lasting([s[1] for s in self.seen]), lasting([s[2] for s in self.seen])


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
    def __init__(self, cfg, db, out, latency: LatencyModel, kbooks: dict, pbooks: dict, kmeta: dict, wake=None):
        self.cfg = cfg
        self.out = out  # DbWriter (or a connection in tests)
        self.latency = latency
        self.kbooks, self.pbooks, self.kmeta = kbooks, pbooks, kmeta
        # wake(pair, delay): have the scanner price ``pair`` again after ``delay`` seconds,
        # so a window that's still open once it's old enough is traded even if neither
        # book changes in the meantime.
        self.wake = wake
        self.rules = bankroll.PickRules.from_config(cfg)
        self.cash = {"K": 0.0, "P": 0.0}
        self.tied = {"K": 0.0, "P": 0.0}  # spent on positions not yet settled
        self.reserved = {"K": 0.0, "P": 0.0}
        self.deposits = {"K": 0.0, "P": 0.0}
        self.open: dict[str, dict] = {}  # trade id -> row, positions awaiting settlement
        self.busy: set[str] = set()  # pairs with orders in flight
        self.traded: dict[tuple[str, str], float] = {}  # (pair, direction) -> window already traded
        self.watch: dict[tuple[str, str], Watch] = {}  # (pair, direction) -> the open window's recent books
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
              budget: float, met: dict | None = None) -> Fill:
        ladder = self._ladder(venue, market, side)
        if met is not None:
            met[venue] = [list(lv) for lv in ladder[:BOOK_LEVELS]] if ladder is not None else None
        if ladder is None or qty < 1:
            return Fill()
        levels = take(ladder, qty, limit, coef, budget, self._hidden(venue, market, side))
        self._hide(venue, market, side, levels)
        return Fill(levels, order_fee(venue, coef, levels) if levels else 0.0)

    # --- deciding (called from the scanner's hot path) ------------------------------------

    def consider(self, pair, label: str, k_side: str, p_side: str, kl, pl, k_coef: float, p_coef: float,
                 days: float | None, window: float, seen_ts: float, seen=None) -> None:
        """Called on every re-pricing of an open window (the scanner's ``window`` id).
        ``seen``: the scanner's own walk of these books (ArbResult), to skip non-picks early."""
        key = (pair.id, label)
        now = time.time()
        w = self.watch.get(key)
        if w is None or w.window != window:
            w = self.watch[key] = Watch(window, now, now + self.rules.min_window_s)
        w.add(now, kl, pl, self.rules.min_window_s)
        if pair.id in self.busy or self.traded.get(key) == window:
            return
        if seen is not None and self.rules.reason(seen.top_edge, math.inf, days,
                                                  bankroll.annualized(seen.profit, seen.cost, days)):
            return
        if now < w.due:
            self._wake(pair, w, now)
            return
        # Both legs' prices must have held still a while: a quote that just moved tends
        # to keep moving, and the second leg arrives a few hundred ms after the first.
        steady = {"K": self._steady("K", pair.kalshi, k_side, now), "P": self._steady("P", pair.pm, p_side, now)}
        hold = self.cfg.paper_quiet_s - min(steady.values())
        if hold > 0:
            w.due = now + hold
            self._wake(pair, w, now)
            return
        # Each venue's money is cash plus what's tied up in open positions; one pick gets
        # at most its stake cap of that, less the longer it locks the money up.
        cap = self.rules.stake_fraction(days)
        budget = {v: min(self.available(v), cap * (self.cash[v] + self.tied[v])) for v in VENUES}
        if min(budget.values()) < 1.0:
            self.stats["no cash"] += 1
            self.traded[key] = window
            return
        kl, pl = w.lasting()
        kv = self._visible(kl, self._hidden("K", pair.kalshi, k_side))
        pv = self._visible(pl, self._hidden("P", pair.pm, p_side))
        res = walk(Leg(kv, k_coef), Leg(pv, p_coef), self.cfg.min_edge, budget_a=budget["K"], budget_b=budget["P"])
        rate = bankroll.annualized(res.profit, res.cost, days)
        if (not res.positive or res.profit < self.cfg.paper_min_profit_usd
                or self.rules.reason(res.top_edge, now - w.since, days, rate)):
            # Not worth it on what stayed put; look again shortly, as a level that came and went ages out.
            w.due = now + RECHECK_S
            self._wake(pair, w, now)
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
                 "days": days, "resolve_ts": now + days * 86400 if days is not None else None,
                 "_seen": {"K": [list(lv) for lv in kv[:BOOK_LEVELS]], "P": [list(lv) for lv in pv[:BOOK_LEVELS]]},
                 "_steady": steady}
        task = asyncio.get_running_loop().create_task(
            self._execute(trade, pair.kalshi, pair.pm, k_coef, p_coef, reserve, max(0.0, now - seen_ts)))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _wake(self, pair, w: Watch, now: float) -> None:
        """Have the scanner price the pair again when the window is next due, in case
        neither book changes before then. A re-check already set for no later than
        that will do: it looks again and sets another if it's still early."""
        if self.wake is not None and not (now < w.woken <= w.due + 0.01):
            w.woken = w.due + 0.01
            self.wake(pair, w.woken - now)

    def _steady(self, venue: str, market: str, side: str, now: float) -> float:
        """How long this leg's best ask has been at its current price."""
        book = (self.kbooks if venue == "K" else self.pbooks).get(market)
        return now - book.top_ts[0 if side == "yes" else 1] if book is not None else 0.0

    def forget_idle(self) -> None:
        """Drop the books of windows that haven't been priced lately (they've closed)."""
        cutoff = time.time() - IDLE_WATCH_S
        for key in [k for k, w in self.watch.items() if w.seen[-1][0] < cutoff]:
            del self.watch[key]

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

            met: dict = {}
            limits = {"K": t["k_limit"], "P": t["p_limit"]}
            steady = t.pop("_steady")
            lead = self.cfg.paper_lead_venue
            if lead == "auto":  # the leg that moved most recently goes first
                lead = "K" if steady["K"] < steady["P"] else "P"
            lead = lead if lead in VENUES else None

            async def leg(v: str, qty: float, at: float) -> Fill:
                await self._at(at)
                return self._fill(v, mk[v], side[v], qty, limits[v], coef[v], reserve[v], met)

            if lead is None:  # both legs at once
                fills = dict(zip(VENUES, await asyncio.gather(leg("K", t["planned_size"], t0 + d["K"].look),
                                                              leg("P", t["planned_size"], t0 + d["P"].look))))
                await self._at(t0 + max(d["K"].reply, d["P"].reply))  # both fill reports are back
            else:
                # The lead leg first; the other only for what it filled, once its report is back.
                follow = "P" if lead == "K" else "K"
                fills = {lead: await leg(lead, t["planned_size"], t0 + d[lead].look)}
                await self._at(t0 + d[lead].reply)
                if fills[lead].qty >= 1:
                    t1 = time.monotonic()
                    df = self.latency.delay(follow)
                    fills[follow] = await leg(follow, fills[lead].qty, t1 + df.look)
                    t[f"{follow.lower()}_delay_ms"] = 1000 * (t1 - t0 + df.look)
                    await self._at(t1 + df.reply)
                else:
                    fills[follow] = Fill()
                    t[f"{follow.lower()}_delay_ms"] = None
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
                    # Selling what we hold needs no cash, so no budget limit here.
                    uw = self._fill(long_v, mk[long_v], _opposite(side[long_v]), x, 0.99, coef[long_v], math.inf)
                    self.cash[long_v] += uw.qty - uw.spent
                    out[long_v] += uw.spent - uw.qty
                    fees[long_v] += uw.fees
                    hold[long_v] -= uw.qty
                    t.update(unwind_venue=long_v, unwind_qty=uw.qty,
                             unwind_loss=uw.qty * unit_long + uw.spent - uw.qty)

            t["books"] = json.dumps({"seen": t.pop("_seen"), "met": met, "lead": lead,
                                     "steady": {v: round(x, 3) for v, x in steady.items()}}, separators=(",", ":"))
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
            "days", "resolve_ts", "status", "settled_ts", "payout_k", "payout_p", "pnl", "note", "books")

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

