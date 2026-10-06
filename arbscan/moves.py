"""Your own money moves: what changes the venues' balances besides the live trader.

The Trading page compares the live account (both venues' real cash, plus open trades at
cost) with the balances live trading started from (``live_start``). Deposits,
withdrawals, bonuses and your own trades in markets the trader never sent an order to
move those balances too, and would show as trading. ``Moves`` reads them from each
venue's ledger every ``EVERY_S`` and keeps them in ``live_moves``, so the page can leave
them out of the change.

What each ledger entry did to the venue's cash:

- **Kalshi deposits and withdrawals** (``/portfolio/deposits``, ``/withdrawals``), once
  applied. A deposit's fee comes out of it (a card deposit of $102.04 with its 2% fee of
  $2.04 arrives as $100.00); a withdrawal takes its whole amount.
- **Kalshi fills** in a market the trader never traded. A fill says only which side of
  the YES book it met (``book_side``), not whether it opened or closed a position, and
  Kalshi nets YES against NO: closing pays out a contract pair's $1. What a fill moved
  follows from the position it changed, walked back from the position now. (On the
  live trader's own fills this comes within a cent of what its records say.)
- **Kalshi settlements** of such a market: their revenue.
- **Polymarket US activities** (``/v1/portfolio/activities``): a deposit or referral
  bonus counts from when it's made (a card deposit can be spent while it's pending); an
  advance against a pending deposit counts until that deposit completes, which then
  counts whole; a withdrawal counts unless rejected. Trades in a market the trader
  never traded (``isAggressor`` says which of the trade's two executions is the
  account's; prices are YES prices) and their resolutions (the position's
  ``cashValue``). Both match the live trader's records to the cent.

Left in the change, since the account earned them: Kalshi's interest (no API shows
it), fee rebates and liquidity rewards. A Polymarket transfer doesn't say which way it
went: it's logged, not counted.

The ledgers are read on their own HTTP connections, never the order client's (an order
would wait for a fresh TLS handshake if a read held its warm connection), kept alive
between reads (a handshake with Kalshi holds up the event loop ~30 ms; a request on a
warm connection ~5 ms, nearly all of it signing). One request at a time, ``PAUSE_S``
apart, and never while a trade is in flight: Polymarket US counts every request from
one IP, orders included, against one limit of 25 a second, and the scanner's own reads
already go at up to 15. A read is four requests to Kalshi and two to Polymarket US, and
four more to Kalshi for the position when you've traded there yourself.
"""

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from .orders import KalshiTrading, PMTrading

log = logging.getLogger(__name__)

EVERY_S = 60.0  # how often the ledgers are read (under the ledger client's 90 s keep-alive)
OVERLAP_S = 900.0  # each read goes back this far before the last one ended, for entries that show up late
ADVANCE_LINK_S = 3600.0  # a Polymarket advance belongs to a deposit made within this long of it
RECENT = 8  # moves sent to the Trading page
PM_PAGE = 10  # Polymarket activities per page: each is ~10 KB of market details
MAX_PAGES = 300
PAUSE_S = 0.5  # before each ledger request

PM_DEPOSIT = "ACTIVITY_TYPE_ACCOUNT_DEPOSIT"
PM_ADVANCE = "ACTIVITY_TYPE_ACCOUNT_ADVANCED_DEPOSIT"
PM_WITHDRAWAL = "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL"
PM_TRANSFER = "ACTIVITY_TYPE_TRANSFER"
PM_BONUS = "ACTIVITY_TYPE_REFERRAL_BONUS"
PM_TRADE = "ACTIVITY_TYPE_TRADE"
PM_RESOLUTION = "ACTIVITY_TYPE_POSITION_RESOLUTION"
PM_CASH_TYPES = [PM_DEPOSIT, PM_ADVANCE, PM_WITHDRAWAL, PM_TRANSFER, PM_BONUS]
PM_MARKET_TYPES = [PM_TRADE, PM_RESOLUTION]
# Polymarket US intents: (+1 cash in / -1 out, which side's price the YES price stands for).
INTENTS = {"ORDER_INTENT_BUY_LONG": (-1, "yes"), "ORDER_INTENT_SELL_LONG": (1, "yes"),
           "ORDER_INTENT_BUY_SHORT": (-1, "no"), "ORDER_INTENT_SELL_SHORT": (1, "no")}


@dataclass
class Move:
    id: str  # unique per ledger entry, e.g. K:fill:<fill id>
    venue: str
    ts: float
    kind: str  # deposit, advance, withdrawal, bonus, trade, payout
    amount: float  # what it moved the venue's cash by: + in, - out
    market: str | None = None
    note: str | None = None


def _iso(text) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


_warned: set[str] = set()


def _warn(key: str, msg: str, *args) -> None:
    """Log an entry that can't be counted once, not at every read."""
    if key not in _warned:
        _warned.add(key)
        log.warning("own moves: " + msg, *args)


def _value(amount) -> float:
    """A Polymarket ``{"value": "1.23"}`` amount."""
    return float((amount or {}).get("value") or 0)


# --- what each ledger entry moved ---------------------------------------------------------

def kalshi_cash(deposits: list[dict], withdrawals: list[dict], since: float) -> list[Move]:
    """Kalshi deposits (less their fee) and withdrawals, applied at or after ``since``.
    One still pending, or that failed or came back, counts nothing."""
    out = []
    for kind, rows in (("deposit", deposits), ("withdrawal", withdrawals)):
        for r in rows:
            ts = float(r.get("finalized_ts") or r.get("created_ts") or 0)
            if ts < since:
                continue
            if kind == "deposit":
                cents = r["amount_cents"] - (r.get("fee_cents") or 0)
            else:
                cents = -r["amount_cents"]
            out.append(Move(f"K:{kind}:{r['id']}", "K", ts, kind, cents / 100 if r.get("status") == "applied" else 0.0,
                            note=f"{r.get('type') or ''} {r.get('status') or ''}".strip()))
    return out


def kalshi_markets(fills: list[dict], settlements: list[dict], positions: dict[str, float],
                   ours: Callable[[str], bool]) -> list[Move]:
    """Fills and settlements in markets the live trader never traded. ``positions``: the
    contracts held now (+ YES, - NO), read after every fill and settlement given here;
    a fill's cash follows from the position it changed (see the module docstring)."""
    events = defaultdict(list)
    for f in fills:
        if not ours(f["ticker"]):
            events[f["ticker"]].append((float(f["ts"]), 0, f))
    for s in settlements:
        if not ours(s["ticker"]):
            events[s["ticker"]].append((_iso(s.get("settled_time")) or 0.0, 1, s))
    out = []
    for ticker, evs in events.items():
        # Newest first, walking the position back; a settlement comes after any fill of the same second.
        evs.sort(key=lambda e: (e[0], e[1]), reverse=True)
        pos = positions.get(ticker, 0.0)
        for ts, settled, x in evs:
            if settled:
                pos = float(x.get("yes_count_fp") or 0) - float(x.get("no_count_fp") or 0)  # held until it settled
                out.append(Move(f"K:settlement:{ticker}", "K", ts, "payout", (x.get("revenue") or 0) / 100, ticker,
                                f"settled {x.get('market_result') or ''}".strip()))
                continue
            n, yes, fee = float(x["count_fp"]), float(x["yes_price_dollars"]), float(x.get("fee_cost") or 0)
            if x.get("book_side") == "bid":  # bought YES: first closes any NO held, each pair paying $1
                before = pos - n
                closed = min(n, max(0.0, -before))
                cash = closed * (1 - yes) - (n - closed) * yes
                what = f"bought {n:g} YES at {yes:g}"
            elif x.get("book_side") == "ask":  # sold YES: first closes any YES held, else buys NO
                before = pos + n
                closed = min(n, max(0.0, before))
                cash = closed * yes - (n - closed) * (1 - yes)
                what = f"sold {n:g} YES at {yes:g}"
            else:
                _warn(f"K:{x.get('fill_id')}", "Kalshi fill %s in %s has no book side; not counted",
                      x.get("fill_id"), ticker)
                continue
            out.append(Move(f"K:fill:{x['fill_id']}", "K", ts, "trade", cash - fee, ticker, what))
            pos = before
    return out


def pm_cash(activities: list[dict], since: float) -> list[Move]:
    """Polymarket US deposits, advances, withdrawals and referral bonuses made at or
    after ``since`` (see the module docstring)."""
    kinds = {PM_DEPOSIT: "deposit", PM_ADVANCE: "advance", PM_WITHDRAWAL: "withdrawal", PM_BONUS: "bonus"}
    out, deposits, advances = [], [], []
    for a in activities:
        b = a.get("accountBalanceChange")
        kind = kinds.get(a["type"])
        if not b or kind is None:
            if b and a["type"] == PM_TRANSFER:
                _warn(f"P:{b.get('transactionId')}", "Polymarket US transfer %s of $%.2f doesn't say which way it "
                      "went; not counted", b.get("transactionId"), _value(b.get("amount")))
            continue  # else a rebate or a liquidity reward: earned by trading
        status = (b.get("status") or "").removeprefix("ACCOUNT_BALANCE_CHANGE_STATUS_")
        amount = 0.0 if status == "REJECTED" else _value(b.get("amount"))
        m = Move(f"P:{b['transactionId']}", "P", _iso(b.get("createTime")) or _iso(b.get("updateTime")) or 0.0,
                 kind, -amount if kind == "withdrawal" else amount,
                 note=f"{b.get('description') or ''} ({status.lower()})".strip())
        if kind == "deposit":
            deposits.append((m, status))
        else:
            (advances if kind == "advance" else out).append(m)
    # An advance is what a pending deposit lets you spend before it clears: it counts
    # while that deposit is pending. Once the deposit completes it counts whole and the
    # advance nothing; one rejected counts nothing either way.
    linked: set[str] = set()
    for d, status in sorted(deposits, key=lambda x: x[0].ts):
        mine = [x for x in advances if x.id not in linked and abs(x.ts - d.ts) <= ADVANCE_LINK_S]
        linked.update(x.id for x in mine)
        if status == "PENDING" and mine:
            d.amount = 0.0
        else:
            for x in mine:
                x.amount = 0.0
    return [m for m in out + [d for d, _ in deposits] + advances if m.ts >= since]


def pm_markets(activities: list[dict], ours: Callable[[str], bool]) -> list[Move]:
    """Trades and resolutions in markets the live trader never traded."""
    out = []
    for a in activities:
        if a["type"] == PM_TRADE:
            t = a["trade"]
            slug = t.get("marketSlug")
            if ours(slug):
                continue
            e = t.get("aggressorExecution" if t.get("isAggressor") else "passiveExecution") or {}
            sign, side = INTENTS.get((e.get("order") or {}).get("intent"), (None, None))
            if sign is None:
                _warn(f"P:{t.get('id')}", "Polymarket US trade %s in %s: can't tell which way it went; "
                      "not counted", t.get("id"), slug)
                continue
            q, yes, fee = float(e.get("lastShares") or 0), _value(e.get("lastPx")), \
                _value(e.get("commissionNotionalCollected"))
            price = yes if side == "yes" else 1 - yes
            busted = t.get("state") == "TRADE_STATE_BUSTED"  # reversed by the exchange
            out.append(Move(f"P:trade:{t['id']}", "P", _iso(t.get("createTime")) or 0.0, "trade",
                            0.0 if busted else sign * q * price - fee, slug,
                            f"{'bought' if sign < 0 else 'sold'} {q:g} {side.upper()} at {price:g}"
                            + (" (busted)" if busted else "")))
        elif a["type"] == PM_RESOLUTION:
            r = a["positionResolution"]
            slug = r.get("marketSlug")
            if ours(slug):
                continue
            ts = _iso(r.get("updateTime")) or 0.0
            out.append(Move(f"P:resolution:{slug}:{r.get('updateTime')}", "P", ts, "payout",
                            _value((r.get("beforePosition") or {}).get("cashValue")), slug, "resolved"))
    return out


def _activity_ts(a: dict) -> float:
    if a.get("trade"):
        return _iso(a["trade"].get("createTime")) or 0.0
    if a.get("positionResolution"):
        return _iso(a["positionResolution"].get("updateTime")) or 0.0
    return _iso((a.get("accountBalanceChange") or {}).get("createTime")) or 0.0


# --- reading the ledgers --------------------------------------------------------------

async def _kalshi_pages(turn, k: KalshiTrading, path: str, key: str, params: dict | None = None) -> list[dict]:
    out, cursor = [], None
    for _ in range(MAX_PAGES):
        await turn()
        j = await k._get(path, dict(params or {}, limit=200) | ({"cursor": cursor} if cursor else {}))
        out += j.get(key) or []
        cursor = j.get("cursor")
        if not cursor:
            return out
    raise RuntimeError(f"Kalshi {path}: more than {MAX_PAGES} pages")


async def _pm_pages(turn, p: PMTrading, types: list[str], until: float | None = None) -> list[dict]:
    """Newest first, back to ``until`` (or all of them)."""
    out, cursor = [], None
    for _ in range(MAX_PAGES):
        await turn()
        j = await p._get("/v1/portfolio/activities",
                         {"types": types, "limit": PM_PAGE} | ({"cursor": cursor} if cursor else {}))
        page = j.get("activities") or []
        out += page
        cursor = j.get("nextCursor")
        if j.get("eof", True) or not cursor or not page or (until is not None and _activity_ts(page[-1]) < until):
            return out
    raise RuntimeError(f"Polymarket US activities: more than {MAX_PAGES} pages")


class Moves:
    """Your own money moves since live trading started, kept in ``live_moves``.
    ``ours(market)``: the live trader has sent an order in that market. ``idle()``: no
    trade is in flight, so a ledger request may go."""

    def __init__(self, db, out, ours: Callable[[str], bool], idle: Callable[[], bool] = lambda: True):
        self.out, self.ours, self.idle = out, ours, idle
        self.moves: dict[str, Move] = {r[0]: Move(*r) for r in db.execute(
            "SELECT id, venue, ts, kind, amount, market, note FROM live_moves")}
        row = db.execute("SELECT value FROM settings WHERE key = 'live_moves_read'").fetchone()
        self.read_to: dict[str, float] = json.loads(row[0]) if row else {}  # venue -> ledger read up to here
        self.read_ts: float | None = None  # the last read that went through, both venues
        self.errors: dict[str, str] = {}

    def total(self, since: float) -> dict[str, float]:
        out = {"K": 0.0, "P": 0.0}
        for m in self.moves.values():
            if m.ts >= since:
                out[m.venue] += m.amount
        return out

    def snapshot(self, since: float | None) -> dict | None:
        if since is None:
            return None
        recent = sorted((m for m in self.moves.values() if m.ts >= since and abs(m.amount) >= 0.005),
                        key=lambda m: m.ts, reverse=True)[:RECENT]
        return {**self.total(since), "recent": [asdict(m) for m in recent], "read": self.read_ts,
                "errors": dict(self.errors) or None}

    async def refresh(self, kalshi: KalshiTrading, pm: PMTrading, since: float) -> None:
        """Read both venues' ledgers since the last read (each on its own: one venue
        down doesn't hold up the other)."""
        ok, was = True, dict(self.read_to)
        for venue, read in (("K", self._kalshi), ("P", self._pm)):
            began = time.time()
            start = max(since, self.read_to.get(venue, since) - OVERLAP_S)
            try:
                done = await read(kalshi if venue == "K" else pm, since, start)
            except Exception as e:
                ok, error = False, f"{type(e).__name__}: {e}"
                if self.errors.get(venue) != error:  # once, not at every read
                    log.warning("own moves: reading %s's ledger failed: %s", venue, error)
                self.errors[venue] = error
                continue
            self.errors.pop(venue, None)
            if done:  # else read again from the same place next time
                self.read_to[venue] = began
            ok = ok and done
        if self.read_to != was:
            self.out.execute("INSERT OR REPLACE INTO settings VALUES ('live_moves_read', ?)",
                             (json.dumps(self.read_to),))
            self.out.commit()
        if ok:
            self.read_ts = time.time()

    async def _turn(self) -> None:
        """Wait for the next ledger request's turn: ``PAUSE_S`` after the last, with no trade in flight."""
        await asyncio.sleep(PAUSE_S)
        while not self.idle():
            await asyncio.sleep(PAUSE_S)

    async def _kalshi(self, k: KalshiTrading, since: float, start: float) -> bool:
        turn = self._turn
        cash = kalshi_cash(await _kalshi_pages(turn, k, "/portfolio/deposits", "deposits"),
                           await _kalshi_pages(turn, k, "/portfolio/withdrawals", "withdrawals"), since)
        fills, settled = await self._kalshi_window(k, start)
        after, moving = {}, set()
        if any(not self.ours(x["ticker"]) for x in fills + settled):
            # Yours, walked back from the position now: read the ledger again between two
            # reads of the position. A position of yours that changed meanwhile may have a
            # fill or settlement that read missed: that market waits for the next read.
            await turn()
            before = await k.positions()
            fills, settled = await self._kalshi_window(k, start)
            await turn()
            after = await k.positions()
            moving = {t for t in set(before) | set(after)
                      if abs(before.get(t, 0.0) - after.get(t, 0.0)) > 1e-9 and not self.ours(t)}
        fills = [f for f in fills if f["ticker"] not in moving and float(f["ts"]) >= since]
        settled = [s for s in settled if s["ticker"] not in moving and (_iso(s.get("settled_time")) or 0) >= since]
        self._keep(cash + kalshi_markets(fills, settled, after, self.ours))
        return not moving

    async def _kalshi_window(self, k: KalshiTrading, start: float) -> tuple[list[dict], list[dict]]:
        """Fills and settlements since ``start``."""
        return (await _kalshi_pages(self._turn, k, "/portfolio/fills", "fills", {"min_ts": int(start)}),
                await _kalshi_pages(self._turn, k, "/portfolio/settlements", "settlements", {"min_ts": int(start)}))

    async def _pm(self, p: PMTrading, since: float, start: float) -> bool:
        cash = pm_cash(await _pm_pages(self._turn, p, PM_CASH_TYPES), since)
        markets = [a for a in await _pm_pages(self._turn, p, PM_MARKET_TYPES, until=start) if _activity_ts(a) >= since]
        self._keep(cash + pm_markets(markets, self.ours))
        return True

    def _keep(self, moves: list[Move]) -> None:
        """Store what's new or changed (a deposit that cleared, a fill read again)."""
        changed = False
        for m in moves:
            m.amount = round(m.amount, 6)
            if self.moves.get(m.id) == m:
                continue
            if m.id not in self.moves and abs(m.amount) >= 0.005:
                log.info("own moves: %s %s %+.2f%s", m.venue, m.kind, m.amount, f" in {m.market}" if m.market else "")
            self.moves[m.id] = m
            self.out.execute("INSERT OR REPLACE INTO live_moves (id, venue, ts, kind, amount, market, note) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (m.id, m.venue, m.ts, m.kind, m.amount, m.market, m.note))
            changed = True
        if changed:
            self.out.commit()
