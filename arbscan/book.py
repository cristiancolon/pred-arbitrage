"""Order-book normalization.

Everything downstream works with *ask ladders*: the (price, qty) levels you can buy a
given outcome at, sorted cheapest first. Each venue's raw book is converted here.
"""

from typing import Any, Iterable

Level = tuple[float, float]  # (price in dollars, contracts)


def _sorted_asks(levels: Iterable[Level]) -> list[Level]:
    return sorted((lv for lv in levels if lv[1] > 0), key=lambda lv: lv[0])


def _complement(levels: Iterable[Level]) -> list[Level]:
    # A bid for one side at p is an offer of the other side at 1 - p.
    return _sorted_asks((round(1.0 - p, 6), q) for p, q in levels)


def kalshi_ladders(orderbook_fp: dict[str, Any]) -> tuple[list[Level], list[Level]]:
    """(yes_asks, no_asks) from a Kalshi ``orderbook_fp`` object.

    Kalshi only publishes bids. Buying YES matches resting NO bids and vice versa.
    """
    yes_bids = [(float(p), float(q)) for p, q in orderbook_fp.get("yes_dollars") or []]
    no_bids = [(float(p), float(q)) for p, q in orderbook_fp.get("no_dollars") or []]
    return _complement(no_bids), _complement(yes_bids)


def pmus_ladders(market_data: dict[str, Any]) -> tuple[list[Level], list[Level]]:
    """(yes_asks, no_asks) from a Polymarket US ``marketData`` object.

    Polymarket US has one instrument per market. Buying NO means selling (shorting)
    YES into the bids, which costs 1 - bid per contract.
    """
    bids = [(float(e["px"]["value"]), float(e["qty"])) for e in market_data.get("bids") or []]
    offers = [(float(e["px"]["value"]), float(e["qty"])) for e in market_data.get("offers") or []]
    return _sorted_asks(offers), _complement(bids)


def top_n(levels: list[Level], n: int = 10) -> list[list[float]]:
    """Compact JSON-friendly copy of the best ``n`` levels, for storage."""
    return [[p, q] for p, q in levels[:n]]
