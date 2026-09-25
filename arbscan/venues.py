"""Thin clients for the public (unauthenticated) market-data endpoints we use."""

import json
from collections.abc import AsyncIterator
from typing import Any

from .http import Api


def chunks(items: list[str], n: int) -> list[list[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


class Kalshi:
    ORDERBOOK_BATCH = 100  # documented max for GET /markets/orderbooks
    MARKETS_BATCH = 100

    def __init__(self, api: Api):
        self.api = api

    async def iter_open_events(self) -> AsyncIterator[list[dict[str, Any]]]:
        """Pages of open events with nested markets (multivariate combos excluded)."""
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 200, "status": "open", "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            d = await self.api.get("/events", params)
            events = d.get("events") or []
            if events:
                yield events
            cursor = d.get("cursor")
            if not cursor or not events:
                return

    async def created_markets(self, since_ts: int) -> list[dict[str, Any]]:
        """Markets created at or after ``since_ts`` (multivariate combos excluded)."""
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"min_created_ts": since_ts, "mve_filter": "exclude", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = await self.api.get("/markets", params)
            ms = d.get("markets") or []
            out.extend(ms)
            cursor = d.get("cursor")
            if not cursor or not ms:
                return out

    async def all_series(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = await self.api.get("/series", params)
            for s in d.get("series") or []:
                out[s["ticker"]] = s
            cursor = d.get("cursor")
            if not cursor:
                return out

    async def orderbooks(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for batch in chunks(tickers, self.ORDERBOOK_BATCH):
            d = await self.api.get("/markets/orderbooks", [("tickers", t) for t in batch])
            for ob in d.get("orderbooks") or []:
                out[ob["ticker"]] = ob.get("orderbook_fp") or {}
        return out

    async def markets(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for batch in chunks(tickers, self.MARKETS_BATCH):
            d = await self.api.get("/markets", {"tickers": ",".join(batch), "limit": len(batch)})
            for m in d.get("markets") or []:
                out[m["ticker"]] = m
        return out

    async def event(self, event_ticker: str) -> dict[str, Any]:
        return (await self.api.get(f"/events/{event_ticker}"))["event"]

    async def series(self, series_ticker: str) -> dict[str, Any]:
        return (await self.api.get(f"/series/{series_ticker}"))["series"]


class PolymarketUS:
    PAGE = 500
    SLUG_BATCH = 100

    def __init__(self, api: Api):
        self.api = api

    async def iter_open_markets(self) -> AsyncIterator[list[dict[str, Any]]]:
        offset = 0
        while True:
            params = {"limit": self.PAGE, "offset": offset, "active": "true", "closed": "false"}
            ms = (await self.api.get("/v1/markets", params)).get("markets") or []
            if ms:
                yield ms
            if len(ms) < self.PAGE:
                return
            offset += self.PAGE

    async def listed_since(self, start_min: str) -> list[dict[str, Any]]:
        """Open markets whose ``startDate`` (their listing time) is at or after
        ``start_min`` (ISO 8601). ``orderBy`` is ignored by the API; this filter isn't."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            params = {"limit": self.PAGE, "offset": offset, "active": "true", "closed": "false",
                      "startDateMin": start_min}
            ms = (await self.api.get("/v1/markets", params)).get("markets") or []
            out.extend(ms)
            if len(ms) < self.PAGE:
                return out
            offset += self.PAGE

    async def markets(self, slugs: list[str]) -> dict[str, dict[str, Any]]:
        """Market metadata plus best bid/ask for many slugs in one request per 100."""
        out = {}
        for batch in chunks(slugs, self.SLUG_BATCH):
            params = [("limit", str(len(batch)))] + [("slug", s) for s in batch]
            for m in (await self.api.get("/v1/markets", params)).get("markets") or []:
                out[m["slug"]] = m
        return out

    async def book(self, slug: str) -> dict[str, Any]:
        return (await self.api.get(f"/v1/markets/{slug}/book"))["marketData"]


def pm_quote(m: dict[str, Any], key: str) -> float | None:
    """Parse ``bestBidQuote`` / ``bestAskQuote`` from a Polymarket US market."""
    q = m.get(key) or {}
    v = q.get("value")
    return float(v) if v not in (None, "") else None


def pm_is_open(m: dict[str, Any]) -> bool:
    return m.get("status") == "MARKET_STATUS_OPEN" and not m.get("closed")


def _side_label(side: dict[str, Any]) -> str:
    label = (side.get("description") or "").strip()
    if label == "No":
        return label  # "not X" is not about team X; don't label it with X's name
    team = side.get("team") or {}
    for key in ("name", "safeName"):  # e.g. "Dukes" / "James Madison"
        v = (team.get(key) or "").strip()
        if v and v.lower() not in label.lower():
            label = f"{label} · {v}" if label else v
    abbr = (team.get("abbreviation") or "").strip()
    if abbr:
        label = f"{label} ({abbr})"
    return label


def pm_sides(m: dict[str, Any]) -> tuple[str, str]:
    """(yes_label, no_label). YES is the instrument's long side (buying it); NO is the
    short side. Labels carry the team name/abbreviation when the venue provides one,
    e.g. ("-2.50 · Cincinnati Reds (cin)", "+2.50 · Atlanta Braves (atl)"). A plain
    "Yes" is extended with the market's subject, e.g. "Yes · Arch Manning"."""
    sides = m.get("marketSides") or []
    long_side = next((s for s in sides if s.get("long")), None)
    short_side = next((s for s in sides if s.get("long") is False), None)
    if long_side:
        yes = _side_label(long_side)
        no = _side_label(short_side) if short_side else "No"
        if yes.startswith("Yes"):
            subject = _subject(m)
            if subject and subject.lower() not in yes.lower():
                yes = f"{yes} · {subject}"
        return yes, no
    try:
        outcomes = json.loads(m.get("outcomes") or "[]")
    except ValueError:
        outcomes = []
    yes = outcomes[0] if outcomes else "Yes"
    return yes, (outcomes[1] if len(outcomes) == 2 else "No")


def _subject(m: dict[str, Any]) -> str:
    """What a plain Yes/No market is about: its short title ("Arch Manning", "BYU"),
    else the slug's trailing code ("...-i5-sd" -> "sd", "...-draw" -> "draw")."""
    title = (m.get("title") or "").strip()
    if title and title != (m.get("question") or "").strip() and len(title.split()) <= 8:
        return title
    last = m.get("slug", "").rsplit("-", 1)[-1]
    return last if last.isalpha() and 2 <= len(last) <= 6 else ""
