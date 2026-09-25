"""Read-side queries for the dashboard. Each takes a short-lived SQLite connection
(see ``store.open_db``) and returns plain JSON-able data."""

import sqlite3
import statistics
import time

from .. import bankroll

MARKET_FIELDS = ("id", "event_id", "series", "category", "title", "yes_label", "no_label", "market_type",
                 "start_ts", "close_ts", "fee_coef", "yes_bid", "yes_ask")
DURATION_BUCKETS = [(0, 5, "<5s"), (5, 15, "5-15s"), (15, 60, "15-60s"), (60, 300, "1-5m"),
                    (300, 1800, "5-30m"), (1800, float("inf"), "30m+")]
EDGE_BUCKETS = [(0.0, 0.005, "0-0.5c"), (0.005, 0.01, "0.5-1c"), (0.01, 0.02, "1-2c"),
                (0.02, 0.05, "2-5c"), (0.05, float("inf"), "5c+")]


def _market(row: sqlite3.Row | None, rules: bool = False) -> dict | None:
    if row is None:
        return None
    out = {k: row[k] for k in MARKET_FIELDS}
    if rules:
        out["rules"] = row["rules"]
    return out


def markets(db: sqlite3.Connection, venue: str, ids: list[str], rules: bool = False) -> dict[str, dict]:
    cols = ", ".join(MARKET_FIELDS + (("rules",) if rules else ()))
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = f"SELECT {cols} FROM markets WHERE venue = ? AND id IN ({','.join('?' * len(chunk))})"
        for r in db.execute(q, (venue, *chunk)):
            out[r["id"]] = _market(r, rules)
    return out


def titles(db: sqlite3.Connection, pair_ids: list[str]) -> dict[str, dict]:
    ks = sorted({p.split("|", 1)[0] for p in pair_ids})
    ps = sorted({p.split("|", 1)[1] for p in pair_ids})
    km, pm = markets(db, "K", ks), markets(db, "P", ps)
    out = {}
    for pid in pair_ids:
        k, p = pid.split("|", 1)
        a, b = km.get(k) or {}, pm.get(p) or {}
        out[pid] = {"k_title": a.get("title") or k, "k_yes": a.get("yes_label"), "p_title": b.get("title") or p,
                    "p_yes": b.get("yes_label"), "p_no": b.get("no_label"), "category": a.get("category"),
                    "event_ts": a.get("start_ts") or b.get("start_ts")}
    return out


def pipeline(db: sqlite3.Connection, paired: set[tuple[str, str]], auto: int) -> dict:
    now = time.time()
    cat = {r[0]: {"count": r[1], "updated": r[2]}
           for r in db.execute("SELECT venue, COUNT(*), MAX(updated) FROM markets GROUP BY venue")}
    n_cand, created, confident = db.execute(
        "SELECT COUNT(*), MAX(created), COALESCE(SUM(confident), 0) FROM candidates").fetchone()
    decisions = dict(db.execute("SELECT decision, COUNT(*) FROM decisions GROUP BY decision").fetchall())
    jev = dict(db.execute(
        "SELECT decision = 'reject', COUNT(*) FROM decisions WHERE source = 'jev' GROUP BY 1").fetchall())
    jev_unsure = db.execute(
        "SELECT COUNT(*) FROM decisions d JOIN jev_reviews j ON j.kalshi = d.kalshi AND j.pm = d.pm "
        "WHERE d.source = 'jev' AND d.decision = 'reject' AND j.verdict = 'unsure'").fetchone()[0]
    pending = sum(
        1 for r in db.execute(
            "SELECT c.kalshi, c.pm FROM candidates c "
            "LEFT JOIN decisions d ON d.kalshi = c.kalshi AND d.pm = c.pm WHERE d.kalshi IS NULL")
        if (r[0], r[1]) not in paired
    )
    disc = {"hour": dict(db.execute("SELECT venue, COUNT(*) FROM discovered WHERE ts >= ? GROUP BY venue",
                                     (now - 3600,)).fetchall()),
            "day": dict(db.execute("SELECT venue, COUNT(*) FROM discovered WHERE ts >= ? GROUP BY venue",
                                    (now - 86400,)).fetchall()),
            "suggestions_day": db.execute("SELECT COALESCE(SUM(candidates), 0) FROM discovered WHERE ts >= ?",
                                          (now - 86400,)).fetchone()[0],
            "last": db.execute("SELECT MAX(ts) FROM discovered").fetchone()[0]}
    windows, profit, capital = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(max_profit), 0), COALESCE(SUM(cost_at_max), 0) FROM episodes "
        "WHERE start_ts >= ?", (now - 86400,)).fetchone()
    return {
        "catalog": {"kalshi": cat.get("K", {"count": 0, "updated": None}),
                    "pm": cat.get("P", {"count": 0, "updated": None})},
        "match": {"candidates": n_cand, "confident": confident, "updated": created},
        "discovery": disc,
        "review": {"pending": pending,
                   "approved": decisions.get("same", 0) + decisions.get("inverse", 0),
                   "rejected": decisions.get("reject", 0), "auto": auto,
                   "jev": {"approved": jev.get(0, 0), "rejected": jev.get(1, 0), "unsure": jev_unsure}},
        "report": {"windows_24h": windows, "profit_24h": profit, "capital_24h": capital},
    }


def _bucket(hours: float, points: int) -> float:
    return max(3.0, hours * 3600 / points)


def overview(db: sqlite3.Connection, hours: float, bankroll_usd: float = 0.0,
             rules: bankroll.PickRules | None = None) -> dict:
    """With ``rules``, picks are counted separately and ``bankroll_usd`` is simulated."""
    rules = rules or bankroll.PickRules()
    now = time.time()
    since = now - hours * 3600
    b = _bucket(hours, 360)
    rows = db.execute(
        "SELECT CAST((ts - ?) / ? AS INT) b, MIN(ts), MAX(best_edge), AVG(dur_ms), SUM(errors), COUNT(*), "
        "MAX(n_pairs) FROM sweeps WHERE ts >= ? GROUP BY b ORDER BY b", (since, b, since)).fetchall()
    edge = [[r[1], r[2]] for r in rows]
    latency = [[r[1], round(r[3] or 0)] for r in rows]
    sweeps = sum(r[5] for r in rows)
    errors = sum(r[4] or 0 for r in rows)
    best = db.execute(
        "SELECT best_edge, best_pair, best_dir, ts FROM sweeps WHERE ts >= ? AND best_edge IS NOT NULL "
        "ORDER BY best_edge DESC LIMIT 1", (since,)).fetchone()

    pb = 3600.0 if hours <= 48 else (6 * 3600.0 if hours <= 24 * 7 else 86400.0)
    start = since - (since % pb)
    n = int((now - start) // pb) + 1
    buckets = [{"ts": start + i * pb, "windows": 0, "picks": 0, "profit": 0.0, "capital": 0.0} for i in range(n)]
    eps = [dict(e) for e in db.execute(
        "SELECT start_ts, end_ts, max_profit, cost_at_max, max_top_edge, days_to_resolve "
        "FROM episodes WHERE start_ts >= ?", (since,))]
    picks = 0
    for e in eps:
        i = min(n - 1, int((e["start_ts"] - start) // pb))
        buckets[i]["windows"] += 1
        if rules.window_reason(e) is None:
            picks += 1
            buckets[i]["picks"] += 1
            buckets[i]["profit"] += e["max_profit"] or 0
            buckets[i]["capital"] += e["cost_at_max"] or 0
    durations = [e["end_ts"] - e["start_ts"] for e in eps]
    simulated = bankroll.simulate(eps, bankroll_usd, rules, now) if bankroll_usd else None
    return {
        "hours": hours, "since": since, "bucket_s": b, "profit_bucket_s": pb, "sim": simulated,
        "edge": edge, "latency": latency,
        "profit": buckets,
        "kpi": {
            "sweeps": sweeps, "errors": errors,
            "avg_sweep_ms": (round(sum(r[3] * r[5] for r in rows if r[3] is not None)
                                   / sum(r[5] for r in rows if r[3] is not None))
                             if any(r[3] is not None for r in rows) else None),
            "windows": len(eps), "picks": picks, "rules": rules.describe(),
            "profit": sum(e["max_profit"] or 0 for e in eps), "capital": sum(e["cost_at_max"] or 0 for e in eps),
            "median_duration": statistics.median(durations) if durations else None,
            "best_edge": best[0] if best else None, "best_pair": best[1] if best else None,
            "best_dir": best[2] if best else None, "best_ts": best[3] if best else None,
        },
    }


def pair_detail(db: sqlite3.Connection, pair: str, hours: float) -> dict:
    k, p = pair.split("|", 1)
    since = time.time() - hours * 3600
    prior = db.execute("SELECT ts, edge_a, edge_b FROM quotes WHERE pair = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                       (pair, since)).fetchone()
    rows = db.execute("SELECT ts, edge_a, edge_b FROM quotes WHERE pair = ? AND ts >= ? ORDER BY ts",
                      (pair, since)).fetchall()
    history = ([[since, prior[1], prior[2]]] if prior else []) + [[r[0], r[1], r[2]] for r in rows]
    if len(history) > 1500:  # keep the chart light; every change is still in the DB
        step = len(history) / 1500
        history = [history[int(i * step)] for i in range(1500)] + [history[-1]]
    eps = [dict(r) for r in db.execute(
        "SELECT direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, cost_at_max, "
        "days_to_resolve FROM episodes WHERE pair = ? ORDER BY start_ts DESC LIMIT 50", (pair,))]
    return {
        "pair": pair,
        "kalshi": markets(db, "K", [k], rules=True).get(k),
        "pm": markets(db, "P", [p], rules=True).get(p),
        "history": history, "since": since, "episodes": eps,
    }


def opportunities(db: sqlite3.Connection, hours: float, rules: bankroll.PickRules | None = None,
                  picks_only: bool = False) -> dict:
    """Windows in the last ``hours``, newest first, each with its return per year
    (``rate``) and why it isn't a pick (``why``, None for picks)."""
    rules = rules or bankroll.PickRules()
    since = time.time() - hours * 3600
    eps = [dict(r) for r in db.execute(
        "SELECT pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, cost_at_max, "
        "days_to_resolve FROM episodes WHERE start_ts >= ? ORDER BY start_ts DESC", (since,))]
    for e in eps:
        e["rate"] = bankroll.window_rate(e)
        e["why"] = rules.window_reason(e)
    n_all, n_picks = len(eps), sum(e["why"] is None for e in eps)
    if picks_only:
        eps = [e for e in eps if e["why"] is None]
    names = titles(db, sorted({e["pair"] for e in eps[:500]}))
    for e in eps[:500]:
        for key, value in names.get(e["pair"], {}).items():
            e.setdefault(key, value)  # never overwrite the episode's own fields
    durations = [e["end_ts"] - e["start_ts"] for e in eps]
    edges = [e["max_top_edge"] or 0 for e in eps]
    return {
        "hours": hours, "rules": rules.describe(), "picks_only": picks_only,
        "episodes": eps[:500], "total": len(eps), "counts": {"all": n_all, "picks": n_picks},
        "durations": [{"label": lab, "count": sum(lo <= d < hi for d in durations)} for lo, hi, lab in DURATION_BUCKETS],
        "edges": [{"label": lab, "count": sum(lo <= x < hi for x in edges),
                   "profit": sum(e["max_profit"] or 0 for e in eps if lo <= (e["max_top_edge"] or 0) < hi)}
                  for lo, hi, lab in EDGE_BUCKETS],
    }


def paper(db: sqlite3.Connection, hours: float) -> dict:
    """Paper trades in the last ``hours`` (newest first) and all-time totals."""
    since = time.time() - hours * 3600
    rows = [dict(r) for r in db.execute("SELECT * FROM paper_trades WHERE ts >= ? ORDER BY ts DESC", (since,))]
    names = titles(db, sorted({r["pair"] for r in rows[:300]}))
    for r in rows[:300]:
        for key, value in names.get(r["pair"], {}).items():
            r.setdefault(key, value)
    everything = [dict(r) for r in db.execute(
        "SELECT ts, status, planned_size, planned_profit, k_qty, p_qty, k_hold, p_hold, k_fees, p_fees, "
        "unwind_loss, locked_profit, pnl FROM paper_trades ORDER BY ts")]
    traded = [r for r in everything if r["status"] != "missed"]
    curve, total = [], 0.0
    for r in traded:  # settled trades at their result, open ones at the profit they locked in
        total += r["pnl"] if r["status"] == "settled" else (r["locked_profit"] or 0.0)
        curve.append([r["ts"], total])
    planned = sum(r["planned_size"] or 0 for r in everything)
    filled = sum(min(r["k_qty"] or 0, r["p_qty"] or 0) for r in everything)
    return {
        "hours": hours, "since": since, "trades": rows[:300], "total": len(rows), "curve": curve,
        "totals": {
            "sent": len(everything), "missed": len(everything) - len(traded),
            "unwound": sum(1 for r in traded if (r["unwind_loss"] or 0) > 0),
            "fill_rate": filled / planned if planned else None,
            "planned_profit": sum(r["planned_profit"] or 0 for r in traded),
            "fees": sum((r["k_fees"] or 0) + (r["p_fees"] or 0) for r in traded),
            "unwind_loss": sum(r["unwind_loss"] or 0 for r in traded),
            "pnl": total,
        },
    }
