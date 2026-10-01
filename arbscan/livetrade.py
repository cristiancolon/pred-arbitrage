"""Live trading: the dry run's picks, traded with real orders.

Off unless ``live_trading = true`` (it also needs both venues' API keys; paper trading
and the dry run may be off). The live trader is the dry-run trader (dryrun.py) with its simulated fills
replaced by immediate-or-cancel orders on both venues (orders.py). Its account
(``live_trades``) holds exactly what the venues hold: each venue's cash is its real
balance (Kalshi's summed over its exchange shards), re-read every ``ACCOUNT_EVERY_S``
and right after a trade or a settlement, so whatever settled trades pay out is traded
again. It has the dry run's limits: series with a clean settlement record, ``live_max_stake_frac`` of the account a trade,
``live_min_profit_usd`` expected, the daily loss limit, no market the account already
holds, and nothing near the Kalshi location check lapsing. On top of those:

- **One trade at a time**, at most ``live_max_trades_per_day`` a day, and orders on
  one venue at least ``ORDER_GAP_S`` apart.
- **Real cash.** A pick is sized to the venue's real cash (on Kalshi, the cash on the
  exchange shard of the pick's market), and to its stake cap of that venue's cash plus
  what open trades have tied up. Nothing is traded before the balances are first read.
- **Fresh books.** Nothing is sent while either leg's feed connection is down or has
  gone quiet for ``FEED_FRESH_S``.
- **Today's losses** count the moment they happen (a leg sold back at a loss, a trade
  settling), not every few minutes.

A trade sends the Polymarket US leg first (``live_lead_venue``) at the planned limit:
its books are thin and its stream can't be checked for gaps, so it is the leg that
misses, and a first leg that misses costs nothing. Kalshi's leg follows for what that
filled, at up to break-even against what it really cost (the order fills at the book's
prices, so the higher limit only matters if the planned price has gone). Anything still
unhedged is got out of the cheaper way, by the books as they stand: the missing
contracts bought past break-even, or the extra ones sold back; either way at a loss of
no more than ``live_unwind_max_loss`` a contract. An
order is never sent twice: one whose reply doesn't say what happened (a timeout, a 5xx)
is read back from the venue's position.

Before any order, three things a pick must pass that paper trading doesn't ask for
(``arbscan backtest`` replays the recorded trades with and without them):

- **The Polymarket US book is checked against the exchange** (confirm.BookCheck) a
  moment before the pick comes due. A streamed book that had frozen is repaired by the
  check, and the pick priced on it is gone.
- **Both prices have settled**: the older of the two legs' prices has held still
  ``live_settle_s``, as well as the newer one ``paper_quiet_s``.
- **No miss on that market just now**: a first leg that found nothing keeps the market
  out for ``COOL_S``.
- **Kalshi is trading**: its exchange status (read with the account) says the market's
  shard takes orders, its published hours don't close it within ``CLOSE_MARGIN_S``
  (every Thursday 03:00-05:00 ET), and no order was just turned away as paused. Its
  books stay up while it's closed, so the scanner goes on seeing windows there.

Every order is written to a journal file (``live_journal.jsonl`` next to the
database) before it's sent and again once its outcome is known, and kept in
``live_orders`` (mode ``live``).

**Stopping.** The trader stops itself by writing ``live.halt`` next to the database,
with the reason, when a position is left unhedged, when an order's outcome can't be
read back, when orders keep being rejected, or when it starts up and finds an order in
the journal whose outcome it never learned (a crash mid-trade). Creating the file by
hand stops it too (``arbscan live-halt``). A stop lasts until the file is removed
(``arbscan live-resume``), restarts included.
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import confirm
from .dryrun import SERIES_SQL, Guard, clean_series
from .fees import order_fee, per_contract
from .orders import EPS, KalshiTrading, Order, PMTrading, Result, paused, record, status_of
from .paper import VENUES, PaperTrader, _opposite, breakeven_price, limit_for
from .shards import Rebalancer
from .store import open_db

log = logging.getLogger(__name__)

HALT_FILE = "live.halt"
JOURNAL_FILE = "live_journal.jsonl"
ACCOUNT_EVERY_S = 30.0  # re-read balances and positions (this also keeps the connections warm)
RECORD_EVERY_S = 600.0  # re-read the series' settlement records
POLL_S = 2.0  # how often the stop file is looked at
FEED_FRESH_S = 5.0
ORDER_GAP_S = 0.1
RESOLVE_WAITS_S = (1.5, 3.0)  # read an unclear order's position this long after it, then again
MAX_REJECTS = 3  # stop after this many rejected orders in a row
COOL_S = 60.0  # a market whose first leg found nothing is left alone this long
SCHEDULE_EVERY_S = 3600.0  # re-read Kalshi's trading hours
CLOSE_MARGIN_S = 120.0  # no trade starts this close to Kalshi's scheduled close
PAUSED_S = 60.0  # after an order turned away for paused trading, Kalshi is left alone this long
ET = ZoneInfo("America/New_York")  # Kalshi's schedule is in Eastern time
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CASH_MARGIN = 0.10  # leave this much of a venue's real cash unspent (fees round up)
TICK = 0.01  # orders the trader prices itself go on whole cents, a valid price on every market
DUST = 0.005  # contracts: less than this held is nothing held
NAMES = {"K": "Kalshi", "P": "Polymarket US"}


def _sign(o: Order) -> int:
    """How one contract of ``o`` moves the venue's position (+ YES, - NO)."""
    return (1 if o.side == "yes" else -1) * (1 if o.action == "buy" else -1)


def tick_down(p: float) -> float:
    return math.floor(p / TICK + 1e-6) * TICK


def tick_up(p: float) -> float:
    return math.ceil(p / TICK - 1e-6) * TICK


class Halt(Exception):
    """The trader stopped itself mid-trade; no further order is sent."""


@dataclass
class Fills:
    """What one venue's orders in a trade bought and sold back."""
    bought: float = 0.0
    cost: float = 0.0  # price paid for what was bought, fees apart
    sold: float = 0.0
    proceeds: float = 0.0  # price received for what was sold, fees apart
    fees: float = 0.0

    @property
    def held(self) -> float:
        return self.bought - self.sold

    @property
    def out(self) -> float:
        """Net cash spent, fees included."""
        return self.cost + self.fees - self.proceeds

    def add(self, res: Result) -> None:
        self.fees += res.fees
        if res.filled <= EPS:
            return
        price = res.avg_price if res.avg_price is not None else res.order.limit
        if res.order.action == "buy":
            self.bought += res.filled
            self.cost += price * res.filled
        else:
            self.sold += res.filled
            self.proceeds += price * res.filled


class Journal:
    """Every live order, written down before it's sent and again once its outcome is
    known, so an order cut off by a crash is found on the next start."""

    def __init__(self, path):
        self.path = Path(path)

    def _add(self, rec: dict) -> None:
        with open(self.path, "a") as f:  # closed at once, so it's with the OS before the order goes
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def send(self, trade: str, o: Order, before: float) -> None:
        self._add({"ts": time.time(), "event": "send", "trade": trade, "id": o.client_id, "venue": o.venue,
                   "market": o.market, "side": o.side, "action": o.action, "qty": o.qty, "limit": o.limit,
                   "before": before})

    def done(self, res: Result) -> None:
        self._add({"ts": time.time(), "event": "done", "id": res.order.client_id, "status": res.status,
                   "filled": res.filled, "avg_price": res.avg_price, "fees": res.fees, "order_id": res.order_id,
                   "error": res.error})

    def clear(self, ids, why: str) -> None:
        self._add({"ts": time.time(), "event": "cleared", "ids": list(ids), "why": why})

    def unresolved(self) -> list[dict]:
        """Orders sent whose outcome was never learned, and not since cleared by hand."""
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        sends, known = {}, set()
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a line cut short by a crash
            if rec.get("event") == "send":
                sends[rec["id"]] = rec
            elif rec.get("event") == "done" and rec.get("status") != "unknown":
                known.add(rec["id"])
            elif rec.get("event") == "cleared":
                known.update(rec.get("ids") or [])
        return [r for i, r in sends.items() if i not in known]


def early_loss(t) -> float:
    """What a trade that stayed open has lost for certain, counted
    toward the day's losses when it happened. Its settlement counts the rest of the
    trade's P&L. A trade unwound completely settles at once, its P&L the whole loss."""
    if t.get("note") == "unwound":
        return 0.0
    # Sold back at a loss, or the pair finished past break-even (its locked profit is
    # then below zero, and already includes anything a sale lost).
    return max(0.0, t.get("unwind_loss") or 0.0, -(t.get("locked_profit") or 0.0))


def _hhmm(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def kalshi_open(schedule: dict, at: float) -> bool:
    """Whether Kalshi's published hours (``/exchange/schedule``) have it trading at ``at``.
    Its standard hours list each weekday's open periods in Eastern time ("00:00" as a
    close is midnight at the end of the day); maintenance windows close it as well."""
    for w in schedule.get("maintenance_windows") or []:
        start, end = (_when(w.get(k)) for k in ("start_datetime", "end_datetime"))
        if start is not None and end is not None and start <= at < end:
            return False
    t = datetime.fromtimestamp(at, ET)
    now = t.hour * 60 + t.minute
    for period in schedule.get("standard_hours") or []:
        start, end = _when(period.get("start_time")), _when(period.get("end_time"))
        if (start is not None and at < start) or (end is not None and at >= end):
            continue
        hours = period.get(DAYS[t.weekday()])
        if hours is None:
            continue
        for h in hours:
            o, c = _hhmm(h["open_time"]), _hhmm(h["close_time"]) or 24 * 60
            if o <= now < c:
                return True
        return False
    return True  # no hours published for now: go by the exchange's status alone


def _when(text) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


class LiveGuard(Guard):
    """The dry run's limits plus the live trader's own; see the module docstring."""

    transient = frozenset({"a trade in flight", "feed not fresh", "an order there just missed",
                           "Kalshi trading paused"})  # look again shortly

    def __init__(self, cfg, kmeta: dict, halt_path, feeds_fresh=None):
        super().__init__(cfg, kmeta)
        self.halt_path = Path(halt_path)
        self.feeds_fresh = feeds_fresh or (lambda pair: True)
        self.halted: str | None = None
        self.stuck = False  # stopped, but the stop file couldn't be written: only a restart clears it
        self.busy: set[str] = set()  # the live trader's pairs with orders in flight
        self.day = date.today()
        self.trades_today = 0
        self.pnl_today = 0.0
        self.on_short = None  # told (shard, dollars) when a pick finds too little cash on its Kalshi shard
        self.stake_cap = lambda: 0.0
        self.cooling: dict[str, float] = {}  # market -> when a pick on it may be taken again
        self.k_trading: dict[int, bool] | None = None  # shard -> takes orders now (-1: the whole exchange)
        self.k_schedule: dict | None = None
        self.k_paused_until = 0.0

    def __call__(self, pair, days) -> str | None:
        if self.halted:
            return "halted"
        if self.busy:
            return "a trade in flight"
        self._roll()
        if self.trades_today >= self.cfg.live_max_trades_per_day:
            return "daily trade limit"
        if not self.feeds_fresh(pair):
            return "feed not fresh"
        if self.kalshi_closed(pair.kalshi):
            return "Kalshi trading paused"
        if self.cooling and max(self.cooling.get(pair.kalshi, 0.0), self.cooling.get(pair.pm, 0.0)) > time.time():
            return "an order there just missed"
        why = super().__call__(pair, days)
        if why and why.startswith("no cash on Kalshi shard") and self.on_short and self.shard_cash is not None:
            shard = getattr(self.kmeta.get(pair.kalshi), "shard", 0)
            self.on_short(shard, self.stake_cap() - self.shard_cash.get(shard, 0.0))
        return why

    def kalshi_closed(self, ticker: str) -> bool:
        """Kalshi won't take an order in ``ticker`` now, or a trade started now could
        still be under way when it closes: its weekly maintenance, a halt, a shard
        paused. Trading one leg while the other can't be traded is how a trade ends up
        sold back or left unhedged."""
        now = time.time()
        if now < self.k_paused_until:
            return True
        if self.k_trading is not None:
            shard = getattr(self.kmeta.get(ticker), "shard", 0)
            if not self.k_trading.get(shard, self.k_trading.get(-1, True)):
                return True
        if self.k_schedule and not (kalshi_open(self.k_schedule, now)
                                    and kalshi_open(self.k_schedule, now + CLOSE_MARGIN_S)):
            return True
        return False

    def kalshi_paused(self) -> None:
        """An order was just turned away for paused trading."""
        self.k_paused_until = time.time() + PAUSED_S

    async def read_exchange(self, kalshi: KalshiTrading, schedule: bool) -> None:
        self.k_trading = await kalshi.trading_status()
        if schedule:
            self.k_schedule = await kalshi.schedule()

    def passing(self, pair, days) -> str | None:
        """The refusals that outlast a moment (``__call__`` without the passing ones, and
        without telling anyone a shard ran short): is this pair worth getting ready for?"""
        if self.halted:
            return "halted"
        if self.kalshi_closed(pair.kalshi):
            return "Kalshi trading paused"
        if self.cooling and max(self.cooling.get(pair.kalshi, 0.0), self.cooling.get(pair.pm, 0.0)) > time.time():
            return "an order there just missed"
        return Guard.__call__(self, pair, days)

    def cool(self, market: str) -> None:
        """Leave ``market`` alone for a while: an order there found nothing of what its book showed."""
        now = time.time()
        self.cooling = {m: t for m, t in self.cooling.items() if t > now}
        self.cooling[market] = now + COOL_S

    def _roll(self) -> None:
        if date.today() != self.day:
            self.day, self.trades_today, self.pnl_today, self.lost_today = date.today(), 0, 0.0, 0.0

    def record_pnl(self, pnl: float) -> None:
        self._roll()
        self.pnl_today += pnl
        self.lost_today = max(0.0, -self.pnl_today)

    def read_records(self, db_path: str, table: str) -> None:
        """The series' settlement records. Today's losses are counted as they happen."""
        db = open_db(db_path)
        try:
            self.series = clean_series(db.execute(SERIES_SQL).fetchall(), self.cfg.live_series_min_settled,
                                       self.cfg.live_series_max_void)
        finally:
            db.close()

    def halt(self, reason: str) -> str:
        """Stop trading until the stop file is removed. Returns the reason."""
        try:
            with open(self.halt_path, "a") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} {reason}\n")
        except OSError as e:
            self.stuck = True
            log.error("live trading: can't write %s (%s); stopped until a restart", self.halt_path, e)
        self.halted = self.halted or reason
        log.error("live trading stopped: %s", reason)
        return reason

    def poll(self) -> None:
        """Trading stops while the stop file exists, whoever made it."""
        try:
            text = self.halt_path.read_text().strip()
        except FileNotFoundError:
            if not self.stuck:
                self.halted = None
            return
        except OSError:
            return
        first = text.splitlines()[0] if text else ""
        stamp, _, rest = first.partition(" ")
        try:
            datetime.fromisoformat(stamp)
            reason = rest
        except ValueError:
            reason = first
        self.halted = reason or f"stopped by hand ({self.halt_path.name})"


class LiveTrader(PaperTrader):
    """Trades the dry run's picks with real orders; see the module docstring."""

    def __init__(self, cfg, db, out, latency, kbooks, pbooks, kmeta, kalshi: KalshiTrading, pm: PMTrading,
                 guard: LiveGuard, journal: Journal, wake=None, check: "confirm.BookCheck | None" = None):
        live_cfg = replace(cfg, bankroll_usd=cfg.live_bankroll_usd, paper_min_profit_usd=cfg.live_min_profit_usd)
        super().__init__(live_cfg, db, out, latency, kbooks, pbooks, kmeta, wake=wake, name="live", guard=guard,
                         max_stake=cfg.live_max_stake_frac)
        self.venues = {"K": kalshi, "P": pm}
        self.journal = journal
        self.check = check  # the Polymarket US book, checked against the exchange before a trade
        guard.busy = self.busy
        self.last_order = {v: 0.0 for v in VENUES}
        self.rejects, self.last_reject = 0, None
        self.account_stale = True  # re-read the account before long
        self.resolve_waits = RESOLVE_WAITS_S
        self.shard_short: tuple | None = None  # (pair, shard, dollars missing, cash there) from the latest sizing
        guard.stake_cap = lambda: self.stake_cap() or 0.0
        midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
        for r in db.execute(f"SELECT ts, status, settled_ts, pnl, unwind_loss, locked_profit, note FROM {self.table} "
                            "WHERE ts >= ? OR settled_ts >= ?", (midnight, midnight)):
            r = dict(r)
            guard.trades_today += r["ts"] >= midnight
            if r["ts"] >= midnight:
                guard.record_pnl(-early_loss(r))
            if r["status"] == "settled" and (r["settled_ts"] or 0) >= midnight:
                guard.record_pnl((r["pnl"] or 0.0) + early_loss(r))

    def _budget(self, pair, days: float | None) -> dict[str, float]:
        """The stake cap, but no more than the venue holds: on Kalshi, the cash on the
        shard of this pair's market."""
        budget = super()._budget(pair, days)
        g = self.guard
        shard = getattr(self.kmeta.get(pair.kalshi), "shard", 0)
        real = {"K": (g.shard_cash or {}).get(shard, 0.0), "P": g.pm_cash or 0.0}
        out = {v: min(budget[v], real[v] - self.reserved[v] - CASH_MARGIN) for v in VENUES}
        # Too little on the shard for a full trade's Kalshi leg: a trade sized to it came
        # out smaller than it could have (see _execute), or none could be made at all.
        full = min(budget["K"], self.stake_cap() or budget["K"])
        self.shard_short = (pair.id, shard, full - out["K"], out["K"]) if out["K"] < full else None
        if self.shard_short and out["K"] < 1.0 <= budget["P"] and g.on_short:
            g.on_short(shard, full - out["K"])
        return out

    def _hold(self, steady: dict[str, float]) -> float:
        """Paper trading's quiet time for the leg that moved last, and ``live_settle_s``
        for the other: with both just moved, the market is still repricing."""
        return max(super()._hold(steady), self.cfg.live_settle_s - max(steady.values()))

    def _ahead(self, pair, days: float | None, w, eta: float, seen=None) -> None:
        """Have the Polymarket US book checked just before a pick comes due, so the
        check costs the trade no time. Only for a window that could still be traded:
        one that stays open without being worth a trade isn't read over and over."""
        if (self.check is None or self.busy or w.poor or self.guard.passing(pair, days) is not None
                or (seen is not None and seen.profit < self.cfg.paper_min_profit_usd)):
            return
        self.check.ask_in(pair.pm, eta - confirm.LEAD_S)

    def _confirmed(self, pair, ask: bool = True) -> bool:
        if self.check is None or self.check.ok(pair.pm):
            return True
        if ask:
            self.check.ask(pair.pm)
        return False

    def _cash(self, venue: str, market: str) -> float:
        """The venue's real cash an order in ``market`` can draw on."""
        g = self.guard
        if venue == "P":
            return g.pm_cash or 0.0
        return (g.shard_cash or {}).get(getattr(self.kmeta.get(market), "shard", 0), 0.0)

    def _cap(self, venue: str, market: str, coef: float, qty: float, limit: float, other: Fills) -> float:
        """The most the second leg may pay: break-even against what the first really
        cost, never under its planned limit, and no more than the venue's cash covers."""
        cap = max(limit, tick_down(breakeven_price(coef, 1.0 - other.out / other.held)))
        while cap > limit + EPS and qty * (cap + coef / 4) > self._cash(venue, market) - CASH_MARGIN:
            cap = max(limit, cap - TICK)
        return cap

    def sync_cash(self) -> None:
        """The account's cash is what the venues hold, once their balances are known."""
        g = self.guard
        if g.shard_cash is not None:
            self.cash["K"] = sum(g.shard_cash.values())
        if g.pm_cash is not None:
            self.cash["P"] = g.pm_cash

    # --- executing ----------------------------------------------------------------------

    async def _execute(self, t: dict, ticker: str, slug: str, k_coef: float, p_coef: float, reserve: dict,
                       decide_s: float) -> None:
        short = self.shard_short
        if (short and short[0] == t["pair"] and reserve["K"] + k_coef / 4 + t["k_limit"] >= short[3]
                and self.guard.on_short):
            self.guard.on_short(short[1], short[2])  # another contract wouldn't have fit on its shard
        mk, side = {"K": ticker, "P": slug}, {"K": t["k_side"], "P": t["p_side"]}
        coef = {"K": k_coef, "P": p_coef}
        limits = {"K": t["k_limit"], "P": t["p_limit"]}
        steady = t.pop("_steady")
        lead = self.cfg.live_lead_venue
        if lead not in VENUES:  # never both at once: the second leg is sized to what the first got
            lead = "K" if steady["K"] < steady["P"] else "P"
        follow = "P" if lead == "K" else "K"
        got = {v: Fills() for v in VENUES}
        tried: dict[str, float] = {}  # venue -> the most a buy there has already offered
        t.update(unwind_venue=None, unwind_qty=0.0, unwind_loss=0.0, k_delay_ms=None, p_delay_ms=None, _orders=[])
        self.guard.epoch += 1
        self.guard._roll()
        self.guard.trades_today += 1
        stopped = None
        try:
            try:
                first = await self._send(t, Order(lead, mk[lead], side[lead], "buy", t["planned_size"],
                                                  limits[lead]), coef[lead], got)
                if got[lead].held >= 1:
                    # The other leg for what the first filled, at up to break-even: it
                    # fills at the book's prices, so the cap only matters if the planned
                    # price has gone, and then it saves selling the first leg back.
                    n = math.floor(got[lead].held + EPS)
                    cap = self._cap(follow, mk[follow], coef[follow], n, limits[follow], got[lead])
                    second = await self._send(t, Order(follow, mk[follow], side[follow], "buy", n, cap),
                                              coef[follow], got)
                    if self._met_book(second):
                        tried[follow] = cap
                elif first.status == "none":
                    self._missed(mk[lead], lead)
                await self._even_up(t, mk, side, coef, got, tried)
            except Halt as e:
                stopped = str(e)
            except Exception as e:
                log.exception("live trade failed")
                stopped = self.guard.halt(f"a live trade failed unexpectedly ({type(e).__name__}: {e}); "
                                          "check its orders")
            finally:
                for v in VENUES:
                    self.reserved[v] -= reserve[v]
                reserve = {"K": 0.0, "P": 0.0}
            self._finish(t, got, lead, steady, stopped)
        except Exception as e:
            log.exception("writing up a live trade failed")
            self.guard.halt(f"writing up a live trade failed ({type(e).__name__}: {e}); check its orders")
            for v in VENUES:
                self.reserved[v] -= reserve[v]
        finally:
            self.busy.discard(t["pair"])
            self.account_stale = True
            if self.rejects >= MAX_REJECTS and not self.guard.halted:
                self.guard.halt(f"the last {self.rejects} orders were rejected (latest: {self.last_reject})")

    def _open(self, venue: str, market: str) -> bool:
        """The venue takes orders in ``market`` now."""
        return venue != "K" or not self.guard.kalshi_closed(market)

    @staticmethod
    def _met_book(res: Result) -> bool:
        """The order reached the book and took what was there at its limit: one that was
        rejected, or whose outcome had to be read from the position, may never have."""
        return res.status in ("filled", "partial", "none") and not res.error

    def _missed(self, market: str, venue: str) -> None:
        """The first leg found nothing of what its book showed: that book isn't to be
        trusted for a while (and the next pick on it is checked afresh)."""
        self.guard.cool(market)
        if venue == "P" and self.check is not None:
            self.check.forget(market)

    async def _send(self, t: dict, o: Order, coef: float, got: dict[str, Fills]) -> Result:
        """Send one order, learn what it did, and write it down before and after."""
        wait = self.last_order[o.venue] + ORDER_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self.last_order[o.venue] = time.monotonic()
        before = self.guard.positions.get(o.market, 0.0)
        self.journal.send(t["id"], o, before)
        t["_orders"].append(o.client_id)
        try:
            res = await self.venues[o.venue].place(o)
        except Exception as e:  # a reply we couldn't read: the order may still have filled
            res = Result(o, "unknown", sent=time.time(), error=f"{type(e).__name__}: {e}",
                         body=self.venues[o.venue].body(o))
        if res.status == "unknown":
            res = await self._resolve(res, before, coef)
        self.journal.done(res)
        record(self.out, res, "live", t["id"])
        got[o.venue].add(res)
        if res.filled > EPS:
            self._moved(o, res, before)
        key = f"{o.venue.lower()}_delay_ms"
        if t.get(key) is None and res.sent:  # decided -> first order at the exchange
            t[key] = 1000 * ((res.exch_ts or res.sent + res.rtt_ms / 2000) - t["ts"])
        log.info("live %s %s %g %s in %s at %.4f: %s, filled %g%s", NAMES[o.venue], o.action, o.qty, o.side.upper(),
                 o.market, o.limit, res.status, res.filled, f" [{res.error}]" if res.error else "")
        if res.status == "rejected" and o.venue == "K" and paused(res.reply):
            self.guard.kalshi_paused()  # the exchange's state, not a fault in the order: no halt for it
        elif res.status == "rejected":
            self.rejects, self.last_reject = self.rejects + 1, f"{NAMES[o.venue]} {o.market}: {res.error}"
        elif res.status == "unknown":
            raise Halt(self.guard.halt(
                f"couldn't tell whether an order filled ({NAMES[o.venue]} {o.action} {o.qty:g} {o.side.upper()} "
                f"in {o.market}: {res.error}); check that position"))
        else:
            self.rejects = 0
        return res

    async def _resolve(self, res: Result, before: float, coef: float) -> Result:
        """Settle an order whose reply didn't say what happened: Polymarket US can read the
        order back; otherwise the venue's position tells how much filled (priced at the
        limit, the worst it can have been)."""
        o = res.order
        if o.venue == "P" and res.order_id:
            try:
                res = await self.venues["P"].resolve(res)
            except Exception as e:
                log.warning("live: reading Polymarket US order %s back failed: %s", res.order_id, e)
            if res.status != "unknown":
                return res
        for wait in self.resolve_waits:
            await asyncio.sleep(wait)
            try:
                pos = await self.venues[o.venue].position(o.market)
            except Exception as e:
                log.warning("live: reading the %s position in %s failed: %s", NAMES[o.venue], o.market, e)
                continue
            n = max(0.0, min(o.qty, (pos - before) * _sign(o)))
            res.filled, res.status = n, status_of(n, o.qty)
            res.avg_price = o.limit if n > EPS else None
            res.fees = order_fee(o.venue, coef, [(o.limit, n)]) if n > EPS else 0.0
            res.error = (f"{res.error}; " if res.error else "") + "outcome read from the position"
            return res
        return res

    def _moved(self, o: Order, res: Result, before: float) -> None:
        """Keep the account's positions and cash current between reads."""
        g = self.guard
        g.positions[o.market] = before + _sign(o) * res.filled
        if abs(g.positions[o.market]) > EPS:
            g.held.add(o.market)
        else:
            g.held.discard(o.market)
        price = res.avg_price if res.avg_price is not None else o.limit
        delta = (-1 if o.action == "buy" else 1) * price * res.filled - res.fees
        if o.venue == "K" and g.shard_cash is not None:
            shard = getattr(self.kmeta.get(o.market), "shard", 0)
            g.shard_cash[shard] = g.shard_cash.get(shard, 0.0) + delta
        elif o.venue == "P" and g.pm_cash is not None:
            g.pm_cash += delta

    async def _even_up(self, t: dict, mk: dict, side: dict, coef: dict, got: dict[str, Fills],
                       tried: dict[str, float] | None = None) -> None:
        """An unequal fill: buy the missing leg up to break-even (unless the order just
        sent there already offered that much), then sell back what's still unhedged,
        but not below the floor. Stops trading if some is left over."""
        gap = got["K"].held - got["P"].held
        if abs(gap) < 1:
            return
        long_v, short_v = ("K", "P") if gap > 0 else ("P", "K")
        x = math.floor(abs(gap) + EPS)
        unit_long = got[long_v].out / got[long_v].held  # each unhedged contract's cost, fees included
        cap = tick_down(breakeven_price(coef[short_v], 1.0 - unit_long))
        tried = {} if tried is None else tried
        if not self._open(short_v, mk[short_v]):
            tried[short_v] = 1.0  # nothing can be bought there now: straight to selling back
        if cap >= TICK and cap > tried.get(short_v, 0.0) + EPS:
            res = await self._send(t, Order(short_v, mk[short_v], side[short_v], "buy", x, cap), coef[short_v], got)
            if self._met_book(res):
                tried[short_v] = cap
            x = math.floor(got[long_v].held - got[short_v].held + EPS)
        if x < 1:
            return
        floor = tick_up(max(TICK, got[long_v].cost / got[long_v].bought - self.cfg.live_unwind_max_loss))
        # Out the cheaper way. Finishing the pair past break-even loses a known few cents;
        # selling back loses the spread and a second fee, and a thin book may have no bid.
        price = self._finish_at(short_v, mk[short_v], side[short_v], coef[short_v], x, unit_long,
                                tried.get(short_v, 0.0), long_v, mk[long_v], side[long_v], coef[long_v], floor)
        if price is not None:
            await self._send(t, Order(short_v, mk[short_v], side[short_v], "buy", x, price), coef[short_v], got)
            x = math.floor(got[long_v].held - got[short_v].held + EPS)
            if x < 1:
                return
        sale = await self._send(t, Order(long_v, mk[long_v], side[long_v], "sell", x, floor, reduce_only=True),
                                coef[long_v], got)
        if sale.filled > EPS:
            received = sale.filled * (sale.avg_price if sale.avg_price is not None else floor) - sale.fees
            t.update(unwind_venue=long_v, unwind_qty=sale.filled, unwind_loss=sale.filled * unit_long - received)
        left = math.floor(got[long_v].held - got[short_v].held + EPS)
        if left >= 1:
            raise Halt(self.guard.halt(
                f"{left:g} {side[long_v].upper()} contracts left unhedged in {mk[long_v]} on {NAMES[long_v]}: "
                f"nothing bid {floor:.2f} or more to sell them back"))

    def _finish_at(self, short_v: str, market: str, side: str, coef: float, x: int, unit_long: float, tried: float,
                   long_v: str, long_market: str, long_side: str, long_coef: float, floor: float) -> float | None:
        """The price at which buying the ``x`` missing contracts loses less than selling
        the unhedged ones back would, by the books as they stand, or None to sell back.
        Never more than ``live_unwind_max_loss`` a contract, the most a sale may lose."""
        if not self._open(short_v, market):
            return None
        asks = self._ladder(short_v, market, side)
        need = limit_for(asks, x) if asks else None
        if need is None:
            return None
        price = tick_up(need)
        if price <= tried + EPS:
            return None  # an order just offered that much there and came up short
        finish = unit_long + price + per_contract(coef, price) - 1.0
        bids = self._ladder(long_v, long_market, _opposite(long_side))  # an offer of the other side at p bids 1 - p
        bid = round(1.0 - bids[0][0], 4) if bids else None
        sell = math.inf if bid is None or bid < floor - EPS else unit_long - (bid - per_contract(long_coef, bid))
        if finish > min(sell, self.cfg.live_unwind_max_loss) + EPS:
            return None
        if x * (price + coef / 4) > self._cash(short_v, market) - CASH_MARGIN:
            return None
        return price

    def _finish(self, t: dict, got: dict[str, Fills], lead: str, steady: dict, stopped: str | None) -> None:
        """Write the trade up the way paper trades are, and move the account's cash."""
        hold = {v: got[v].held for v in VENUES}
        out = {v: got[v].out for v in VENUES}
        self.sync_cash()  # the fills already moved the venues' balances (_moved)
        t["books"] = json.dumps({"seen": t.pop("_seen"), "lead": lead, "orders": t.pop("_orders"),
                                 "steady": {v: round(x, 3) for v, x in steady.items()}}, separators=(",", ":"))
        t.update(k_qty=got["K"].bought, p_qty=got["P"].bought, k_fees=got["K"].fees, p_fees=got["P"].fees,
                 k_hold=hold["K"], p_hold=hold["P"], k_out=out["K"], p_out=out["P"],
                 locked_profit=min(hold["K"], hold["P"]) - out["K"] - out["P"])
        if hold["K"] < DUST and hold["P"] < DUST and stopped is None:
            moved = abs(out["K"]) + abs(out["P"]) > EPS
            t.update(status="settled" if moved else "missed", settled_ts=time.time(), payout_k=0.0, payout_p=0.0,
                     pnl=-out["K"] - out["P"], note="unwound" if moved else "no fill")
            self.realized += t["pnl"]
            self.guard.record_pnl(t["pnl"])
        else:
            t["status"] = "open"
            self.guard.record_pnl(-early_loss(t))  # a leftover leg sold back: lost now, not at settlement
            # Polymarket US can fill part of a contract; Kalshi's leg is whole contracts,
            # and under one contract nothing is sold back. It's held, and said.
            notes = [f"{abs(hold['K'] - hold['P']):g} contracts unhedged"] if abs(hold["K"] - hold["P"]) >= DUST else []
            t["note"] = "; ".join(notes + ([f"stopped: {stopped}"] if stopped else [])) or None
            self.open[t["id"]] = t
            for v in VENUES:
                self.tied[v] += out[v]
        self.stats[t["status"] if t["status"] != "settled" else "unwound"] += 1
        self._write(t)
        log.info("live %s %s %s: planned %d for $%.2f, got K %g / P %g, locked $%.2f", t["status"], t["pair"],
                 t["direction"], t["planned_size"], t["planned_profit"], hold["K"], hold["P"], t["locked_profit"])

    # --- settling -----------------------------------------------------------------------

    def settle(self, lookup, finished: set[str]) -> int:
        before = dict(self.open)
        n = super().settle(lookup, finished)
        for tid, t in before.items():
            if tid not in self.open:
                self.guard.record_pnl((t["pnl"] or 0.0) + early_loss(t))  # what wasn't counted already
        if n:
            self.account_stale = True  # read what the venues actually paid out
        return n


class LiveRun:
    """The live trader with its limits, journal and stop file, for the streaming scanner."""

    def __init__(self, cfg, db, out, latency, kbooks, pbooks, kmeta, kalshi: KalshiTrading, pm: PMTrading,
                 feeds_fresh=None, wake=None, reprice=None):
        """``reprice(slug)``: price the pairs on a Polymarket US market again (its book was just checked)."""
        self.cfg, self.kalshi, self.pm, self.db = cfg, kalshi, pm, db
        folder = Path(cfg.db_path).parent
        self.guard = LiveGuard(cfg, kmeta, folder / HALT_FILE, feeds_fresh)
        self.journal = Journal(folder / JOURNAL_FILE)
        self.check = confirm.BookCheck(pm.book, pbooks, reprice)
        self.trader = LiveTrader(cfg, db, out, latency, kbooks, pbooks, kmeta, kalshi, pm, self.guard, self.journal,
                                 wake=wake, check=self.check)
        self.shards = Rebalancer(kalshi, self.guard, self.trader, kmeta) if cfg.live_shard_rebalance else None
        if self.shards is not None:
            self.guard.on_short = self.shards.ran_short
        self.guard.poll()
        stuck = self.journal.unresolved()
        if stuck and not self.guard.halted:
            self.guard.halt(f"{len(stuck)} order(s) sent before the last stop have no known outcome ("
                            + ", ".join(f"{NAMES[s['venue']]} {s['market']}" for s in stuck[:5])
                            + "); check those positions, then run arbscan live-resume --checked")

    async def run(self, stop: asyncio.Event) -> None:
        """Keep the stop file, the settlement records and the account current."""
        next_records = next_account = next_schedule = 0.0
        while not stop.is_set():
            self.guard.poll()
            now = time.monotonic()
            if now >= next_records:
                next_records = now + RECORD_EVERY_S
                try:
                    await asyncio.to_thread(self.guard.read_records, self.cfg.db_path, self.trader.table)
                except Exception as e:
                    log.warning("live: reading settlement records failed: %s", e)
            if (now >= next_account or self.trader.account_stale) and not self.trader.busy:
                next_account, self.trader.account_stale = now + ACCOUNT_EVERY_S, False
                try:
                    await self.guard.read_account(self.kalshi, self.pm)
                    self.trader.sync_cash()
                except Exception as e:
                    log.warning("live: reading the accounts failed: %s", e)
                try:
                    was = self.guard.k_trading
                    await self.guard.read_exchange(self.kalshi, schedule=now >= next_schedule)
                    if now >= next_schedule:
                        next_schedule = now + SCHEDULE_EVERY_S
                    if was is not None and was.get(-1, True) != self.guard.k_trading.get(-1, True):
                        log.warning("live: Kalshi trading %s", "resumed" if self.guard.k_trading.get(-1) else "paused")
                except Exception as e:
                    log.warning("live: reading Kalshi's exchange status failed: %s", e)
                if self.shards is not None and not self.trader.account_stale:
                    try:
                        if await self.shards.step(self.db):
                            self.trader.sync_cash()
                    except Exception as e:
                        log.warning("live: rebalancing Kalshi's shards failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass

    def snapshot(self) -> dict:
        g, cfg = self.guard, self.cfg
        return {**self.trader.snapshot(), "halted": g.halted, "rejects": self.trader.rejects,
                "limits": {"series": None if g.series is None else len(g.series), "held": len(g.held),
                           "shard_cash": g.shard_cash, "pm_cash": g.pm_cash, "attested_until": g.attested_until or None,
                           "lost_today": g.lost_today, "trades_today": g.trades_today,
                           "max_trades": cfg.live_max_trades_per_day, "max_stake": self.trader.stake_cap(),
                           "max_stake_frac": cfg.live_max_stake_frac,
                           "min_profit": cfg.live_min_profit_usd, "daily_loss": cfg.live_daily_loss_usd,
                           "unwind_max_loss": cfg.live_unwind_max_loss,
                           "shards": self.shards.snapshot() if self.shards is not None else None},
                "execution": {"kalshi_open": not g.kalshi_closed(""), "lead": cfg.live_lead_venue,
                              "settle_s": cfg.live_settle_s,
                              "quiet_s": cfg.paper_quiet_s, "cool_s": COOL_S, "book_check": self.check.snapshot()}}


# --- arbscan live-check / live-halt / live-resume --------------------------------------

def _paths(cfg) -> tuple[Path, Path]:
    folder = Path(cfg.db_path).parent
    return folder / HALT_FILE, folder / JOURNAL_FILE


async def check(cfg) -> None:
    """``arbscan live-check``: what the live trader would start with. Reads only."""
    from .auth import KalshiSigner, PMSigner
    from .http import make_client

    halt_path, journal_path = _paths(cfg)
    print(f"live trading: {'ON' if cfg.live_trading else 'off'} (live_trading in config.toml)")
    print(f"limits: the venues' real balances, {100 * cfg.live_max_stake_frac:g}% of them a trade, "
          f"${cfg.live_min_profit_usd:g} expected profit, ${cfg.live_daily_loss_usd:g} daily loss, "
          f"{cfg.live_max_trades_per_day} trades a day, sell-back floor {100 * cfg.live_unwind_max_loss:g}c under cost")
    guard = LiveGuard(cfg, {}, halt_path)
    guard.poll()
    print(f"stopped: {guard.halted}" if guard.halted else "stopped: no")
    stuck = Journal(journal_path).unresolved()
    for s in stuck:
        print(f"  no known outcome: {s['venue']} {s['action']} {s['qty']:g} {s['side'].upper()} in {s['market']} "
              f"(sent {datetime.fromtimestamp(s['ts']).isoformat(timespec='seconds')})")
    if not cfg.can_stream:
        print("API keys: not set for both venues")
        return
    guard.read_records(cfg.db_path, "live_trades")
    print(f"series with a clean record: {len(guard.series or ())}")
    async with make_client() as c:
        await guard.read_account(
            KalshiTrading(c, cfg.kalshi_base, KalshiSigner.from_file(cfg.kalshi_key_id, cfg.kalshi_private_key_path)),
            PMTrading(c, cfg.pmus_trade_base, PMSigner(cfg.pmus_key_id, cfg.pmus_secret_key)))
    shards = ", ".join(f"shard {s} ${c:.2f}" for s, c in sorted((guard.shard_cash or {}).items()))
    print(f"Kalshi cash: {shards or 'none'}")
    print(f"Polymarket US buying power: ${guard.pm_cash or 0:.2f}")
    print(f"markets held (never traded by the live trader while held): {len(guard.held)}"
          + (f" ({', '.join(sorted(guard.held)[:10])})" if guard.held else ""))
    if guard.attested_until:
        lapses = datetime.fromtimestamp(guard.attested_until).isoformat(timespec="minutes")
        days = (guard.attested_until - time.time()) / 86400
        print(f"Kalshi location check: lapses {lapses} (in {days:.1f} days; trading stops a day before)")
    else:
        print("Kalshi location check: never done")


def halt(cfg, reason: str) -> None:
    """``arbscan live-halt``: stop live trading until ``live-resume``."""
    halt_path, _ = _paths(cfg)
    LiveGuard(cfg, {}, halt_path).halt(reason)
    print(f"stopped: {halt_path} written; the running trader stops within {POLL_S:g} s")


def resume(cfg, checked: bool) -> bool:
    """``arbscan live-resume``: remove the stop file, once any order with no known
    outcome has been checked by hand (``checked``)."""
    halt_path, journal_path = _paths(cfg)
    journal = Journal(journal_path)
    stuck = journal.unresolved()
    if stuck and not checked:
        print("These orders have no known outcome. Check each market's position on the venue, then run "
              "arbscan live-resume --checked:")
        for s in stuck:
            print(f"  {s['venue']} {s['action']} {s['qty']:g} {s['side'].upper()} in {s['market']} "
                  f"(position before it: {s['before']:g})")
        return False
    if stuck:
        journal.clear([s["id"] for s in stuck], "checked by hand")
    try:
        reason = halt_path.read_text().strip()
        halt_path.unlink()
        print(f"resumed; it had stopped for: {reason}")
    except FileNotFoundError:
        print("not stopped")
    return True
