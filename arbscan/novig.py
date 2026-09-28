"""Novig, a sports-only exchange: its public catalog and order books, and how its
markets map onto arbscan's YES/NO model.

Novig lists events (games) holding markets, and each market has two outcomes with
their own order books. A resting bid for one outcome at ``p`` is an offer of the
other outcome at ``1 - p``. Quantities count 1-cent contracts, so 100 of them make
one of the $1 contracts Kalshi and Polymarket US trade.

YES is the outcome the market is about: "Over" for an over/under, "Yes" for a yes/no
prop, else the team or player named in the market's description ("DET -7.5" is about
DET, "OAK" is Oakland's moneyline).

Fees: game markets charge takers ``0.03 * p * (1 - p)`` only while the event is live,
so a fill before the game starts is free; season futures charge ``0.06`` always.
Each market carries its own schedule in ``fee``.

Keys (https://docs.novig.com/api/api-keys): the account's ``management`` key, made in
the web app, opens subaccounts and issues their keys. arbscan streams books with a
``trading::read`` key, which can't place orders or move money; ``issue_read_key``
opens a subaccount for it (the subaccount's own ``trading`` key is saved beside the
management key, never on the scanning machine).
"""

import json
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from .auth import NovigSigner
from .http import Api, ApiError

CONTRACT = 100  # Novig contracts (1 cent each) per $1 contract
PAGE = 500

# Market types whose outcomes don't line up with a two-way bet elsewhere: a 1st-half
# moneyline can end tied (Kalshi's then pays neither team), and scorer/method props
# are one of many outcomes.
SKIP_TYPES = frozenset({"MONEY_1H", "FIRST_TOUCHDOWN_SCORER", "FIRST_GOAL_SCORER", "DRAW"})

# Words the matcher's hard filters look for (bet type, game segment, stat), in the
# way Kalshi and Polymarket US phrase them.
TYPE_TEXT = {
    "MONEY": "winner",
    "SPREAD": "spread",
    "SPREAD_1H": "1st half spread",
    "TOTAL": "total",
    "TOTAL_1H": "1st half total",
    "TEAM_TOTAL": "team total",
    "SET_SPREAD": "set spread",
    "TOTAL_SETS": "total sets",
    "FIRST_SET_MONEYLINE": "1st set winner",
    "FIRST_INNING_TOTAL": "1st inning total",
    "MONEYLINE_3_WAY_WIN": "winner",
    "MONEYLINE_3_WAY_DRAW": "draw",
    "RECEIVING_YARDS": "receiving yards",
    "RUSHING_YARDS": "rushing yards",
    "PASSING_YARDS": "passing yards",
    "RUSHING_AND_RECEIVING_YARDS": "rushing + receiving yards",
    "PASSING_AND_RUSHING_YARDS": "passing + rushing yards",
    "RECEPTIONS": "receptions",
    "TOUCHDOWNS": "touchdowns",
    "PASSING_TOUCHDOWNS": "passing touchdowns",
    "PASSING_ATTEMPTS": "passing attempts",
    "PASSING_COMPLETIONS": "completions",
    "RUSHING_ATTEMPTS": "rushing attempts",
    "INTERCEPTIONS_THROWN": "interceptions",
    "LONGEST_RECEPTION": "longest reception",
    "LONGEST_RUSH": "longest rush",
    "LONGEST_COMPLETION": "longest completion",
    "KICKING_POINTS": "kicking points",
    "FIELD_GOALS_MADE": "field goals made",
    "HITS": "hits",
    "HOME_RUNS": "home runs",
    "RUNS": "runs scored",
    "RBIS": "rbis",
    "HITS_RUNS_RBIS": "hits + runs + rbis",
    "TOTAL_BASES": "total bases",
    "STRIKEOUTS": "strikeouts",
    "POINTS": "points",
    "REBOUNDS": "rebounds",
    "ASSISTS": "assists",
    "THREE_POINTERS_MADE": "threes",
    "PLAYER_GAMES_WON": "games won",
    "PLAYER_GOALS": "goals",
    "SHOTS_ON_TARGET": "shots on target",
    "SAVES": "saves",
}

# Tennis spreads and totals count games; its set markets say so.
TENNIS = frozenset({"ATP", "WTA"})
TENNIS_TEXT = {"SPREAD": "games spread", "TOTAL": "total games"}

_ET = ZoneInfo("America/New_York")


class Novig:
    """Novig's unauthenticated catalog and books (``/v3/public``), throttled per IP."""

    def __init__(self, api: Api):
        self.api = api

    async def _pages(self, path: str, params: dict | None = None) -> AsyncIterator[list[dict[str, Any]]]:
        after = None
        while True:
            q = {"limit": PAGE, **(params or {})}
            if after:
                q["after"] = after
            d = await self.api.get(path, q)
            items = d.get("items") or []
            if items:
                yield items
            after = d.get("next")
            if not after or not items:
                return

    async def events(self) -> list[dict[str, Any]]:
        return [e async for page in self._pages("/v3/public/catalog/events") for e in page]

    async def iter_markets(self) -> AsyncIterator[list[dict[str, Any]]]:
        async for page in self._pages("/v3/public/catalog/markets"):
            yield page

    async def book(self, market_id: str) -> dict[str, Any]:
        return await self.api.get(f"/v3/public/catalog/markets/{market_id}/book")


class NovigAccount:
    """Signed calls on a Novig account. No retries: opening a subaccount or issuing a
    key isn't idempotent, so a retried request could do it twice."""

    def __init__(self, client: httpx.AsyncClient, base: str, signer: NovigSigner):
        self.client, self.base, self.signer = client, base.rstrip("/"), signer

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = self.signer.headers(method, path, data)  # signs these exact bytes
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = await self.client.request(method, self.base + path, content=data, headers=headers)
        if r.status_code >= 400:
            raise ApiError(f"novig {method} {path}: HTTP {r.status_code}: {r.text[:300]}", r.status_code)
        return r.json() if r.content else None


def key_algorithm(public_pem: str) -> str:
    key = serialization.load_pem_public_key(public_pem.encode())
    if isinstance(key, ed25519.Ed25519PublicKey):
        return "Ed25519"
    if isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
        return "P-256"
    raise ValueError("Novig keys are Ed25519 or P-256")


def new_keypair() -> tuple[bytes, str]:
    """(private PKCS#8 PEM, public SPKI PEM) of a fresh Ed25519 key."""
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public.decode()


async def issue_read_key(account: NovigAccount, read_public_pem: str, label: str, trading_key_out: Path,
                         log=print) -> dict[str, str]:
    """Issue a ``trading::read`` key for ``read_public_pem`` on the subaccount labelled
    ``label``, opening it first if there's none. Opening one creates its ``trading``
    key; its private half goes to ``trading_key_out`` (never overwritten) before the
    call, so a success can't lose it."""
    echo = await account.call("POST", "/v3/echo", {"arbscan": "signature check"})
    if echo != {"arbscan": "signature check"}:
        raise ApiError(f"novig echo returned {echo!r}")
    log("management key signs correctly")
    sub = next((s for s in await account.call("GET", "/v3/account/subaccounts") if s.get("label") == label), None)
    if sub is None:
        private, public = new_keypair()
        fd = os.open(trading_key_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(private)
        sub = await account.call("POST", "/v3/account/subaccounts",
                                 {"label": label, "publicKey": public, "algorithm": "Ed25519"})
        log(f"opened subaccount {label!r} ({sub['keyId']}); its trading key is in {trading_key_out}")
    else:
        log(f"using subaccount {label!r} ({sub['keyId']})")
    made = await account.call("POST", f"/v3/account/subaccounts/{sub['keyId']}/keys", {
        "name": f"{label}-read", "publicKey": read_public_pem, "algorithm": key_algorithm(read_public_pem),
        "scope": "trading::read"})
    return {"subaccount": sub["keyId"], "key_id": made["keyId"], "fingerprint": made["fingerprint"]}


def teams(description: str) -> tuple[str, str] | None:
    """(away, home) from an event description like "New York Jets @ Detroit Lions"
    (a tennis round name after the second player is dropped)."""
    if " @ " not in description:
        return None
    away, home = description.split(" @ ", 1)
    home = re.sub(r"\s+(Round of \d+|Quarterfinals|Semifinals|Finals?|Qualifying.*|R\d+)$", "", home.strip())
    return away.strip(), home


def _initials_score(code: str, name: str) -> int:
    """How well a short code ("NYJ", "OAK") abbreviates a name: 2 if the name starts
    with it or it spells the name's initials, 1 if its letters appear in order, else 0."""
    code, low = code.lower(), name.lower()
    if low.startswith(code) or "".join(w[0] for w in low.split()).startswith(code):
        return 2
    it = iter(low)
    return 1 if low[:1] == code[:1] and all(ch in it for ch in code) else 0


def name_for(code: str, names: tuple[str, str] | None) -> str | None:
    """Which of the event's two names a team code stands for, if exactly one fits best."""
    if not names:
        return None
    scores = [_initials_score(code, n) for n in names]
    best = max(scores)
    return names[scores.index(best)] if best and scores.count(best) == 1 else None


def orient(market: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(YES outcome, NO outcome), or None if the market isn't a clean two-way bet."""
    outs = market.get("outcomes") or []
    if len(outs) != 2:
        return None
    a, b = outs
    na, nb = a["name"].strip(), b["name"].strip()
    for yes_word in ("Yes", "Over"):
        if na.startswith(yes_word) and not nb.startswith(yes_word):
            return a, b
        if nb.startswith(yes_word) and not na.startswith(yes_word):
            return b, a
    subject = (market.get("description") or "").split()[:1]
    if subject:
        s = subject[0]
        if na.split()[:1] == [s] and nb.split()[:1] != [s]:
            return a, b
        if nb.split()[:1] == [s] and na.split()[:1] != [s]:
            return b, a
    # Tennis outcomes abbreviate first names ("H. Gaston"); the description does too.
    desc = market.get("description") or ""
    if desc.startswith(na) and not desc.startswith(nb):
        return a, b
    if desc.startswith(nb) and not desc.startswith(na):
        return b, a
    return None


def _label(outcome_name: str, names: tuple[str, str] | None, subject: str = "") -> str:
    """An outcome's name with its team or player spelled out, e.g. "DET -7.5 · Detroit
    Lions (det)", "H. Gaston · Hugo Gaston", or "Over 43.5 · Adonai Mitchell"."""
    first = outcome_name.split()[0] if outcome_name.split() else ""
    if first.isupper() and 2 <= len(first) <= 5 and first.isalpha():
        full = name_for(first, names)
        if full:
            return f"{outcome_name} · {full} ({first.lower()})"
        return f"{outcome_name} ({first.lower()})"
    m = re.match(r"([A-Z])\. (.+?)(?: [+-]\d.*)?$", outcome_name)  # "H. Gaston", "J. Grabher +3.5"
    if m and names:
        full = [n for n in names if n.endswith(" " + m.group(2)) and n.startswith(m.group(1))]
        if len(full) == 1:
            return f"{outcome_name} · {full[0]}"
    if subject and outcome_name.split()[:1] in (["Over"], ["Under"], ["Yes"], ["No"]):
        return f"{outcome_name} · {subject}"
    return outcome_name


def subject_of(market: dict[str, Any]) -> str:
    """The player or team a prop or team total is about ("Adonai Mitchell 43.5
    RECEIVING_YARDS" -> "Adonai Mitchell"), else ""."""
    kind, desc = market.get("marketType") or "", market.get("description") or ""
    if not kind or not desc.endswith(kind):
        return ""
    subject = desc[: -len(kind)].strip()
    strike = str(market.get("strike") or "")
    if strike and subject.endswith(strike):
        subject = subject[: -len(strike)].strip()
    return subject


def title(event: dict[str, Any], market: dict[str, Any]) -> str:
    """A readable title in the words the matcher and Jev look for, e.g. "New York Jets
    @ Detroit Lions (NFL, Sep 27) | spread | DET -7.5"."""
    kind = market.get("marketType") or ""
    what = TYPE_TEXT.get(kind, kind.replace("_", " ").lower())
    if event.get("league") in TENNIS:
        what = TENNIS_TEXT.get(kind, what)
    desc = market.get("description") or ""
    # "Adonai Mitchell 43.5 RECEIVING_YARDS" -> "Adonai Mitchell: receiving yards over 43.5"
    subject = subject_of(market)
    if subject:
        strike = market.get("strike")
        desc = f"{subject}: {what} over {strike}" if strike and strike != "0" else f"{subject}: {what}"
    elif kind in ("TOTAL", "TOTAL_1H", "TOTAL_SETS", "FIRST_INNING_TOTAL", "TEAM_TOTAL"):
        desc = f"{what} over {market.get('strike')}"
    when = datetime.fromtimestamp(event["startsTs"] / 1000, _ET).strftime("%b %-d") if event.get("startsTs") else ""
    head = f"{event.get('description', '')} ({event.get('league', '')}{', ' + when if when else ''})"
    return " | ".join(x for x in (head, what, desc) if x)


def summary(event: dict[str, Any], market: dict[str, Any], yes: dict, no: dict) -> str:
    """What the API says about the market, for review. Novig publishes no rules text
    per market; this is only its structured fields."""
    when = datetime.fromtimestamp(event["startsTs"] / 1000, _ET).strftime("%b %-d, %Y %-I:%M %p ET")
    kind = market.get("marketType")
    line = f" Line: {market['strike']}." if market.get("strike") not in (None, "0") else ""
    voids = {"FMV": "settles every outcome at its fair market value", "PUSH": "refunds every fill"}
    void = voids.get(market.get("voids"), market.get("voids"))
    return (f"Novig {event.get('league')} {kind} market on {event.get('description')}, starting {when}.{line} "
            f"Outcomes: {yes['name']} / {no['name']}. Exactly one outcome wins; if the market is voided, "
            f"it {void}.")


def row(event: dict[str, Any], market: dict[str, Any], now: float) -> tuple[tuple, tuple] | None:
    """A ``markets`` table row (venue 'N') and its ``novig_outcomes`` row, or None
    for markets we don't pair."""
    if (market.get("marketType") in SKIP_TYPES or market.get("status") != "OPEN"
            or event.get("status") not in ("OPEN_PREGAME", "OPEN_INGAME")):
        return None
    names = teams(event.get("description") or "")
    if names is None:
        return None  # a season future ("MVP Winner"), not a game: its money is tied up for months
    sides = orient(market)
    if sides is None:
        return None
    yes, no = sides
    start = market.get("startsTs") or event.get("startsTs")
    fee = market.get("fee") or {}
    return (
        ("N", market["marketId"], event.get("eventId"), event.get("league"), "Sports",
         title(event, market), _label(yes["name"], names, subject_of(market)),
         _label(no["name"], names, subject_of(market)), market.get("marketType"),
         start / 1000 if start else None, None, summary(event, market, yes, no),
         float(fee.get("coefficient") or 0.03), None, None, None, now),
        (market["marketId"], event.get("eventId"), yes["outcomeId"], no["outcomeId"],
         int(fee.get("charged", "WHEN_LIVE") == "WHEN_LIVE")),
    )


def asks_from(bids) -> list:
    """Offers of one outcome from the ``(price, qty)`` bids resting on the other:
    ``(1 - price, $1 contracts)``, cheapest first."""
    levels: dict[float, float] = {}
    for price, qty in bids:
        p = round(1.0 - float(price), 6)
        levels[p] = levels.get(p, 0.0) + qty / CONTRACT
    return sorted(levels.items())


def ladders(book: dict[str, Any], yes_id: str, no_id: str) -> tuple[list, list]:
    """(yes_asks, no_asks) in $1 contracts, cheapest first. Buying YES takes the bids
    resting on NO, at 1 - their price."""
    orders = book.get("orders") or {}

    def bids(outcome: str):
        return ((o["price"], o["qty"]) for o in orders.get(outcome) or [])

    return asks_from(bids(no_id)), asks_from(bids(yes_id))
