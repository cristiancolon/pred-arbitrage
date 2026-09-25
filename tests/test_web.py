import time

import pytest
from starlette.testclient import TestClient

from arbscan.catalog import ROW_SQL
from arbscan.config import Config
from arbscan.jobs import RefreshJob
from arbscan.pairs import append_pair, load_pairs
from arbscan.scanner import Scanner
from arbscan.store import connect
from arbscan.web.app import Hub, Service, create_app

from test_scanner import FakeKalshi, FakePM

NOW = time.time()


def _market(venue, mid, title, yes, no, start):
    return (venue, mid, "EV", "SER", "Sports", title, yes, no, None, start, start + 3600, f"rules for {mid}",
            0.07, 0.4, 0.42, 1.0, NOW)


@pytest.fixture
def env(tmp_path):
    pairs = tmp_path / "pairs.csv"
    cfg = Config(db_path=str(tmp_path / "w.db"), pairs_path=str(pairs))
    db = connect(cfg.db_path)
    db.executemany(ROW_SQL, [
        _market("K", "K-1", "A vs B | A wins", "A", "A", NOW + 86400),
        _market("P", "p-1", "A vs B", "A (a)", "B (b)", NOW + 86400),
        _market("K", "K-2", "C vs D | C wins", "C", "C", NOW + 86400),
        _market("P", "p-2", "C vs D", "C (c)", "D (d)", NOW + 86400),
    ])
    db.execute("INSERT INTO candidates VALUES ('K-2', 'p-2', 0.9, 'same', 1, ?)", (NOW,))
    db.execute(
        "INSERT INTO episodes (pair, direction, start_ts, end_ts, n_obs, max_top_edge, max_profit, max_size, "
        "first_profit, cost_at_max, days_to_resolve) VALUES ('K-1|p-1', 'K:YES+P:NO', ?, ?, 3, 0.01, 1.5, 150, 1.5, 148, 1)",
        (NOW - 100, NOW - 91))
    db.commit()
    append_pair(str(pairs), "K-1", "p-1", "same")
    kbook = {"yes_dollars": [["0.4000", "100"]], "no_dollars": [["0.5500", "100"]]}
    scanner = Scanner(cfg, db, FakeKalshi(kbook), FakePM([(0.44, 10)], [(0.46, 10)]))
    hub = Hub()
    svc = Service(cfg, scanner, RefreshJob(None, 3600, None, hub.publish), hub)
    return cfg, svc


def test_state_and_pairs(env):
    cfg, svc = env
    svc.scanner.pairs.refresh()
    client = TestClient(create_app(svc))
    st = client.get("/api/state").json()
    assert st["scanner"]["pairs"]["total"] == 1
    assert st["job"]["state"] == "idle"
    pairs = client.get("/api/pairs").json()["pairs"]
    assert [p["id"] for p in pairs] == ["K-1|p-1"]
    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").headers["cache-control"] == "no-cache"


def test_opportunities_keep_episode_times(env):
    # Regression: merging market titles must not overwrite the episode's start time.
    _, svc = env
    client = TestClient(create_app(svc))
    data = client.get("/api/opportunities?hours=1").json()
    ep = data["episodes"][0]
    assert ep["start_ts"] == pytest.approx(NOW - 100)
    assert ep["k_title"] == "A vs B | A wins"
    assert [d["count"] for d in data["durations"]][1] == 1  # 9s -> "5-15s"


def test_review_decision_appends_pair(env):
    cfg, svc = env
    svc.scanner.pairs.refresh()
    client = TestClient(create_app(svc))
    cands = client.get("/api/candidates?min_score=0.5").json()
    assert cands["total"] == 1 and cands["items"][0]["k"]["rules"] == "rules for K-2"
    r = client.post("/api/candidates/decide", json={"kalshi": "K-2", "pm": "p-2", "decision": "same"})
    assert r.status_code == 200
    assert {p.id for p in load_pairs(cfg.pairs_path)} == {"K-1|p-1", "K-2|p-2"}
    assert client.get("/api/candidates?min_score=0.5").json()["total"] == 0
    assert client.post("/api/candidates/decide", json={"kalshi": "K-2"}).status_code == 400


def test_remove_pairs(env):
    cfg, svc = env
    client = TestClient(create_app(svc))
    assert client.post("/api/pairs/remove", json={"ids": ["K-1|p-1"]}).json() == {"removed": 1}
    assert load_pairs(cfg.pairs_path) == []


def test_token_gate(env):
    from dataclasses import replace

    cfg, svc = env
    svc.cfg = replace(cfg, web_token="s3cret")
    client = TestClient(create_app(svc))
    assert client.get("/api/state").status_code == 401
    assert client.get("/?token=s3cret").status_code == 200  # sets the cookie
    assert client.get("/api/state").status_code == 200
