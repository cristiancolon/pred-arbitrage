"""Summaries of what the scanner has seen: is there money here, and how much?"""

import sqlite3
import statistics
import time
from datetime import datetime

EDGE_BUCKETS = [(0.0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, 1.0)]
SUSPICIOUS_EDGE = 0.05
SUSPICIOUS_DURATION_S = 3600


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def _dur(s: float) -> str:
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def run(db: sqlite3.Connection, hours: float, min_profit: float, top: int) -> None:
    now = time.time()
    since = now - hours * 3600
    sw = db.execute(
        "SELECT COUNT(*) n, MIN(ts) t0, MAX(ts) t1, AVG(dur_ms) dur, SUM(errors) err, "
        "SUM(depth_fetches) depth FROM sweeps WHERE ts >= ?", (since,)
    ).fetchone()
    print(f"Window: last {hours:g}h")
    if not sw["n"]:
        print("No scanner sweeps recorded in this window. Is `arbscan scan` running?")
        return
    last_pairs = db.execute("SELECT n_pairs FROM sweeps ORDER BY ts DESC LIMIT 1").fetchone()[0]
    span = sw["t1"] - sw["t0"]
    print(f"Sweeps: {sw['n']} over {_dur(span)} (avg {sw['dur'] / 1000:.1f}s each), {last_pairs} pairs watched, "
          f"{sw['depth']} depth fetches, {sw['err'] or 0} API errors")
    gaps = db.execute(
        "SELECT MAX(gap) FROM (SELECT ts - LAG(ts) OVER (ORDER BY ts) gap FROM sweeps WHERE ts >= ?)", (since,)
    ).fetchone()[0]
    if gaps and gaps > 120:
        print(f"  note: longest gap between sweeps was {_dur(gaps)}; the scanner was down or stalled")

    eps = db.execute(
        "SELECT * FROM episodes WHERE start_ts >= ? AND max_profit >= ? ORDER BY start_ts", (since, min_profit)
    ).fetchall()
    print()
    if not eps:
        print("No profitable windows (after fees) in this window.")
        return

    durations = [e["end_ts"] - e["start_ts"] for e in eps]
    total = sum(e["max_profit"] for e in eps)
    capital = sum(e["cost_at_max"] or 0 for e in eps)
    print(f"Profitable windows: {len(eps)}")
    print(f"  duration: median {_dur(statistics.median(durations))}, p90 {_dur(_pct(durations, 0.9))}, "
          f"{sum(d >= 10 for d in durations)} lasted 10s+, {sum(d >= 60 for d in durations)} lasted 60s+")
    print(f"  best-case profit if every window were caught once at its peak: ${total:,.2f} "
          f"on ${capital:,.2f} of capital ({100 * total / capital if capital else 0:.2f}%)")
    per_day = total / max(span, 3600) * 86400
    print(f"  that is ~${per_day:,.2f}/day at the observed rate, before execution slippage and failed legs")

    print("\nBy peak edge (profit per $1 pair after fees):")
    for lo, hi in EDGE_BUCKETS:
        b = [e for e in eps if lo <= (e["max_top_edge"] or 0) < hi]
        if b:
            label = f"{100 * lo:g}c+" if hi >= 1.0 else f"{100 * lo:g}-{100 * hi:g}c"
            print(f"  {label:>8}  {len(b):5d} windows  ${sum(e['max_profit'] for e in b):10,.2f}")

    agg: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for e in eps:
        agg.setdefault((e["pair"], e["direction"]), []).append(e)
    rows = sorted(agg.items(), key=lambda kv: -sum(e["max_profit"] for e in kv[1]))[:top]
    print(f"\nTop {len(rows)} pairs by best-case profit:")
    print(f"  {'profit':>9} {'windows':>7} {'peak':>6} {'med dur':>7} {'ret':>6} {'annual':>7} {'days':>5}  pair / direction")
    for (pair, direction), es in rows:
        prof = sum(e["max_profit"] for e in es)
        cost = sum(e["cost_at_max"] or 0 for e in es)
        days = [e["days_to_resolve"] for e in es if e["days_to_resolve"] is not None]
        d = statistics.median(days) if days else None
        ret = prof / cost if cost else 0.0
        annual = f"{100 * ret * 365 / d:6.0f}%" if d and d > 0 else "     ?"
        peak = max(e["max_top_edge"] or 0 for e in es)
        med = statistics.median(e["end_ts"] - e["start_ts"] for e in es)
        flag = ""
        if peak >= SUSPICIOUS_EDGE or med >= SUSPICIOUS_DURATION_S:
            flag = "  <- check: large or lasting gaps usually mean the markets differ"
        print(f"  ${prof:8,.2f} {len(es):7d} {100 * peak:5.1f}c {_dur(med):>7} {100 * ret:5.1f}% {annual} "
              f"{(f'{d:5.1f}' if d is not None else '    ?')}  {pair} {direction}{flag}")

    recent = db.execute(
        "SELECT ts, pair, direction, size, profit, top_edge FROM opportunities ORDER BY ts DESC LIMIT 5"
    ).fetchall()
    if recent:
        print("\nMost recent observations:")
        for r in recent:
            when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
            print(f"  {when}  {r['pair']} {r['direction']}  {r['size']} @ {100 * r['top_edge']:.2f}c  "
                  f"profit ${r['profit']:.2f}")
