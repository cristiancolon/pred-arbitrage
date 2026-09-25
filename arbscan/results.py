"""How each market we've paired actually settled, for judging trades and backtests.

An arb only pays if both markets settle as one bet: for the direction bought, the
two legs' payouts must add up to $1. The ``results`` table keeps, per venue and
market, what one YES contract paid (``yes_value``, $0-$1) once the venue publishes
it, with the venue's own status, result and timestamps. The views below join it to
the recorded windows and to the approved pairs, so a backtest can tell which
windows would really have paid.

Where results come from:

- Kalshi's lifecycle feed announces ``determined`` and ``settled`` with the result
  (recorded the moment it arrives, streaming mode only);
- ``ResultsRecorder`` reads both venues' REST APIs for every market that has ever
  been paired, had a profitable window or a paper trade: each market once, then
  every ``RECHECK_CLOSED_S`` once it has closed (or the scanner saw it finish), and
  every ``RECHECK_OPEN_S`` before that, until a result is in; then once a day for
  three days in case a venue corrects it. Polymarket US doesn't
  publish when it resolved a market, so ``first_final_ts`` (when we first saw the
  result) stands in for it.
"""

import asyncio
import json
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

KALSHI_FINAL = {"determined", "settled", "finalized"}
PM_FINAL = "MARKET_STATUS_RESOLVED"
RECHECK_CLOSED_S = 600.0
RECHECK_OPEN_S = 6 * 3600.0
# Venues occasionally correct a result: re-read results once a day for this long.
RECHECK_FINAL_S = 86400.0
FINAL_WATCH_S = 3 * 86400.0
EVERY_S = 60.0
BATCH = 500  # markets per recorder pass (several REST requests)

UPSERT = (
    "INSERT INTO results (venue, id, status, yes_value, result, closed_ts, settled_ts, first_final_ts, checked_ts, "
    "raw) VALUES (?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT (venue, id) DO UPDATE SET status = excluded.status, "
    "yes_value = COALESCE(excluded.yes_value, results.yes_value), result = COALESCE(excluded.result, results.result), "
    "closed_ts = COALESCE(excluded.closed_ts, results.closed_ts), "
    "settled_ts = COALESCE(excluded.settled_ts, results.settled_ts), "
    "first_final_ts = COALESCE(results.first_final_ts, excluded.first_final_ts), "
    "checked_ts = excluded.checked_ts, raw = excluded.raw"
)


def _ts(v) -> float | None:
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _row(venue, mid, status, yes, result, closed, settled, now, raw) -> tuple:
    return (venue, mid, status, yes, result, closed, settled, now if yes is not None else None, now,
            json.dumps(raw, separators=(",", ":")))


def kalshi_yes_value(status: str | None, result: str | None, value) -> float | None:
    """What one YES contract paid, once Kalshi has a result."""
    if status not in KALSHI_FINAL:
        return None
    if value not in (None, ""):
        return float(value)
    return {"yes": 1.0, "no": 0.0}.get(result or "")


def kalshi_row(m: dict, now: float) -> tuple:
    status = m.get("status")
    yes = kalshi_yes_value(status, m.get("result"), m.get("settlement_value_dollars"))
    raw = {k: m.get(k) for k in ("status", "result", "settlement_value_dollars", "close_time",
                                 "expected_expiration_time", "settlement_ts", "expiration_value") if m.get(k)}
    return _row("K", m["ticker"], status, yes, m.get("result") or None, _ts(m.get("close_time")),
                _ts(m.get("settlement_ts")), now, raw)


def kalshi_lifecycle_row(msg: dict, now: float) -> tuple | None:
    """From a ``market_lifecycle_v2`` determined/settled event."""
    kind = msg.get("event_type")
    if kind not in ("determined", "settled") or not msg.get("market_ticker"):
        return None
    yes = kalshi_yes_value(kind, msg.get("result"), msg.get("settlement_value"))
    raw = {k: msg.get(k) for k in ("event_type", "result", "settlement_value", "determination_ts", "settled_ts",
                                   "close_ts") if msg.get(k) is not None}
    return _row("K", msg["market_ticker"], kind, yes, msg.get("result") or None, _ts(msg.get("close_ts")),
                _ts(msg.get("settled_ts") or msg.get("determination_ts")), now, raw)


def pm_yes_value(status: str | None, outcome_prices: str | None) -> float | None:
    """What one YES (long) contract paid, once Polymarket US has resolved the market."""
    if status != PM_FINAL:
        return None
    try:
        return float(json.loads(outcome_prices or "[]")[0])
    except (ValueError, IndexError, TypeError):
        return None


def pm_row(m: dict, now: float) -> tuple:
    status = m.get("status")
    yes = pm_yes_value(status, m.get("outcomePrices"))
    result = None
    if yes is not None:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            result = outcomes[0] if yes >= 0.5 else outcomes[1]
        except (ValueError, IndexError, TypeError):
            pass
    raw = {k: m.get(k) for k in ("status", "closed", "outcomes", "outcomePrices", "endDate", "ep3Status")
           if m.get(k) is not None}
    return _row("P", m["slug"], status, yes, result, _ts(m.get("endDate")), None, now, raw)


def tracked_markets(db, extra_pairs=()) -> set[tuple[str, str]]:
    """Every (venue, id) that has been paired, had a window or a paper trade."""
    pairs = {r[0] for r in db.execute(
        "SELECT pair FROM episodes UNION SELECT pair FROM paper_trades "
        "UNION SELECT kalshi || '|' || pm FROM decisions WHERE decision IN ('same', 'inverse')")}
    pairs |= set(extra_pairs)
    out = set()
    for p in pairs:
        k, _, pm = p.partition("|")
        if k and pm:
            out |= {("K", k), ("P", pm)}
    return out


def due(db, tracked: set[tuple[str, str]], finished: set[tuple[str, str]], now: float) -> list[tuple[str, str]]:
    """Markets to look up now, most overdue first."""
    known = {(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in db.execute(
        "SELECT venue, id, yes_value, closed_ts, checked_ts, first_final_ts FROM results")}
    out = []
    for key in tracked:
        yes, closed, checked, final = known.get(key, (None, None, None, None))
        if yes is not None:
            if final is not None and now - final < FINAL_WATCH_S and now - checked >= RECHECK_FINAL_S:
                out.append((checked, key))
            continue
        if checked is None:
            out.append((0.0, key))
            continue
        closed_now = key in finished or (closed is not None and closed <= now)
        wait = RECHECK_CLOSED_S if closed_now else RECHECK_OPEN_S
        if now - checked >= wait:
            out.append((checked, key))
    return [k for _, k in sorted(out)]


class ResultsRecorder:
    def __init__(self, read, write, kalshi, pm, extra_pairs=lambda: (), finished=lambda: ()):
        """``read(fn)`` / ``write(fn)`` run ``fn(db)`` on a short-lived connection
        (web.app.Service.read/write); ``finished()`` gives pair ids the scanner
        saw finish."""
        self.read, self.write = read, write
        self.kalshi, self.pm = kalshi, pm
        self.extra_pairs, self.finished = extra_pairs, finished
        self.recorded = 0

    async def step(self) -> int:
        now = time.time()
        extra = list(self.extra_pairs())
        fin = set()
        for p in self.finished():
            k, _, pm = p.partition("|")
            fin |= {("K", k), ("P", pm)}
        todo = await self.read(lambda db: due(db, tracked_markets(db, extra), fin, now))
        # Half of each pass per venue (the rest to whichever has more waiting).
        k = [x for x in todo if x[0] == "K"]
        pm = [x for x in todo if x[0] == "P"]
        nk = min(len(k), max(BATCH // 2, BATCH - len(pm)))
        todo = k[:nk] + pm[:BATCH - nk]
        if not todo:
            return 0
        tickers = [i for v, i in todo if v == "K"]
        slugs = [i for v, i in todo if v == "P"]
        km = await self.kalshi.markets(tickers) if tickers else {}
        pmk = await self.pm.markets(slugs) if slugs else {}
        rows = [kalshi_row(m, now) for m in km.values()] + [pm_row(m, now) for m in pmk.values()]
        # Markets the venue didn't return still count as checked, so they wait their turn.
        seen = {(r[0], r[1]) for r in rows}
        rows += [_row(v, i, None, None, None, None, None, now, {"missing": True}) for v, i in todo if (v, i) not in seen]

        def save(db):
            db.executemany(UPSERT, rows)
            db.commit()

        await self.write(save)
        final = sum(1 for r in rows if r[3] is not None)
        self.recorded += final
        if final:
            log.info("recorded %d settlement results (%d markets checked)", final, len(rows))
        return final

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.step()
            except Exception as e:
                log.warning("settlement check failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=EVERY_S)
            except asyncio.TimeoutError:
                pass


def lookup(db, keys) -> dict[tuple[str, str], float]:
    """yes_value for each (venue, id) that has a result."""
    out = {}
    for venue, mid in keys:
        r = db.execute("SELECT yes_value FROM results WHERE venue = ? AND id = ? AND yes_value IS NOT NULL",
                       (venue, mid)).fetchone()
        if r is not None:
            out[(venue, mid)] = r[0]
    return out
