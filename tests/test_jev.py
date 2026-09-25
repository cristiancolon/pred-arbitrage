import asyncio
import time

import pytest
from starlette.testclient import TestClient

from arbscan import jev
from arbscan.catalog import ROW_SQL
from arbscan.config import Config
from arbscan.http import ApiError
from arbscan.jobs import RefreshJob
from arbscan.pairs import load_pairs
from arbscan.scanner import Scanner
from arbscan.store import connect
from arbscan.web import queries
from arbscan.web.app import Hub, Service, create_app

from test_scanner import FakeKalshi, FakePM

NOW = time.time()


def answers(yes=0.9, no=0.02, neither=0.08, scope=0.95, contradiction=0.05):
    return {"side": {"type": "choice", "probabilities": {"yes_side": yes, "no_side": no, "neither": neither}},
            "same_scope": {"type": "noul", "noul": scope}, "contradiction": {"type": "noul", "noul": contradiction}}


@pytest.mark.parametrize("kw, relation, verdict", [
    ({}, "same", "approve"),
    ({"yes": 0.02, "no": 0.9}, "inverse", "approve"),
    ({"yes": 0.1, "neither": 0.88}, "same", "reject"),
    ({"contradiction": 0.9}, "same", "unsure"),  # Polymarket rules disagree with its own label
    ({"scope": 0.4}, "same", "unsure"),  # e.g. conference leader vs national leader
    ({}, "inverse", "unsure"),  # Jev reads it the other way round
    ({"yes": 0.5, "neither": 0.48}, "same", "unsure"),
])
def test_judge(kw, relation, verdict):
    assert jev.judge(answers(**kw), relation).verdict == verdict


def test_request_body_drops_disclaimers_and_names_sides():
    k = {"title": "A vs B", "yes_label": "A", "rules": "If A wins, Yes.\nKalshi is not affiliated with the league."}
    p = {"title": "A vs B", "yes_label": "A (a)", "no_label": "B (b)", "rules": "Settles to the winner."}
    body = jev.request_body(k, p, "jev-1.13.0")
    assert body["state"]["kalshi_market"]["rules"] == "If A wins, Yes."
    assert '"A (a)"' in body["questions"]["side"]["criteria"]["yes_side"]
    assert body["model"] == "jev-1.13.0"


def _market(venue, mid, title, yes, no):
    return (venue, mid, "EV", "SER", "Sports", title, yes, no, None, NOW + 86400, NOW + 90000,
            f"rules for {mid}", 0.07, 0.4, 0.42, 1.0, NOW)


class FakeApi:
    """Answers by Polymarket title: 'same' approves, 'diff' rejects, anything else is unsure."""

    def __init__(self, status=None):
        self.calls = 0
        self.status = status

    async def post(self, path, body):
        self.calls += 1
        if self.status:
            raise ApiError("nope", self.status)
        title = body["state"]["polymarket_market"]["title"]
        a = {"same": answers(), "diff": answers(yes=0.05, neither=0.93)}.get(title, answers(yes=0.5, neither=0.48))
        return {"model": "jev-1.13.0", "answers": a, "usage": {"input_tokens": 900}}


@pytest.fixture
def env(tmp_path):
    cfg = Config(db_path=str(tmp_path / "j.db"), pairs_path=str(tmp_path / "pairs.csv"), jev_api_key="k")
    db = connect(cfg.db_path)
    db.executemany(ROW_SQL, [
        _market("K", "K-1", "A wins", "A", "A"), _market("P", "p-1", "same", "A", "B"),
        _market("K", "K-2", "C wins", "C", "C"), _market("P", "p-2", "diff", "D", "E"),
        _market("K", "K-3", "F wins", "F", "F"), _market("P", "p-3", "hmm", "F", "G"),
    ])
    db.executemany("INSERT INTO candidates VALUES (?, ?, 0.8, 'same', 1, ?)",
                   [("K-1", "p-1", NOW), ("K-2", "p-2", NOW), ("K-3", "p-3", NOW)])
    db.commit()
    return cfg, db


def test_review_decides_and_remembers(env):
    cfg, db = env
    api = FakeApi()
    stats = asyncio.run(jev.review(cfg, db, api=api))
    assert (stats["approve"], stats["reject"], stats["unsure"]) == (1, 1, 1)
    assert [(p.kalshi, p.pm, p.relation, p.note) for p in load_pairs(cfg.pairs_path)] == [("K-1", "p-1", "same", "jev:0.90")]
    # Unsure counts as a rejection; its reason is kept.
    rows = {r["kalshi"]: (r["decision"], r["source"]) for r in db.execute("SELECT * FROM decisions")}
    assert rows == {"K-1": ("same", "jev"), "K-2": ("reject", "jev"), "K-3": ("reject", "jev")}
    assert db.execute("SELECT verdict FROM jev_reviews WHERE kalshi = 'K-3'").fetchone()[0] == "unsure"
    assert asyncio.run(jev.review(cfg, db, api=api))["sent"] == 0 and api.calls == 3


def test_earlier_unsure_verdicts_become_rejections(env):
    # Pairs Jev left unsure before unsure meant reject get rejected without another call.
    cfg, db = env
    api = FakeApi()
    asyncio.run(jev.review(cfg, db, api=api))
    db.execute("DELETE FROM decisions WHERE kalshi = 'K-3'")
    db.commit()
    stats = asyncio.run(jev.review(cfg, db, api=api))
    assert (stats["sent"], stats["known"], api.calls) == (0, 1, 3)
    assert db.execute("SELECT decision, source FROM decisions WHERE kalshi = 'K-3'").fetchone()[:] == ("reject", "jev")

    # If its text has changed since, Jev reads it again instead.
    db.execute("DELETE FROM decisions WHERE kalshi = 'K-3'")
    db.execute("UPDATE markets SET rules = 'new rules' WHERE id = 'p-3'")
    db.commit()
    assert asyncio.run(jev.review(cfg, db, api=api))["sent"] == 1


def test_dry_run_records_nothing(env):
    cfg, db = env
    stats = asyncio.run(jev.review(cfg, db, dry_run=True, api=FakeApi()))
    assert stats["sent"] == 3
    assert db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM jev_reviews").fetchone()[0] == 0
    assert load_pairs(cfg.pairs_path) == []


def test_bad_key_stops_the_run(env):
    cfg, db = env
    api = FakeApi(status=401)
    with pytest.raises(SystemExit, match="API key"):
        asyncio.run(jev.review(cfg, db, api=api))
    assert api.calls < 3  # stops early instead of trying every pair


def test_pipeline_counts(env):
    cfg, db = env
    asyncio.run(jev.review(cfg, db, api=FakeApi()))
    hub = Hub()
    scanner = Scanner(cfg, db, FakeKalshi({"yes_dollars": [], "no_dollars": []}), FakePM([], []))
    scanner.pairs.refresh()
    svc = Service(cfg, scanner, RefreshJob(None, 3600, None, hub.publish), hub)
    rev = queries.pipeline(db, svc.paired(), 0)["review"]
    assert (rev["pending"], rev["approved"], rev["rejected"]) == (0, 1, 2)
    assert rev["jev"] == {"approved": 1, "rejected": 2, "unsure": 1}
    client = TestClient(create_app(svc))
    assert client.get("/api/state").json()["features"] == {"jev": True}
    assert client.get("/api/candidates").status_code == 404  # no review page any more
