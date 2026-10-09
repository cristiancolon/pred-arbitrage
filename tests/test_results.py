"""Settlement results: parsing both venues, what to look up when, the backtest views,
and windows that survive a restart."""

import asyncio
import time

import pytest

from arbscan import results
from arbscan.arb import ArbResult
from arbscan.scanner import Episodes
from arbscan.store import connect

NOW = time.time()


def test_parse_both_venues():
    k = results.kalshi_row({"ticker": "K-1", "status": "finalized", "result": "no", "close_time": "2026-09-25T02:00:00Z",
                            "settlement_ts": "2026-09-25T03:00:00Z"}, NOW)
    assert k[:5] == ("K", "K-1", "finalized", 0.0, "no") and k[6] is not None and k[7] == NOW
    assert results.kalshi_row({"ticker": "K-2", "status": "active", "result": ""}, NOW)[3] is None
    scalar = results.kalshi_row({"ticker": "K-3", "status": "settled", "result": "scalar",
                                 "settlement_value_dollars": "0.3500"}, NOW)
    assert scalar[3] == pytest.approx(0.35)
    p = results.pm_row({"slug": "p-1", "status": "MARKET_STATUS_RESOLVED", "outcomes": '["Chargers","Titans"]',
                        "outcomePrices": '["1","0"]', "endDate": "2025-11-03T18:00:00Z"}, NOW)
    assert p[:5] == ("P", "p-1", "MARKET_STATUS_RESOLVED", 1.0, "Chargers")
    assert results.pm_row({"slug": "p-2", "status": "MARKET_STATUS_OPEN"}, NOW)[3] is None
    life = results.kalshi_lifecycle_row({"event_type": "determined", "market_ticker": "K-4", "result": "yes",
                                         "determination_ts": 1790000000}, NOW)
    assert life[:5] == ("K", "K-4", "determined", 1.0, "yes") and life[6] == 1790000000
    assert results.kalshi_lifecycle_row({"event_type": "activated", "market_ticker": "K-4"}, NOW) is None


def _db(tmp_path):
    db = connect(str(tmp_path / "r.db"))
    db.execute("INSERT INTO decisions (kalshi, pm, decision, ts, source) VALUES ('K-1', 'p-1', 'same', 0, 'jev')")
    db.execute("INSERT INTO decisions (kalshi, pm, decision, ts, source) VALUES ('K-2', 'p-2', 'inverse', 0, 'jev')")
    db.execute("INSERT INTO episodes (pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, "
               "first_profit, cost_at_max, days_to_resolve) VALUES ('K-1|p-1', 'K:YES+P:NO', ?, ?, 3, 0.02, 2, 100, "
               "2, 98, 0.5)", (NOW - 100, NOW - 50))
    db.commit()
    return db


def test_what_to_look_up_when(tmp_path):
    db = _db(tmp_path)
    tracked = results.tracked_markets(db, ["K-9|p-9"])
    assert tracked == {("K", "K-1"), ("P", "p-1"), ("K", "K-2"), ("P", "p-2"), ("K", "K-9"), ("P", "p-9")}
    assert len(results.due(db, tracked, set(), NOW)) == 6  # never looked up
    db.executemany(results.UPSERT, [
        results._row("K", "K-1", "finalized", 1.0, "yes", NOW - 3600, None, NOW - 10, {}),  # final: done
        results._row("P", "p-1", "MARKET_STATUS_OPEN", None, None, NOW - 3600, None, NOW - 700, {}),  # closed
        results._row("K", "K-2", "active", None, None, NOW + 86400, None, NOW - 700, {}),  # still open
        results._row("P", "p-2", "MARKET_STATUS_OPEN", None, None, NOW + 86400, None, NOW - 700, {}),
    ])
    due = results.due(db, tracked, {("P", "p-2")}, NOW)  # the scanner saw p-2 finish
    assert set(due) == {("P", "p-1"), ("P", "p-2"), ("K", "K-9"), ("P", "p-9")}
    # A day later, K-1's result is read once more in case Kalshi corrected it.
    assert ("K", "K-1") in results.due(db, tracked, set(), NOW + 86400)


def _live_trade(db, tid, pair, status, settled_ts=None, hold=10):
    db.execute("INSERT INTO live_trades (id, ts, pair, direction, k_side, p_side, k_hold, p_hold, status, settled_ts) "
               "VALUES (?, ?, ?, 'K:YES+P:NO', 'yes', 'no', ?, ?, ?, ?)",
               (tid, NOW - 7200, pair, hold, hold, status, settled_ts))


def test_the_live_accounts_markets_are_read_until_paid_out(tmp_path):
    db = _db(tmp_path)
    # An open trade on a pair no longer tracked (unpaired while held); a trade settled
    # an hour ago; one settled long ago; and one sold back whole (nothing to be paid).
    _live_trade(db, "a", "K-5|p-5", "open")
    _live_trade(db, "b", "K-6|p-6", "settled", NOW - 3600)
    _live_trade(db, "c", "K-7|p-7", "settled", NOW - 3 * 86400)
    _live_trade(db, "d", "K-8|p-8", "settled", NOW - 60, hold=0)
    db.commit()
    assert results.live_markets(db, NOW) == {("K", "K-5"), ("P", "p-5"), ("K", "K-6"), ("P", "p-6")}
    tracked = results.tracked_markets(db)
    assert {("K", "K-5"), ("P", "p-5")} <= set(results.due(db, tracked, set(), NOW))  # never looked up
    db.executemany(results.UPSERT, [
        # Kalshi's close time is two weeks off and the scanner knows nothing of the pair.
        results._row("K", "K-5", "inactive", None, None, NOW + 14 * 86400, None, NOW - results.RECHECK_LIVE_S, {}),
        results._row("P", "p-5", "MARKET_STATUS_OPEN", None, None, NOW + 14 * 86400, None, NOW - 30, {}),
        # Determined but not yet paid out: read again; Polymarket's is paid out.
        results._row("K", "K-6", "determined", 1.0, "yes", NOW - 7200, None, NOW - results.RECHECK_LIVE_S, {}),
        results._row("P", "p-6", "MARKET_STATUS_RESOLVED", 0.0, "No", NOW - 7200, None, NOW - 600, {}),
    ])
    due = set(results.due(db, tracked, set(), NOW))
    assert ("K", "K-5") in due and ("P", "p-5") not in due  # each every RECHECK_LIVE_S
    assert ("K", "K-6") in due and ("P", "p-6") not in due
    db.executemany(results.UPSERT, [results._row("K", "K-6", "finalized", 1.0, "yes", NOW - 7200, None, NOW - 600, {})])
    assert ("K", "K-6") not in set(results.due(db, tracked, set(), NOW))  # paid out: once a day from now on
    assert results.outcomes(db, {("K", "K-5"), ("K", "K-6"), ("P", "p-6")}) == {
        ("K", "K-6"): (1.0, True), ("P", "p-6"): (0.0, True)}
    db.executemany(results.UPSERT, [results._row("K", "K-5", "determined", 0.0, "no", None, None, NOW, {})])
    assert results.outcomes(db, {("K", "K-5")}) == {("K", "K-5"): (0.0, False)}


def test_recorder_and_backtest_views(tmp_path):
    db = _db(tmp_path)

    class Venue:
        def __init__(self, markets):
            self.m = markets

        async def markets(self, ids):
            return {i: self.m[i] for i in ids if i in self.m}

    kalshi = Venue({"K-1": {"ticker": "K-1", "status": "finalized", "result": "yes"},
                    "K-2": {"ticker": "K-2", "status": "finalized", "result": "yes"}})
    pm = Venue({"p-1": {"slug": "p-1", "status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["1","0"]'},
                "p-2": {"slug": "p-2", "status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["1","0"]'}})

    async def run(fn):
        return fn(db)

    rec = results.ResultsRecorder(run, run, kalshi, pm)
    assert asyncio.run(rec.step()) == 4
    assert asyncio.run(rec.step()) == 0  # nothing left to look up

    # K-1/p-1 ("same") both said YES: the window bought Kalshi YES + Polymarket NO and got $1 a pair.
    w = dict(db.execute("SELECT * FROM window_outcomes").fetchone())
    assert w["payout_per_pair"] == 1.0 and w["realized_at_peak"] == pytest.approx(2.0)
    # K-2/p-2 was approved as inverse, but both said YES: not one bet.
    out = {r["kalshi"]: r["consistent"] for r in db.execute("SELECT * FROM pair_outcomes")}
    assert out == {"K-1": 1, "K-2": 0}
    assert results.lookup(db, {("K", "K-1"), ("P", "p-9")}) == {("K", "K-1"): 1.0}


def _res(profit=1.0):
    return ArbResult(0.01, 100, 99.0, profit, 0.01)


def test_windows_survive_a_quick_restart(tmp_path):
    db = connect(str(tmp_path / "e.db"))
    eps = Episodes(db)
    eps.observe(("A|a", "K:YES+P:NO"), NOW - 60, _res(1.0), 1.0)
    eps.observe(("B|b", "K:YES+P:NO"), NOW - 60, _res(1.0), 1.0)
    eps.close_all()  # the scanner stops
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM episodes WHERE cut = 1").fetchone()[0] == 2

    again = Episodes(db, reader=db)
    again.observe(("A|a", "K:YES+P:NO"), NOW, _res(3.0), 1.0)  # still open after the restart
    again.observe(("B|b", "K:YES+P:NO"), NOW, None, 1.0)  # closed while we were away
    again.close_all()
    db.commit()
    rows = {r["pair"]: dict(r) for r in db.execute("SELECT * FROM episodes")}
    assert len(rows) == 2
    assert rows["A|a"]["start_ts"] == pytest.approx(NOW - 60) and rows["A|a"]["max_profit"] == 3.0
    assert rows["A|a"]["n_obs"] == 2
    assert rows["B|b"]["end_ts"] == pytest.approx(NOW - 60)


def test_first_pass_covers_both_venues(tmp_path, monkeypatch):
    db = connect(str(tmp_path / "b.db"))
    for i in range(10):
        db.execute("INSERT INTO decisions (kalshi, pm, decision, ts, source) VALUES (?, ?, 'same', 0, 'jev')",
                   (f"K-{i}", f"p-{i}"))
    db.commit()
    asked = {"K": 0, "P": 0}

    class Venue:
        def __init__(self, v):
            self.v = v

        async def markets(self, ids):
            asked[self.v] += len(ids)
            return {}

    async def run(fn):
        return fn(db)

    monkeypatch.setattr(results, "BATCH", 8)
    asyncio.run(results.ResultsRecorder(run, run, Venue("K"), Venue("P")).step())
    assert asked == {"K": 4, "P": 4}


def test_settled_pairs_tell_voids_from_conflicts(tmp_path):
    path = str(tmp_path / "o.db")
    db = connect(path)
    rows = [  # (kalshi, pm, relation, Kalshi YES paid, Polymarket YES paid), from 2026-09-25..27
        ("KXMLBGAME-26SEP241915CINATL-CIN", "aec-mlb-cin-atl", "same", 1.0, 1.0),
        ("KXCS2GAME-26SEP250400ENCEWAL-WAL", "aec-cs2-wal-ence", "same", 0.52, 0.53),  # both voided the match
        ("KXKBOGAME-26SEP250400SAMSSG-SAM", "aec-kbo-sls-sla", "inverse", 0.6, 0.0),  # Kalshi voided, Polymarket didn't
        ("KXMLBGAME-26SEP251905BALNYY-BAL", "aec-mlb-bal-nyy", "same", 0.0, 1.0),  # game 2 paired with game 1
    ]
    for k, p, rel, kv, pv in rows:
        db.execute("INSERT INTO decisions (kalshi, pm, decision, ts, source) VALUES (?, ?, ?, 0, 'jev')", (k, p, rel))
        db.executemany(results.UPSERT, [results._row("K", k, "finalized", kv, None, None, None, NOW, {}),
                                        results._row("P", p, results.PM_FINAL, pv, None, None, None, NOW, {})])
    db.commit()
    got = [(r["outcome"], round(r["miss"], 2), r["consistent"])
           for r in db.execute("SELECT * FROM pair_outcomes ORDER BY kalshi")]
    assert got == [("void", 0.01, 0), ("void", 0.4, 0), ("one bet", 0.0, 1), ("conflict", 1.0, 0)]

    # A database made with the old view gets the new one on its next connect.
    db.execute("DROP VIEW pair_outcomes")
    db.execute("CREATE VIEW pair_outcomes AS SELECT kalshi FROM decisions")
    db.commit()
    db.close()
    assert "outcome" in {c[0] for c in connect(path).execute("SELECT * FROM pair_outcomes").description}
