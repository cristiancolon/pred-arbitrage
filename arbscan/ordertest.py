"""``arbscan order-test``: buy one contract and sell it straight back, to check live
order placement end to end on one venue. With ``--send`` these are real orders; a
round trip costs the spread plus two taker fees, a few cents.

For each side asked for (YES, NO or both), on a market you name:

1. refuse if the account already holds a position in that market (it could be one
   you took by hand), the spread is wider than ``MAX_SPREAD``, or the price is near
   0 or 1;
2. buy 1 contract at the best ask, immediate-or-cancel;
3. sell whatever filled at the best bid, immediate-or-cancel (reduce-only on Kalshi);
4. read the position back: it should be flat again.

It prints each order's fill, price, fees and round trip, and how much cash each one
moved according to the venue's own balance, so the fee model can be checked against
what the venue charged. Without ``--send`` it only prints the orders it would send,
and has Polymarket US validate them (order preview). Every order sent is kept in
``live_orders`` with mode ``test``.
"""

import asyncio
import json

from . import venues
from .auth import KalshiSigner, PMSigner
from .book import kalshi_ladders, pmus_ladders
from .http import Api, make_client
from .orders import EPS, KalshiTrading, Order, PMTrading, Result, record

MAX_SPREAD = 0.03
PRICE_MIN, PRICE_MAX = 0.03, 0.97
SELL_LOSS_MAX = 0.10  # don't sell back below the buy price by more than this
SETTLE_S = 1.5  # let the venue's balance catch up before reading it


def quote(yes_asks, no_asks, side: str) -> tuple[float | None, float | None]:
    """(best ask, best bid) for one side. Selling a side at p is buying the other at 1 - p."""
    asks, other = (yes_asks, no_asks) if side == "yes" else (no_asks, yes_asks)
    return (asks[0][0] if asks else None), (round(1.0 - other[0][0], 4) if other else None)


def check(ask: float | None, bid: float | None) -> str | None:
    """Why a book is unfit for a test round trip, or None."""
    if ask is None or bid is None:
        return "no ask or no bid"
    if not PRICE_MIN <= ask <= PRICE_MAX:
        return f"ask {ask} is too close to 0 or 1"
    if ask - bid > MAX_SPREAD + EPS:
        return f"spread {ask - bid:.3f} is wider than {MAX_SPREAD}"
    if bid > ask + EPS:
        return f"bid {bid} is above ask {ask}: the book is stale"
    return None


async def cash(trader) -> float:
    b = await trader.balance()
    if isinstance(trader, KalshiTrading):
        return float(b["balance_dollars"]) if b.get("balance_dollars") is not None else b["balance"] / 100
    return float(b.get("buyingPower") or 0)


def show(res: Result, moved: float | None) -> str:
    o = res.order
    price = f"{res.avg_price:.4f}" if res.avg_price is not None else "-"
    lag = f", exchange +{1000 * (res.exch_ts - res.sent):.0f} ms" if res.exch_ts else ""
    cash_note = ""
    if moved is not None and res.filled > EPS and res.avg_price is not None:
        sign = -1 if o.action == "buy" else 1
        expect = sign * res.avg_price * res.filled - res.fees
        cash_note = f"; cash {moved:+.4f} (expected {expect:+.4f} from price and fees)"
    return (f"{o.action} {o.qty:g} {o.side.upper()} at <= {o.limit:.4f}: {res.status}, "
            f"filled {res.filled:g} at {price}, fees {res.fees:.4f}, reply {res.rtt_ms:.0f} ms{lag}{cash_note}"
            + (f" [{res.error}]" if res.error else ""))


class Tester:
    def __init__(self, trader, ladders, venue: str, db, out=print, settle_s: float = SETTLE_S):
        self.trader, self.ladders, self.venue, self.db, self.out = trader, ladders, venue, db, out
        self.settle_s = settle_s

    async def _send(self, o: Order) -> tuple[Result, float]:
        before = await cash(self.trader)
        res = await self.trader.place(o)
        if res.status == "unknown" and isinstance(self.trader, PMTrading):
            res = await self.trader.resolve(res)
        record(self.db, res, "test")
        self.db.commit()
        await asyncio.sleep(self.settle_s)
        return res, await cash(self.trader) - before

    async def round_trip(self, market: str, side: str, send: bool) -> list[Result]:
        ask, bid = quote(*await self.ladders(market), side)
        problem = check(ask, bid)
        if problem:
            self.out(f"{side.upper()}: {problem}; skipped")
            return []
        buy = Order(self.venue, market, side, "buy", 1, ask)
        if not send:
            self.out(f"{side.upper()}: would buy 1 at {ask} and sell back at about {bid}: "
                     f"{json.dumps(self.trader.body(buy))}")
            if isinstance(self.trader, PMTrading):
                pv = await self.trader.preview(buy)
                self.out(f"  Polymarket US preview: {'accepted' if pv.status == 'none' else pv.status}"
                         + (f" [{pv.error}]" if pv.error else ""))
            return []
        r1, moved = await self._send(buy)
        self.out(show(r1, moved))
        out = [r1]
        held = r1.filled
        if r1.status == "unknown":
            self.out("  the buy's outcome is unknown: not selling; check the position by hand")
            return out
        if held > EPS:
            _, bid = quote(*await self.ladders(market), side)
            floor = (r1.avg_price or ask) - SELL_LOSS_MAX
            if bid is None or bid < floor:
                self.out(f"  best bid {bid} is below {floor:.4f}: not selling; the contract is still held")
                return out
            r2, moved = await self._send(Order(self.venue, market, side, "sell", held, bid, reduce_only=True))
            self.out(show(r2, moved))
            out.append(r2)
            if r1.avg_price is not None and r2.avg_price is not None and r2.filled > EPS:
                cost = (r1.avg_price - r2.avg_price) * r2.filled + r1.fees + r2.fees
                self.out(f"  round trip cost {cost:.4f} (spread {r1.avg_price - r2.avg_price:.4f}, "
                         f"fees {r1.fees + r2.fees:.4f})")
        return out


def _venue(cfg, client, venue: str):
    if venue == "K":
        trader = KalshiTrading(client, cfg.kalshi_base,
                               KalshiSigner.from_file(cfg.kalshi_key_id, cfg.kalshi_private_key_path))
        k = venues.Kalshi(Api(client, cfg.kalshi_base, cfg.kalshi_rps, "kalshi"))

        async def ladders(m):
            return kalshi_ladders((await k.orderbooks([m])).get(m) or {})
    else:
        trader = PMTrading(client, cfg.pmus_trade_base, PMSigner(cfg.pmus_key_id, cfg.pmus_secret_key))
        p = venues.PolymarketUS(Api(client, cfg.pmus_base, cfg.pmus_rps, "pmus"))

        async def ladders(m):
            return pmus_ladders(await p.book(m))
    return trader, ladders


async def run(cfg, db, venue: str, market: str, sides: list[str], send: bool) -> None:
    async with make_client(trading=True) as client:
        trader, ladders = _venue(cfg, client, venue)
        held = await trader.position(market)
        if abs(held) > EPS:
            print(f"The account already holds {held:g} contracts in {market}; pick another market.")
            return
        if isinstance(trader, KalshiTrading):
            m = await trader.market(market)
            shard = m.get("exchange_index", 0)
            bal = await trader.balance()
            on_shard = next((float(b["balance"]) for b in bal.get("balance_breakdown") or []
                             if b.get("exchange_index") == shard), None)
            print(f"{market}: {m.get('status')}, exchange shard {shard}, cash on that shard "
                  f"{'unknown' if on_shard is None else f'${on_shard:.2f}'}")
        print(f"cash: ${await cash(trader):.4f}" + ("" if send else " (showing the orders only; --send places them)"))
        for side in sides:
            await Tester(trader, ladders, venue, db).round_trip(market, side, send)
        if send:
            print(f"position now: {await trader.position(market):g} (should be 0); cash: ${await cash(trader):.4f}")
