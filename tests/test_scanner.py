import asyncio

import pytest

from arbscan.config import Config
from arbscan.pairs import append_pair, load_pairs
from arbscan.scanner import Scanner
from arbscan.store import connect


class FakeApi:
    errors = 0


class FakeKalshi:
    api = FakeApi()

    def __init__(self, book):
        self.book = book

    async def markets(self, tickers):
        return {t: {"ticker": t, "event_ticker": "EV-1", "status": "active",
                    "expected_expiration_time": "2099-01-01T00:00:00Z"} for t in tickers}

    async def event(self, ev):
        return {"series_ticker": "SER"}

    async def series(self, s):
        return {"fee_type": "quadratic", "fee_multiplier": 1}

    async def orderbooks(self, tickers):
        return {t: self.book for t in tickers}


class FakePM:
    api = FakeApi()

    def __init__(self, bids, offers):
        self.bids, self.offers = bids, offers
        self.book_calls = 0

    def _q(self, levels):
        return {"value": f"{levels[0][0]:.4f}"} if levels else None

    async def markets(self, slugs):
        return {s: {"slug": s, "status": "MARKET_STATUS_OPEN", "closed": False, "feeCoefficient": 0.0695,
                    "bestBidQuote": self._q(self.bids), "bestAskQuote": self._q(self.offers)} for s in slugs}

    async def book(self, slug):
        self.book_calls += 1
        lv = lambda levels: [{"px": {"value": str(p)}, "qty": str(q)} for p, q in levels]
        return {"bids": lv(self.bids), "offers": lv(self.offers)}


def _scanner(tmp_path, kbook, bids, offers):
    pairs = tmp_path / "pairs.csv"
    append_pair(str(pairs), "K-1", "p-1", "same")
    cfg = Config(db_path=str(tmp_path / "s.db"), pairs_path=str(pairs))
    db = connect(cfg.db_path)
    return Scanner(cfg, db, FakeKalshi(kbook), FakePM(bids, offers)), db


def test_sweep_records_opportunity_and_episode(tmp_path):
    # Kalshi YES ask 0.40 (from a NO bid at 0.60); Polymarket NO costs 1 - 0.50 bid.
    kbook = {"yes_dollars": [["0.3500", "100"]], "no_dollars": [["0.6000", "20"]]}
    sc, db = _scanner(tmp_path, kbook, bids=[(0.50, 15)], offers=[(0.52, 50)])
    asyncio.run(sc.sweep())

    opp = db.execute("SELECT * FROM opportunities").fetchall()
    assert len(opp) == 1
    assert opp[0]["direction"] == "K:YES+P:NO"
    assert opp[0]["size"] == 15  # limited by Polymarket bid depth
    assert opp[0]["profit"] == pytest.approx(15 * (1 - 0.9 - 0.07 * 0.24 - 0.0695 * 0.25))
    assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    assert sc.pm.book_calls == 1

    # Same prices again: quote row is deduplicated, episode stays open.
    asyncio.run(sc.sweep())
    assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0

    # Gap closes: the episode is written.
    sc.pm.bids = [(0.30, 15)]
    asyncio.run(sc.sweep())
    ep = db.execute("SELECT * FROM episodes").fetchall()
    assert len(ep) == 1 and ep[0]["n_obs"] == 2 and ep[0]["max_size"] == 15
    assert db.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0] == 3


def test_sweep_skips_depth_when_no_edge(tmp_path):
    kbook = {"yes_dollars": [["0.4000", "100"]], "no_dollars": [["0.5500", "100"]]}
    sc, db = _scanner(tmp_path, kbook, bids=[(0.44, 10)], offers=[(0.46, 10)])
    asyncio.run(sc.sweep())
    assert sc.pm.book_calls == 0
    assert db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 0


def test_pairs_file_validation(tmp_path):
    p = tmp_path / "pairs.csv"
    p.write_text(
        "kalshi_ticker,pm_slug,relation,added,note\n"
        "# a comment\n"
        "K-1,p-1,same,,ok\n"
        "K-2,p-2,sideways,,bad relation\n"
        ",p-3,same,,missing ticker\n"
        "K-1,p-1,inverse,,duplicate: last wins\n"
    )
    pairs = load_pairs(str(p))
    assert [(x.kalshi, x.pm, x.relation) for x in pairs] == [("K-1", "p-1", "inverse")]


def test_finished_pairs_stop_polling(tmp_path):
    kbook = {"yes_dollars": [["0.4000", "100"]], "no_dollars": [["0.5500", "100"]]}
    sc, db = _scanner(tmp_path, kbook, bids=[(0.44, 10)], offers=[(0.46, 10)])
    requested = []
    orig = sc.pm.markets

    async def markets(slugs):
        requested.append(list(slugs))
        out = await orig(slugs)
        for m in out.values():
            m["status"] = "MARKET_STATUS_RESOLVED"
        return out

    sc.pm.markets = markets
    asyncio.run(sc.sweep())
    assert sc.finished == {"K-1|p-1"}
    asyncio.run(sc.sweep())  # nothing left to poll
    assert requested == [["p-1"]]


def test_remove_pairs(tmp_path):
    from arbscan.pairs import remove_pairs

    p = tmp_path / "pairs.csv"
    append_pair(str(p), "K-1", "p-1", "same")
    append_pair(str(p), "K-2", "p-2", "inverse")
    with open(p, "a") as f:
        f.write("# keep me\n")
    assert remove_pairs(str(p), {"K-1|p-1"}) == 1
    assert [x.id for x in load_pairs(str(p))] == ["K-2|p-2"]
    assert "# keep me" in p.read_text()
