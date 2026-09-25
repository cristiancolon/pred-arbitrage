"""Download both venues' open markets into the ``markets`` table for matching and review.

Pages are processed one at a time so memory stays flat (~100 MB peak, mostly the
Kalshi series list) even though the raw listings are several hundred MB.
"""

import logging
import re
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .fees import kalshi_taker_coef
from .http import Api, make_client
from .scanner import PMUS_DEFAULT_COEF, parse_ts
from .venues import Kalshi, PolymarketUS, pm_is_open, pm_quote, pm_sides

log = logging.getLogger(__name__)

ROW_SQL = "INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _join(*parts: str | None) -> str:
    out: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in out:
            out.append(p)
    return " | ".join(out)


_ET = ZoneInfo("America/New_York")
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
# Game event tickers embed the scheduled start in ET, e.g. KXMLBGAME-26SEP241915CINATL.
_TICKER_START = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})[A-Z]")


def kalshi_start_ts(event_ticker: str | None) -> float | None:
    m = _TICKER_START.search(event_ticker or "")
    if not m:
        return None
    yy, mon, dd, hhmm = m.groups()
    try:
        dt = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hhmm[:2]), int(hhmm[2:]), tzinfo=_ET)
    except (KeyError, ValueError):
        return None
    return dt.timestamp()


def kalshi_row(event: dict[str, Any], m: dict[str, Any], fee_coef: float, now: float) -> tuple:
    rules = "\n\n".join(r for r in (m.get("rules_primary"), m.get("rules_secondary")) if r)
    close = parse_ts(m.get("expected_expiration_time")) or parse_ts(m.get("close_time"))
    # occurrence_datetime is roughly when a game is expected to *end*; prefer the
    # start time encoded in the ticker when there is one.
    start = kalshi_start_ts(event.get("event_ticker")) or parse_ts(m.get("occurrence_datetime"))
    return (
        "K", m["ticker"], event.get("event_ticker"), event.get("series_ticker"), event.get("category"),
        _join(event.get("title"), event.get("sub_title"), m.get("title")),
        m.get("yes_sub_title") or "", m.get("no_sub_title") or "", None,
        start, close, rules, fee_coef,
        _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")), _f(m.get("volume_24h_fp")), now,
    )


def pm_row(m: dict[str, Any], now: float) -> tuple:
    yes, no = pm_sides(m)
    kind = m.get("marketType")
    # e.g. "totals/football_team_points_full_game_total": the structured type is often
    # more precise than the question text.
    market_type = "/".join(x for x in (kind, m.get("sportsMarketType")) if x) or None
    # gameStartTime is only meaningful for single-game markets, not season futures.
    start = parse_ts(m.get("gameStartTime")) if kind not in (None, "futures", "election") else None
    return (
        "P", m["slug"], None, m["slug"].split("-", 1)[0], m.get("category"),
        _join(m.get("question"), m.get("title"), m.get("subtitle")),
        yes, no, market_type,
        start, parse_ts(m.get("endDate")), m.get("description") or "",
        _f(m.get("feeCoefficient")) or PMUS_DEFAULT_COEF,
        pm_quote(m, "bestBidQuote"), pm_quote(m, "bestAskQuote"), None, now,
    )


async def build(cfg: Config, db: sqlite3.Connection) -> None:
    horizon = time.time() + cfg.catalog_horizon_days * 86400
    async with make_client() as client:
        kalshi = Kalshi(Api(client, cfg.kalshi_base, cfg.kalshi_rps, "kalshi"))
        pm = PolymarketUS(Api(client, cfg.pmus_base, cfg.pmus_rps, "pmus"))

        now = time.time()
        log.info("fetching Kalshi series fee schedule")
        fees = {t: kalshi_taker_coef(s.get("fee_type"), s.get("fee_multiplier"))
                for t, s in (await kalshi.all_series()).items()}
        log.info("fetching Kalshi open events (this takes a minute or two)")
        n = pages = 0
        async for events in kalshi.iter_open_events():
            rows = []
            for e in events:
                coef = fees.get(e.get("series_ticker"), kalshi_taker_coef(None, None))
                for m in e.get("markets") or []:
                    if m.get("status") != "active":
                        continue
                    row = kalshi_row(e, m, coef, now)
                    if row[10] is None or row[10] <= horizon:
                        rows.append(row)
            db.executemany(ROW_SQL, rows)
            db.commit()
            n += len(rows)
            pages += 1
            if pages % 10 == 0:
                log.info("Kalshi: %d markets so far", n)
        db.execute("DELETE FROM markets WHERE venue = 'K' AND updated < ?", (now,))
        db.commit()
        log.info("Kalshi: %d active markets resolving within %d days", n, cfg.catalog_horizon_days)

        now = time.time()
        log.info("fetching Polymarket US open markets")
        n = pages = 0
        async for ms in pm.iter_open_markets():
            rows = [pm_row(m, now) for m in ms if pm_is_open(m)]
            rows = [r for r in rows if r[10] is None or r[10] <= horizon]
            db.executemany(ROW_SQL, rows)
            db.commit()
            n += len(rows)
            pages += 1
            if pages % 20 == 0:
                log.info("Polymarket US: %d markets so far", n)
        db.execute("DELETE FROM markets WHERE venue = 'P' AND updated < ?", (now,))
        db.commit()
        log.info("Polymarket US: %d open markets resolving within %d days", n, cfg.catalog_horizon_days)
