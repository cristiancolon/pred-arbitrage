import json
import time

import pytest

from arbscan import rescale
from arbscan.config import Config
from arbscan.store import connect


def test_rescale_resizes_history_once(tmp_path):
    cfg = Config(db_path=str(tmp_path / "r.db"), bankroll_usd=500)
    db = connect(cfg.db_path)
    t = time.time()
    books = (json.dumps([[0.40, 10000]]), json.dumps([[0.50, 10000]]))
    # Recorded with no bankroll: 10,000 contracts, $9,000 of capital.
    db.execute("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (t, "K-1|p-1", "K:YES+P:NO", 0.07, 10000, 9000.0, 1000.0, 0.07, 1.0, *books))
    db.execute("INSERT INTO episodes (pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, "
               "first_profit, cost_at_max, days_to_resolve) VALUES ('K-1|p-1', 'K:YES+P:NO', ?, ?, 1, 0.07, 1000, "
               "10000, 1000, 9000, 1)", (t, t + 5))
    db.commit()

    assert rescale.needed(cfg, db)
    rescale.ensure(cfg, db)
    opp = db.execute("SELECT size, cost FROM opportunities").fetchone()
    ep = db.execute("SELECT max_size, cost_at_max, max_profit FROM episodes").fetchone()
    # $250 per venue: the Kalshi leg costs 40c + 1.68c fee, Polymarket 50c + 1.74c.
    assert opp["size"] == ep["max_size"] == 483 and ep["cost_at_max"] < 500
    assert ep["max_profit"] == pytest.approx(483 * (1 - 0.9 - 0.07 * 0.24 - 0.0695 * 0.25))
    assert not rescale.needed(cfg, db)


def test_fresh_database_needs_no_rescale(tmp_path):
    cfg = Config(db_path=str(tmp_path / "f.db"), bankroll_usd=500)
    db = connect(cfg.db_path)
    assert not rescale.needed(cfg, db)
    assert db.execute("SELECT value FROM settings WHERE key = 'bankroll_usd'").fetchone()[0] == "500"
