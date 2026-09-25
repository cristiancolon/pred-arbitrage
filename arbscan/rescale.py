"""Re-size recorded opportunities for the configured bankroll.

Every opportunity row keeps both order books (top levels), so when ``bankroll_usd``
changes, each can be walked again with the new per-venue budget, and each episode's
peak profit, size and capital recomputed from its opportunities. This runs once,
before the scanner starts, whenever the stored bankroll differs from the config.
"""

import json
import logging
import sqlite3
import time
from collections import defaultdict

from .arb import Leg, walk
from .config import Config
from .scanner import PMUS_DEFAULT_COEF

log = logging.getLogger(__name__)

KALSHI_DEFAULT_COEF = 0.07


def _key(cfg: Config) -> str:
    return f"{cfg.bankroll_usd:g}"


def needed(cfg: Config, db: sqlite3.Connection) -> bool:
    row = db.execute("SELECT value FROM settings WHERE key = 'bankroll_usd'").fetchone()
    if row is None:  # a fresh database has nothing to rescale
        if not db.execute("SELECT 1 FROM opportunities LIMIT 1").fetchone():
            db.execute("INSERT OR REPLACE INTO settings VALUES ('bankroll_usd', ?)", (_key(cfg),))
            db.commit()
            return False
        return True
    return row[0] != _key(cfg)


def rescale(cfg: Config, db: sqlite3.Connection) -> None:
    t0 = time.monotonic()
    budget = cfg.leg_budget
    coef = {(v, i): c for v, i, c in db.execute("SELECT venue, id, fee_coef FROM markets WHERE fee_coef IS NOT NULL")}
    rebate = 1 - cfg.pmus_taker_rebate
    by_key: dict[tuple[str, str], list[tuple[float, int, float, float]]] = defaultdict(list)
    updates = []
    for rowid, ts, pair, direction, kb, pb in db.execute(
            "SELECT rowid, ts, pair, direction, k_book, p_book FROM opportunities ORDER BY ts"):
        k, p = pair.split("|", 1)
        res = walk(Leg([tuple(x) for x in json.loads(kb)], coef.get(("K", k), KALSHI_DEFAULT_COEF)),
                   Leg([tuple(x) for x in json.loads(pb)], coef.get(("P", p), PMUS_DEFAULT_COEF) * rebate),
                   cfg.min_edge, budget_a=budget, budget_b=budget)
        updates.append((res.size, res.cost, res.profit, res.last_edge, rowid))
        by_key[(pair, direction)].append((ts, res.size, res.cost, res.profit))
    db.executemany("UPDATE opportunities SET size = ?, cost = ?, profit = ?, last_edge = ? WHERE rowid = ?", updates)

    eps = []
    for rowid, pair, direction, start, end in db.execute(
            "SELECT rowid, pair, direction, start_ts, end_ts FROM episodes"):
        obs = [o for o in by_key.get((pair, direction), ()) if start - 1 <= o[0] <= end + 1 and o[3] > 0]
        if not obs:
            continue  # nothing recorded to re-walk (kept as it was)
        best = max(obs, key=lambda o: o[3])
        eps.append((best[3], best[1], best[2], max(o[1] for o in obs), obs[0][3], rowid))
    db.executemany("UPDATE episodes SET max_profit = ?, cost_at_max = ?, max_size = ?, first_profit = ? "
                   "WHERE rowid = ?", [(p, c, ms, fp, r) for p, _, c, ms, fp, r in eps])
    db.execute("INSERT OR REPLACE INTO settings VALUES ('bankroll_usd', ?)", (_key(cfg),))
    db.commit()
    log.info("re-sized %d opportunities and %d windows for a $%s bankroll in %.0fs",
             len(updates), len(eps), _key(cfg) if cfg.bankroll_usd else "unlimited", time.monotonic() - t0)


def ensure(cfg: Config, db: sqlite3.Connection) -> None:
    if needed(cfg, db):
        log.info("bankroll changed to $%s; re-sizing recorded opportunities", _key(cfg))
        rescale(cfg, db)
