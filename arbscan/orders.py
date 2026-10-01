"""Placing orders on both venues: the only part of arbscan that trades.

Everything else reads. These clients send immediate-or-cancel limit orders and read
back balances and positions; nothing calls them unless trading is switched on
(``arbscan order-test --send``).

Both venues quote a market from its YES side, so an order for NO is written as the
opposite trade in YES, at 1 - price:

- Kalshi (``POST /portfolio/events/orders``, the V2 endpoint; the legacy
  ``/portfolio/orders`` no longer takes orders): ``side`` is ``bid`` (buy YES) or
  ``ask`` (sell YES), and ``price`` is a YES price. Buying NO at p is an ask at
  1 - p; selling NO at p is a bid at 1 - p.
- Polymarket US (``POST /v1/orders`` on api.polymarket.us; gateway.polymarket.us is
  the public, read-only API): ``intent`` names the outcome and the action, and
  ``price.value`` is always the YES price, so a NO order at p carries 1 - p.

An order is sent once and never retried: a request that fails in flight may still
have reached the exchange. When the reply doesn't say what happened (a timeout, a
5xx, a Polymarket US order still working when it answered), the result's ``status``
is ``unknown``, and the caller has to read the position before doing anything else.
"""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

KALSHI_ORDERS = "/portfolio/events/orders"
PM_ORDERS = "/v1/orders"
PM_PREVIEW = "/v1/order/preview"
PM_BLOCK_S = 5  # Polymarket US replies once the order is done, or after this long
PM_INTENT = {("yes", "buy"): "ORDER_INTENT_BUY_LONG", ("yes", "sell"): "ORDER_INTENT_SELL_LONG",
             ("no", "buy"): "ORDER_INTENT_BUY_SHORT", ("no", "sell"): "ORDER_INTENT_SELL_SHORT"}
PM_FILLS = {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}
PM_DONE = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"}
EPS = 1e-9


@dataclass
class Order:
    venue: str  # K | P
    market: str  # Kalshi ticker | Polymarket US slug
    side: str  # yes | no: the outcome bought or sold
    action: str  # buy | sell
    qty: float  # contracts
    limit: float  # the worst price accepted for `side` (a NO price for NO)
    reduce_only: bool = False  # Kalshi: never trade more than the position held
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def yes_price(self) -> float:
        """The limit as both venues write it: a YES price."""
        return round(self.limit if self.side == "yes" else 1.0 - self.limit, 4)


@dataclass
class Result:
    order: Order
    status: str  # filled | partial | none | rejected | unknown
    filled: float = 0.0
    avg_price: float | None = None  # average price of `side`, paid or received
    fees: float = 0.0
    order_id: str | None = None
    sent: float = 0.0  # when the request went out (epoch seconds)
    rtt_ms: float = 0.0  # until the reply was back
    exch_ts: float | None = None  # when the exchange processed it, if it says
    error: str | None = None
    body: dict | None = None
    reply: Any = None


def _decimal(x: float) -> str:
    """A price as a plain decimal string: 0.55, 0.555."""
    return f"{round(x, 4):.4f}".rstrip("0").rstrip(".")


def status_of(filled: float, qty: float) -> str:
    return "filled" if filled >= qty - EPS else "partial" if filled > EPS else "none"


def _side_price(side: str, yes: float) -> float:
    return yes if side == "yes" else round(1.0 - yes, 6)


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:1000]


def _ts(v) -> float | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() if v else None
    except ValueError:
        return None


class _Venue:
    def __init__(self, client: httpx.AsyncClient, base: str, signer):
        self.client, self.base, self.signer = client, base.rstrip("/"), signer

    async def _request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        """One signed request: (response or None, sent at, round trip ms, transport error)."""
        url = self.base + path
        headers = self.signer.headers(method, httpx.URL(url).path)
        sent, t0 = time.time(), time.perf_counter()
        try:
            r = await self.client.request(method, url, json=body, params=params, headers=headers)
        except httpx.HTTPError as e:
            # Nothing reached the exchange if the connection never opened.
            return None, sent, 1000 * (time.perf_counter() - t0), e
        return r, sent, 1000 * (time.perf_counter() - t0), None

    async def _get(self, path: str, params: dict | None = None) -> Any:
        r, _, _, err = await self._request("GET", path, params=params)
        if err is not None:
            raise err
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        r, _, _, err = await self._request("POST", path, body)
        if err is not None:
            raise err
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    async def _place(self, o: Order, path: str, body: dict, ok: tuple[int, ...]) -> Result:
        r, sent, ms, err = await self._request("POST", path, body)
        res = Result(o, "unknown", sent=sent, rtt_ms=ms, body=body)
        if err is not None:
            res.error = f"{type(err).__name__}: {err}"
            if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
                res.status = "rejected"
            return res
        res.reply = _json(r)
        if r.status_code in ok:
            return self.parse(o, res.reply, res)
        res.error = f"HTTP {r.status_code}: {r.text[:300]}"
        # A 4xx is an order the exchange refused; 409 (Kalshi: that client order id
        # was already used) and anything 5xx leave it unknown.
        if 400 <= r.status_code < 500 and r.status_code != 409:
            res.status = "rejected"
        return res

    @staticmethod
    def parse(o: Order, reply: Any, res: Result) -> Result:
        raise NotImplementedError


class KalshiTrading(_Venue):
    """Kalshi's trading API: V2 orders, balance, positions and fills."""

    @staticmethod
    def body(o: Order) -> dict:
        bid = (o.side == "yes") == (o.action == "buy")  # buying YES, or selling NO
        b = {"ticker": o.market, "client_order_id": o.client_id, "side": "bid" if bid else "ask",
             "count": f"{o.qty:.2f}", "price": f"{o.yes_price:.4f}", "time_in_force": "immediate_or_cancel",
             "self_trade_prevention_type": "taker_at_cross"}
        if o.reduce_only:
            b["reduce_only"] = True
        return b

    async def place(self, o: Order) -> Result:
        return await self._place(o, KALSHI_ORDERS, self.body(o), (200, 201))

    @staticmethod
    def parse(o: Order, reply: Any, res: Result) -> Result:
        res.order_id = reply.get("order_id")
        res.filled = float(reply.get("fill_count") or 0)
        if res.filled > EPS and reply.get("average_fill_price") is not None:
            res.avg_price = _side_price(o.side, float(reply["average_fill_price"]))  # quoted as YES
        res.fees = res.filled * float(reply.get("average_fee_paid") or 0)
        res.exch_ts = reply["ts_ms"] / 1000 if reply.get("ts_ms") else None
        res.status = status_of(res.filled, o.qty)
        return res

    async def balance(self) -> dict:
        return await self._get("/portfolio/balance")

    async def position(self, ticker: str) -> float:
        """Contracts held: positive for YES, negative for NO."""
        j = await self._get("/portfolio/positions", {"ticker": ticker, "count_filter": "position"})
        for p in j.get("market_positions") or []:
            if p.get("ticker") == ticker:
                return float(p.get("position_fp") or 0)
        return 0.0

    async def positions(self) -> dict[str, float]:
        """Every market the account holds contracts in: ticker -> signed position."""
        out, cursor = {}, None
        while True:
            params = {"count_filter": "position", "limit": 1000} | ({"cursor": cursor} if cursor else {})
            j = await self._get("/portfolio/positions", params)
            for p in j.get("market_positions") or []:
                if float(p.get("position_fp") or 0):
                    out[p["ticker"]] = float(p["position_fp"])
            cursor = j.get("cursor")
            if not cursor:
                return out

    async def shard_cash(self) -> dict[int, float]:
        """Cash per exchange shard: Kalshi only fills an order from the cash on its market's shard."""
        b = await self.balance()
        return {int(x.get("exchange_index") or 0): float(x["balance"]) for x in b.get("balance_breakdown") or []}

    async def transfer(self, src: int, dst: int, dollars: float) -> dict:
        """Move cash between two exchange shards of the account."""
        body = {"source": "event_contract", "destination": "event_contract", "source_exchange_shard": src,
                "destination_exchange_shard": dst, "amount": round(dollars * 10000)}  # centicents
        return await self._post("/portfolio/intra_exchange_instance_transfer", body)

    async def allocate(self, percent: dict[int, int]) -> dict:
        """Kalshi's own target split of the account's cash across shards (whole percents)."""
        body = {"allocations": [{"exchange_index": s, "percent": p} for s, p in sorted(percent.items())],
                "resting_margin_reservation": "sum"}
        return await self._post("/portfolio/target_balance_allocation", body)

    async def attested_until(self) -> float | None:
        """When the account's location check for API keys lapses; after that Kalshi takes
        no API orders on sports, elections or entertainment. None: never attested."""
        return (await self._get("/api_keys")).get("api_key_region_expiration_ts")

    async def fills(self, order_id: str) -> list[dict]:
        return (await self._get("/portfolio/fills", {"order_id": order_id})).get("fills") or []

    async def market(self, ticker: str) -> dict:
        return (await self._get(f"/markets/{ticker}")).get("market") or {}


class PMTrading(_Venue):
    """Polymarket US's trading API: orders, order preview, balance and positions."""

    @staticmethod
    def body(o: Order) -> dict:
        return {"marketSlug": o.market, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": _decimal(o.yes_price), "currency": "USD"}, "quantity": o.qty,
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "intent": PM_INTENT[(o.side, o.action)],
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "synchronousExecution": True, "maxBlockTime": str(PM_BLOCK_S)}

    async def place(self, o: Order) -> Result:
        return await self._place(o, PM_ORDERS, self.body(o), (200, 201))

    async def preview(self, o: Order) -> Result:
        """Validate an order without placing it. ``status`` is ``none`` if accepted."""
        body = {k: v for k, v in self.body(o).items() if k not in ("synchronousExecution", "maxBlockTime")}
        r, sent, ms, err = await self._request("POST", PM_PREVIEW, {"request": body})
        res = Result(o, "rejected", sent=sent, rtt_ms=ms, body=body)
        if err is not None:
            res.status, res.error = "unknown", f"{type(err).__name__}: {err}"
        else:
            res.reply = _json(r)
            if r.status_code == 200:
                res.status = "none"
            else:
                res.error = f"HTTP {r.status_code}: {r.text[:300]}"
        return res

    @staticmethod
    def parse(o: Order, reply: Any, res: Result) -> Result:
        res.order_id = reply.get("id")
        execs = reply.get("executions") or []
        shares = notional = fees = 0.0
        for e in execs:
            if e.get("type") in PM_FILLS:
                q = float(e.get("lastShares") or 0)
                shares += q
                notional += q * float((e.get("lastPx") or {}).get("value") or 0)
                fees += float((e.get("commissionNotionalCollected") or {}).get("value") or 0)
            if e.get("type") == "EXECUTION_TYPE_REJECTED":
                res.error = e.get("orderRejectReason") or e.get("text") or "rejected"
            res.exch_ts = _ts(e.get("transactTime")) or res.exch_ts
        PMTrading._tally(o, res, (execs[-1].get("order") or {}) if execs else {}, shares, notional, fees)
        return res

    @staticmethod
    def _tally(o: Order, res: Result, order: dict, shares: float, notional: float, fees: float) -> None:
        """Fill, price, fees and status from the executions' sums and the order's own totals."""
        cum = float(order.get("cumQuantity") or 0)
        if cum > shares + EPS:  # the order's own tally has fills the executions didn't list
            shares = cum
            px = (order.get("avgPx") or {}).get("value")
            notional = cum * float(px) if px else notional
            fees = max(fees, float((order.get("commissionNotionalTotalCollected") or {}).get("value") or 0))
        res.filled, res.fees = shares, fees
        if shares > EPS and notional > 0:
            res.avg_price = _side_price(o.side, notional / shares)  # quoted as YES
        if order.get("state") == "ORDER_STATE_REJECTED" or (res.error is not None and shares <= EPS):
            res.status, res.error = "rejected", res.error or "rejected"
        elif order.get("state") in PM_DONE:
            res.status = status_of(shares, o.qty)
        else:
            res.status = "unknown"  # still working when the reply came back: read the order

    async def order(self, order_id: str) -> dict:
        return (await self._get(f"/v1/order/{order_id}")).get("order") or {}

    async def resolve(self, res: Result) -> Result:
        """Settle an ``unknown`` result by reading the order back."""
        if res.order_id:
            order = await self.order(res.order_id)
            self._tally(res.order, res, order, 0.0, 0.0, 0.0)
        return res

    async def book(self, slug: str) -> dict:
        """The market's book (``marketData``) straight from the exchange. The route is
        cached for 30 s at the edge; a unique query string gets past that."""
        return (await self._get(f"/v1/markets/{slug}/book", {"_": str(time.time_ns())})).get("marketData") or {}

    async def balance(self) -> dict:
        j = await self._get("/v1/account/balances")
        return next((b for b in j.get("balances") or [] if b.get("currency", "USD") == "USD"), {})

    async def position(self, slug: str) -> float:
        """Contracts held: positive for YES, negative for NO."""
        p = ((await self._get("/v1/portfolio/positions", {"market": slug})).get("positions") or {}).get(slug)
        return self._net(p) if p else 0.0

    async def positions(self) -> dict[str, float]:
        """Every market the account holds contracts in: slug -> signed position."""
        out, cursor = {}, None
        while True:
            j = await self._get("/v1/portfolio/positions", {"limit": 100} | ({"cursor": cursor} if cursor else {}))
            for slug, p in (j.get("positions") or {}).items():
                if self._net(p):
                    out[slug] = self._net(p)
            cursor = j.get("nextCursor")
            if j.get("eof", True) or not cursor:
                return out

    @staticmethod
    def _net(p: dict) -> float:
        return float(p.get("netPositionDecimal") or p.get("netPosition") or 0)


def record(db, res: Result, mode: str, trade: str | None = None) -> None:
    """Keep every order sent (or built, in a dry run) in ``live_orders``."""
    o = res.order
    db.execute(
        "INSERT OR REPLACE INTO live_orders (id, ts, mode, venue, market, side, action, qty, limit_price, body, "
        "status, filled, avg_price, fees, order_id, rtt_ms, exch_ts, error, reply, trade) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (o.client_id, res.sent or time.time(), mode, o.venue, o.market, o.side, o.action, o.qty, o.limit,
         json.dumps(res.body, separators=(",", ":")), res.status, res.filled, res.avg_price, res.fees, res.order_id,
         res.rtt_ms, res.exch_ts, res.error,
         json.dumps(res.reply, separators=(",", ":"), default=str) if res.reply is not None else None, trade))
