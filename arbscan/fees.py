"""Taker fee models for both venues.

Both venues charge ``coef * contracts * p * (1 - p)`` to takers, where ``p`` is the
trade price in dollars. Rounding is ignored: Kalshi aligns direct-member balances to
$0.0001 and Polymarket US to whole cents, both negligible at the sizes we care about.
"""

KALSHI_BASE_TAKER_COEF = 0.07

# Kalshi fee types that use the general quadratic taker formula.
_KALSHI_QUADRATIC = {"quadratic", "quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


def kalshi_taker_coef(fee_type: str | None, multiplier: float | None) -> float:
    """Taker coefficient for a Kalshi series.

    Unknown fee types (e.g. 'flat', which no open series used when this was written)
    fall back to the general rate so we never understate costs.
    """
    mult = 1.0 if multiplier is None else float(multiplier)
    if fee_type in _KALSHI_QUADRATIC:
        return KALSHI_BASE_TAKER_COEF * mult
    return KALSHI_BASE_TAKER_COEF * max(mult, 1.0)


def per_contract(coef: float, price: float) -> float:
    """Fee in dollars for one contract traded at ``price``."""
    return coef * price * (1.0 - price)
