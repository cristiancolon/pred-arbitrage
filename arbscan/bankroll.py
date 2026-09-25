"""Which windows to pick, and what one bankroll could have done with them.

Money in a pair is tied up until the market resolves, so a small edge that resolves
tonight beats a bigger one that resolves in three months. Windows are ranked by
their return per year of lock-up (profit / capital, scaled by the days until the
market resolves), and a window is a *pick* only if it also passes the rules in
``PickRules``: open long enough to act on, an edge small enough to be real (a larger
one usually means the two markets aren't the same bet, or one quote was stale),
resolving soon enough, and returning enough per year.

Summing every window's profit assumes a fresh bankroll for each one. ``simulate``
instead spends one pool of cash: whenever cash is free it funds the best-ranked
picks open at that moment, each up to its stake cap (``PickRules.stake_fraction``)
and the cash left, and keeps each stake tied up until its market resolves (then
returns stake plus profit).
It's still a best case: each window counts at its peak, both legs fill at the
quoted prices, and nothing settles against you.
"""

import heapq
import math
from collections import Counter
from dataclasses import dataclass

UNKNOWN_RESOLUTION_DAYS = 365.0
# Markets resolving within hours all count as this far out when annualizing, so a
# window closing in five minutes doesn't get an absurd rate from a tiny edge.
MIN_ANNUALIZE_DAYS = 0.25
# Money stays tied up at least this long, even in a market already past its expected
# resolution time (a game in progress): settling takes a while.
MIN_LOCK_DAYS = 1 / 24


def days_locked(w: dict) -> float:
    d = w.get("days_to_resolve")
    return UNKNOWN_RESOLUTION_DAYS if d is None else max(d, MIN_LOCK_DAYS)


def annualized(profit: float | None, cost: float | None, days: float | None) -> float | None:
    """Return per year on the capital, for money tied up ``days`` (None = unknown)."""
    if not cost or profit is None:
        return None
    d = UNKNOWN_RESOLUTION_DAYS if days is None else days
    return profit / cost * 365 / max(d, MIN_ANNUALIZE_DAYS)


def window_rate(w: dict) -> float:
    return annualized(w.get("max_profit"), w.get("cost_at_max"), w.get("days_to_resolve")) or 0.0


@dataclass(frozen=True)
class PickRules:
    min_window_s: float = 1.0
    min_annualized: float = 1.0
    max_edge: float | None = 0.05
    max_days: float | None = 7.0
    # Sizing: one pick may use at most this share of the money (on each venue, when
    # paper trading) if it resolves within a day, and proportionally less the longer
    # it ties the money up, so a multi-day pick can't starve the quick ones. None: no cap.
    max_stake: float | None = 0.5

    @classmethod
    def from_config(cls, cfg) -> "PickRules":
        return cls(cfg.pick_min_window_s, cfg.pick_min_annualized_return, cfg.pick_max_edge or None,
                   cfg.pick_max_days or None, cfg.pick_max_stake or None)

    def stake_fraction(self, days: float | None) -> float:
        if self.max_stake is None:
            return 1.0
        d = UNKNOWN_RESOLUTION_DAYS if days is None else max(days, MIN_LOCK_DAYS)
        return self.max_stake * min(1.0, 1.0 / d)

    def reason(self, edge: float | None, open_s: float, days: float | None, rate: float | None) -> str | None:
        """Why a window isn't a pick, or None if it is."""
        if self.max_edge is not None and (edge or 0.0) > self.max_edge:
            return "suspicious edge"
        if open_s < self.min_window_s:
            return "too short"
        if self.max_days is not None and (days is None or days > self.max_days):
            return "resolves too late"
        if (rate or 0.0) < self.min_annualized:
            return "low return"
        return None

    def window_reason(self, w: dict) -> str | None:
        return self.reason(w.get("max_top_edge"), w["end_ts"] - w["start_ts"], w.get("days_to_resolve"),
                           window_rate(w))

    def describe(self) -> dict:
        return {"min_window_s": self.min_window_s, "min_annualized": self.min_annualized,
                "max_edge": self.max_edge, "max_days": self.max_days, "max_stake": self.max_stake}


def simulate(windows, bankroll: float, rules: PickRules, now: float | None = None) -> dict:
    """``windows``: dicts with start_ts, end_ts, max_profit, cost_at_max, days_to_resolve
    and max_top_edge. ``tied_up`` counts stakes still unresolved at ``now`` (default: the
    last pick)."""
    skipped: Counter[str] = Counter()
    picks = []
    for w in windows:
        if (w.get("cost_at_max") or 0.0) <= 0 or (w.get("max_profit") or 0.0) <= 0:
            continue
        why = rules.window_reason(w)
        if why:
            skipped[why] += 1
        else:
            # Actionable once it has been open min_window_s; open until end_ts.
            picks.append((w["start_ts"] + rules.min_window_s, w))
    picks.sort(key=lambda x: x[0])

    cash, profit, taken = bankroll, 0.0, 0
    tied: list[tuple[float, float, float]] = []  # (resolves at, stake, profit)
    open_: list[dict] = []
    i = 0
    while True:
        t = min(picks[i][0] if i < len(picks) else math.inf, tied[0][0] if tied and open_ else math.inf)
        if t == math.inf:
            break
        while tied and tied[0][0] <= t:
            _, stake, gain = heapq.heappop(tied)
            cash += stake + gain
        while i < len(picks) and picks[i][0] <= t:
            open_.append(picks[i][1])
            i += 1
        open_ = [w for w in open_ if w["end_ts"] >= t]
        open_.sort(key=lambda w: (window_rate(w), w["max_profit"]), reverse=True)
        while open_ and cash >= 1.0:
            w = open_.pop(0)
            capital = cash + sum(stake for _, stake, _ in tied)
            cap = rules.stake_fraction(w.get("days_to_resolve")) * capital
            frac = min(1.0, cash / w["cost_at_max"], cap / w["cost_at_max"])
            cash -= w["cost_at_max"] * frac
            profit += w["max_profit"] * frac
            taken += 1
            heapq.heappush(tied, (t + days_locked(w) * 86400, w["cost_at_max"] * frac, w["max_profit"] * frac))
    skipped["no cash"] = len(picks) - taken
    if now is not None:
        tied = [x for x in tied if x[0] > now]
    return {"bankroll": bankroll, "profit": profit, "taken": taken, "picks": len(picks),
            "skipped": {k: v for k, v in skipped.items() if v}, "tied_up": sum(s for _, s, _ in tied),
            **rules.describe()}
