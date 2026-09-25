"""Read-side queries for the dashboard. Each takes a short-lived SQLite connection
(see ``store.open_db``) and returns plain JSON-able data."""

import json
import sqlite3
import statistics
import time

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
    pending = [
        r[2] for r in db.execute(
            "SELECT c.kalshi, c.pm, j.verdict FROM candidates c "
            "LEFT JOIN decisions d ON d.kalshi = c.kalshi AND d.pm = c.pm "
            "LEFT JOIN jev_reviews j ON j.kalshi = c.kalshi AND j.pm = c.pm "
            "WHERE d.kalshi IS NULL")
        if (r[0], r[1]) not in paired
    ]
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
        "review": {"pending": len(pending),
                   "approved": decisions.get("same", 0) + decisions.get("inverse", 0),
                   "rejected": decisions.get("reject", 0), "auto": auto,
                   "jev": {"approved": jev.get(0, 0), "rejected": jev.get(1, 0),
                           "unsure": pending.count("unsure"), "unreviewed": pending.count(None)}},
        "report": {"windows_24h": windows, "profit_24h": profit, "capital_24h": capital},
    }


def _bucket(hours: float, points: int) -> float:
    return max(3.0, hours * 3600 / points)


def overview(db: sqlite3.Connection, hours: float) -> dict:
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
    buckets = [{"ts": start + i * pb, "windows": 0, "profit": 0.0, "capital": 0.0} for i in range(n)]
    eps = db.execute("SELECT start_ts, end_ts, max_profit, cost_at_max, max_top_edge FROM episodes "
                     "WHERE start_ts >= ?", (since,)).fetchall()
    for e in eps:
        i = min(n - 1, int((e[0] - start) // pb))
        buckets[i]["windows"] += 1
        buckets[i]["profit"] += e[2] or 0
        buckets[i]["capital"] += e[3] or 0
    durations = [e[1] - e[0] for e in eps]
    return {
        "hours": hours, "since": since, "bucket_s": b, "profit_bucket_s": pb,
        "edge": edge, "latency": latency,
        "profit": buckets,
        "kpi": {
            "sweeps": sweeps, "errors": errors,
            "avg_sweep_ms": (round(sum(r[3] * r[5] for r in rows if r[3] is not None)
                                   / sum(r[5] for r in rows if r[3] is not None))
                             if any(r[3] is not None for r in rows) else None),
            "windows": len(eps), "profit": sum(e[2] or 0 for e in eps), "capital": sum(e[3] or 0 for e in eps),
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


def _jev(row: sqlite3.Row) -> dict | None:
    if row["verdict"] is None:
        return None
    return {"verdict": row["verdict"], "reason": row["reason"], "model": row["model"], "ts": row["jts"],
            "answers": json.loads(row["answers"]) if row["answers"] else None}


def candidates(db: sqlite3.Connection, paired: set[tuple[str, str]], min_score: float, confident: bool,
               relation: str | None, q: str | None, offset: int, limit: int, view: str = "pending") -> dict:
    """view: 'pending' (undecided), 'unsure' (Jev couldn't decide), 'unreviewed' (Jev
    hasn't read it yet) or 'rejected' (Jev rejected it; approving overrides that)."""
    sql = ("SELECT c.kalshi, c.pm, c.score, c.relation, c.confident, "
           "j.verdict, j.reason, j.answers, j.model, j.ts AS jts FROM candidates c "
           "LEFT JOIN decisions d ON d.kalshi = c.kalshi AND d.pm = c.pm "
           "LEFT JOIN jev_reviews j ON j.kalshi = c.kalshi AND j.pm = c.pm "
           "JOIN markets k ON k.venue = 'K' AND k.id = c.kalshi "
           "JOIN markets p ON p.venue = 'P' AND p.id = c.pm "
           "WHERE c.score >= ?")
    args: list = [min_score]
    if view == "rejected":
        sql += " AND d.source = 'jev' AND d.decision = 'reject'"
    else:
        sql += " AND d.kalshi IS NULL"
        if view == "unsure":
            sql += " AND j.verdict = 'unsure'"
        elif view == "unreviewed":
            sql += " AND j.kalshi IS NULL"
    if confident:
        sql += " AND c.confident = 1"
    if relation in ("same", "inverse"):
        sql += " AND c.relation = ?"
        args.append(relation)
    if q:
        sql += " AND (k.title LIKE ? OR p.title LIKE ? OR c.kalshi LIKE ? OR c.pm LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += " ORDER BY c.confident DESC, c.score DESC"
    rows = [r for r in db.execute(sql, args) if (r[0], r[1]) not in paired]
    page = rows[offset : offset + limit]
    km = markets(db, "K", [r[0] for r in page], rules=True)
    pm = markets(db, "P", [r[1] for r in page], rules=True)
    items = [{"kalshi": r[0], "pm": r[1], "score": r[2], "relation": r[3], "confident": bool(r[4]),
              "jev": _jev(r), "k": km.get(r[0]), "p": pm.get(r[1])} for r in page]
    return {"total": len(rows), "offset": offset, "items": items}


def opportunities(db: sqlite3.Connection, hours: float) -> dict:
    since = time.time() - hours * 3600
    eps = [dict(r) for r in db.execute(
        "SELECT pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, cost_at_max, "
        "days_to_resolve FROM episodes WHERE start_ts >= ? ORDER BY start_ts DESC", (since,))]
    names = titles(db, sorted({e["pair"] for e in eps}))
    for e in eps:
        for key, value in names.get(e["pair"], {}).items():
            e.setdefault(key, value)  # never overwrite the episode's own fields
    durations = [e["end_ts"] - e["start_ts"] for e in eps]
    edges = [e["max_top_edge"] or 0 for e in eps]
    return {
        "hours": hours,
        "episodes": eps[:500], "total": len(eps),
        "durations": [{"label": lab, "count": sum(lo <= d < hi for d in durations)} for lo, hi, lab in DURATION_BUCKETS],
        "edges": [{"label": lab, "count": sum(lo <= x < hi for x in edges),
                   "profit": sum(e["max_profit"] or 0 for e in eps if lo <= (e["max_top_edge"] or 0) < hi)}
                  for lo, hi, lab in EDGE_BUCKETS],
    }
