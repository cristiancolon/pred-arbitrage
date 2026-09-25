"""What one bankroll could have done with the recorded windows.

Summing every window's profit assumes a fresh bankroll for each one. Instead this
takes windows in the order they appeared, spending from one pool of cash, and keeps
each stake tied up until its market resolves (then returns stake plus profit). It
skips windows that closed too fast to act on, that return less than a minimum
annual rate, or whose edge is too big to be real (more than ``max_edge`` per $1 pair
usually means the two markets aren't the same bet, or one quote was stale), and
scales a window down when the cash left can't cover all of it.
It's still a best case: both legs fill at the quoted prices, and nothing settles
against you.
"""

import heapq
from collections import Counter

UNKNOWN_RESOLUTION_DAYS = 365.0


def simulate(windows, bankroll: float, min_window_s: float, min_annualized: float,
             max_edge: float | None = None) -> dict:
    """``windows``: dicts with start_ts, end_ts, max_profit, cost_at_max, days_to_resolve
    and max_top_edge."""
    cash = bankroll
    tied: list[tuple[float, float, float]] = []  # (resolves at, stake, profit)
    profit = 0.0
    taken = 0
    skipped: Counter[str] = Counter()
    for w in sorted(windows, key=lambda w: w["start_ts"]):
        t = w["start_ts"]
        while tied and tied[0][0] <= t:
            _, stake, gain = heapq.heappop(tied)
            cash += stake + gain
        cost, gain = w.get("cost_at_max") or 0.0, w.get("max_profit") or 0.0
        if cost <= 0 or gain <= 0:
            continue
        days = w.get("days_to_resolve")
        days = UNKNOWN_RESOLUTION_DAYS if days is None else max(days, 0.0)
        if max_edge is not None and (w.get("max_top_edge") or 0.0) > max_edge:
            skipped["suspicious edge"] += 1
            continue
        if (w["end_ts"] - w["start_ts"]) < min_window_s:
            skipped["too short"] += 1
            continue
        if gain / cost * 365 / max(days, 0.25) < min_annualized:
            skipped["low return"] += 1
            continue
        if cash < 1.0:
            skipped["no cash"] += 1
            continue
        frac = min(1.0, cash / cost)
        cash -= cost * frac
        profit += gain * frac
        taken += 1
        heapq.heappush(tied, (t + days * 86400, cost * frac, gain * frac))
    return {"bankroll": bankroll, "profit": profit, "taken": taken, "skipped": dict(skipped),
            "tied_up": sum(stake for _, stake, _ in tied), "min_window_s": min_window_s,
            "min_annualized": min_annualized, "max_edge": max_edge}
