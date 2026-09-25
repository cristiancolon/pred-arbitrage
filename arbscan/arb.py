"""Cross-venue arbitrage math.

An arb buys complementary outcomes on the two venues (e.g. YES on Kalshi and NO on
Polymarket US) so exactly one leg pays $1 at resolution. It is profitable when the
combined price plus both taker fees is below $1.
"""

import math
from dataclasses import dataclass

from .book import Level
from .fees import per_contract

EPS = 1e-9


@dataclass(frozen=True)
class Leg:
    asks: list[Level]  # cheapest first
    fee_coef: float


@dataclass(frozen=True)
class ArbResult:
    top_edge: float | None  # $ profit per contract pair at the best prices (can be < 0)
    size: int  # whole contract pairs executable at marginal edge > min_edge
    cost: float  # $ spent on those pairs, fees included
    profit: float  # size * $1 - cost
    last_edge: float | None  # marginal edge of the last pair taken

    @property
    def positive(self) -> bool:
        return self.size >= 1 and self.profit > 0


def unit_cost(pa: float, a_coef: float, pb: float, b_coef: float) -> float:
    return pa + pb + per_contract(a_coef, pa) + per_contract(b_coef, pb)


def top_edge(pa: float | None, a_coef: float, pb: float | None, b_coef: float) -> float | None:
    if pa is None or pb is None:
        return None
    return 1.0 - unit_cost(pa, a_coef, pb, b_coef)


def walk(a: Leg, b: Leg, min_edge: float = 0.0, max_size: float | None = None,
         budget_a: float | None = None, budget_b: float | None = None) -> ArbResult:
    """Walk both ask ladders together, taking pairs while each marginal pair clears
    ``min_edge`` dollars of profit after fees, and while each leg's spend (fees
    included) stays within its venue's ``budget`` (the cash you hold there).

    Size is floored to whole contracts because Polymarket US does not trade fractions.
    """
    if not a.asks or not b.asks:
        return ArbResult(None, 0, 0.0, 0.0, None)

    first = 1.0 - unit_cost(a.asks[0][0], a.fee_coef, b.asks[0][0], b.fee_coef)
    i = j = 0
    rem_a, rem_b = a.asks[0][1], b.asks[0][1]
    size = cost = spent_a = spent_b = 0.0
    last_unit = None
    while i < len(a.asks) and j < len(b.asks):
        pa, pb = a.asks[i][0], b.asks[j][0]
        unit_a, unit_b = pa + per_contract(a.fee_coef, pa), pb + per_contract(b.fee_coef, pb)
        unit = unit_a + unit_b
        if 1.0 - unit <= min_edge:
            break
        take = min(rem_a, rem_b)
        if max_size is not None:
            take = min(take, max_size - size)
        if budget_a is not None:
            take = min(take, (budget_a - spent_a) / unit_a)
        if budget_b is not None:
            take = min(take, (budget_b - spent_b) / unit_b)
        if take <= EPS:
            break
        size += take
        cost += take * unit
        spent_a += take * unit_a
        spent_b += take * unit_b
        last_unit = unit
        rem_a -= take
        rem_b -= take
        if rem_a <= EPS:
            i += 1
            rem_a = a.asks[i][1] if i < len(a.asks) else 0.0
        if rem_b <= EPS:
            j += 1
            rem_b = b.asks[j][1] if j < len(b.asks) else 0.0

    whole = math.floor(size + EPS)
    if last_unit is not None and whole < size:
        # Drop the fractional tail, which was bought at the last (worst) unit cost.
        cost -= (size - whole) * last_unit
    if whole == 0:
        return ArbResult(first, 0, 0.0, 0.0, None)
    return ArbResult(first, whole, cost, whole - cost, 1.0 - last_unit)


# Directions are named by what you buy on each venue. With relation "same" the two
# markets ask the same question; with "inverse" Polymarket's YES is Kalshi's NO
# (e.g. Kalshi "Team B wins" vs a Polymarket moneyline whose long side is Team A).
DIRECTIONS = {
    "same": (("K:YES+P:NO", "yes", "no"), ("K:NO+P:YES", "no", "yes")),
    "inverse": (("K:YES+P:YES", "yes", "yes"), ("K:NO+P:NO", "no", "no")),
}


def directions(relation: str) -> tuple[tuple[str, str, str], ...]:
    """[(label, kalshi_side, pm_side), ...] for a pair relation."""
    return DIRECTIONS[relation]
