"""Live discovery: the resident index, the polling step, and the process supervisor."""

import asyncio
import sys
import time
from datetime import datetime, timezone

import pytest

from arbscan import discover, jev, match
from arbscan.config import AutoApproveRule, Config
from arbscan.jobs import Daemon
from arbscan.pairs import load_pairs
from arbscan.store import connect

from test_match import START, build_catalog

CIN = "KXMLBGAME-26SEP241915CINATL-CIN"
ATL = "KXMLBGAME-26SEP241915CINATL-ATL"
MONEYLINE = "aec-mlb-cin-atl-2026-09-24"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(db, venue, mid) -> dict:
    r = db.execute("SELECT * FROM markets WHERE venue = ? AND id = ?", (venue, mid)).fetchone()
    return dict(r)


def test_live_index_matches_new_markets_both_ways(tmp_path):
    path = str(tmp_path / "c.db")
    build_catalog(path)
    db = connect(path)
    pm_row, k_row = _row(db, "P", MONEYLINE), _row(db, "K", CIN)
    db.execute("DELETE FROM markets WHERE (venue = 'P' AND id = ?) OR (venue = 'K' AND id = ?)", (MONEYLINE, CIN))
    idx = match.LiveIndex(db)

    # A newly listed Polymarket moneyline finds the Kalshi team market still indexed.
    got = {(c.kalshi, c.relation) for c in idx.add_pm(pm_row, 0.3)}
    assert got == {(ATL, "inverse")}
    # Then the Kalshi market listed later finds the Polymarket market added above.
    got = {(c.pm, c.relation) for c in idx.add_kalshi(k_row, 0.3)}
    assert (MONEYLINE, "same") in got
    # Words the index has never seen don't break scoring, and re-adding is a no-op.
    new = dict(pm_row, id="aec-mlb-zzz-yyy-2026-09-24", title="Zorblax vs Quuxington")
    idx.add_pm(new, 0.3)
    assert idx.add_pm(new, 0.3) == [] and idx.add_kalshi(k_row, 0.3) == []


class FakeKalshiVenue:
    def __init__(self):
        self.markets: list[dict] = []
        self.since: list[int] = []

    async def created_markets(self, since_ts):
        self.since.append(since_ts)
        return [m for m in self.markets if datetime.fromisoformat(m["created_time"].replace("Z", "+00:00")).timestamp()
                >= since_ts]

    async def event(self, ticker):
        return {"event_ticker": ticker, "series_ticker": "KXMLBGAME", "category": "Sports",
                "title": "Cincinnati vs Atlanta", "sub_title": "CIN vs ATL (Sep 24)"}

    async def series(self, s):
        return {"fee_type": "quadratic", "fee_multiplier": 1}


class FakePMVenue:
    def __init__(self):
        self.markets: list[dict] = []

    async def listed_since(self, start_min):
        return [m for m in self.markets if m["startDate"] >= start_min]


def _kalshi_market(ticker, created):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": "active", "created_time": _iso(created),
            "title": "Cincinnati wins", "yes_sub_title": "Cincinnati", "no_sub_title": "Cincinnati",
            "rules_primary": "If Cincinnati wins the Cincinnati vs Atlanta professional baseball game, then the "
                             "market resolves to Yes.",
            "expected_expiration_time": _iso(START + 3 * 3600)}


def _discovery(tmp_path, **cfg):
    path = str(tmp_path / "d.db")
    build_catalog(path)
    db = connect(path)
    db.execute("DELETE FROM markets WHERE venue = 'K' AND id = ?", (CIN,))
    db.commit()
    c = Config(db_path=path, pairs_path=str(tmp_path / "pairs.csv"), match_min_score=0.3, **cfg)
    d = discover.Discovery(c, db, FakeKalshiVenue(), FakePMVenue())
    d.build()
    return d, db


def test_step_adds_matches_and_approves_new_kalshi_market(tmp_path):
    rule = AutoApproveRule(kalshi_series="KXMLBGAME", pm_slug_prefix="aec-mlb-", min_score=0.3)
    d, db = _discovery(tmp_path, auto_approve=(rule,))
    d.kalshi.markets = [_kalshi_market(CIN, time.time() - 5)]
    asyncio.run(d.step("K"))

    assert db.execute("SELECT COUNT(*) FROM markets WHERE venue = 'K' AND id = ?", (CIN,)).fetchone()[0] == 1
    assert db.execute("SELECT relation FROM candidates WHERE kalshi = ? AND pm = ?", (CIN, MONEYLINE)).fetchone()[0] == "same"
    assert db.execute("SELECT candidates FROM discovered WHERE id = ?", (CIN,)).fetchone()[0] >= 1
    assert [(p.kalshi, p.pm) for p in load_pairs(d.cfg.pairs_path)] == [(CIN, MONEYLINE)]

    # The next poll asks from the newest listing (minus a small overlap) and skips it.
    asyncio.run(d.step("K"))
    assert d.kalshi.since[-1] >= int(time.time()) - discover.OVERLAP_S - 10
    assert db.execute("SELECT COUNT(*) FROM discovered").fetchone()[0] == 1


def test_step_sends_only_new_suggestions_to_jev(tmp_path, monkeypatch):
    d, db = _discovery(tmp_path, jev_api_key="k")
    calls = []

    async def fake_review(cfg, db, only=None, **kw):
        calls.append(only)
        return {"approve": 1}

    monkeypatch.setattr(jev, "review", fake_review)
    d.kalshi.markets = [_kalshi_market(CIN, time.time() - 5)]
    asyncio.run(d.step("K"))
    assert calls and (CIN, MONEYLINE) in calls[0] and all(k == CIN for k, _ in calls[0])
    assert d.window["approved"] == 1


def test_rebuilds_after_catalog_refresh_settles(tmp_path):
    d, db = _discovery(tmp_path)
    first = d.index
    d.maybe_rebuild()
    assert d.index is first  # nothing changed
    db.execute("UPDATE markets SET updated = ?", (time.time() + 100,))  # a refresh rewrote the catalog
    db.commit()
    d.maybe_rebuild()
    assert d.index is first  # wait one more check in case it's still writing
    d.maybe_rebuild()
    assert d.index is not first


def test_daemon_restarts_and_stops():
    lines = []
    d = Daemon(None, "discover", "discover", lambda text, stage: lines.append((stage, text)), lambda *a: None)
    d.MIN_BACKOFF_S = 0.05
    d.argv = lambda: [sys.executable, "-c", "print('2026-09-25 01:00:00,000 INFO arbscan.discover: hello', flush=True)"]

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(d.run(stop))
        while d.restarts < 2:
            await asyncio.sleep(0.02)
        d.argv = lambda: [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"]
        while not any(t == "up" for _, t in lines):
            await asyncio.sleep(0.02)
        t0 = time.monotonic()
        stop.set()
        await task
        return time.monotonic() - t0

    took = asyncio.run(asyncio.wait_for(main(), 30))
    assert took < 5 and d.state == "stopped"
    assert ("discover", "hello") in lines
    assert any("restarting" in t for _, t in lines)
