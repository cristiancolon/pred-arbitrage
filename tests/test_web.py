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
    pairs = client.get("/api/pairs").json()["items"]
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
    # 1% resolving within a day is a pick: ~370% a year.
    assert data["counts"] == {"all": 1, "picks": 1} and ep["why"] is None and ep["rate"] == pytest.approx(3.7, rel=0.01)
    assert client.get("/api/opportunities?hours=1&view=all").json()["total"] == 1


def test_pairs_are_filtered_sorted_and_paged_on_the_server(env):
    cfg, svc = env
    append_pair(cfg.pairs_path, "K-2", "p-2", "inverse")
    append_pair(cfg.pairs_path, "K-3", "p-3", "same")
    svc.scanner.pairs.refresh()
    svc.scanner.pair_state.update({
        "K-1|p-1": {"status": "live", "edges": {"a": -0.02, "b": 0.01}},
        "K-2|p-2": {"status": "paused", "edges": {"a": -0.05, "b": None}},
    })
    svc.scanner.finished.add("K-3|p-3")
    client = TestClient(create_app(svc))

    def ids(**q):
        return [p["id"] for p in client.get("/api/pairs", params=q).json()["items"]]

    data = client.get("/api/pairs").json()
    assert data["counts"] == {"all": 3, "live": 1, "paused": 1, "finished": 1} and data["total"] == 3
    assert ids() == ["K-1|p-1", "K-2|p-2", "K-3|p-3"]  # best edge first; no edge last
    assert ids(dir="asc") == ["K-2|p-2", "K-1|p-1", "K-3|p-3"]
    assert ids(status="finished") == ["K-3|p-3"] and ids(relation="inverse") == ["K-2|p-2"]
    assert ids(q="P-2") == ["K-2|p-2"]  # tickers and titles, any case
    page = client.get("/api/pairs", params={"offset": 1, "limit": 1}).json()
    assert [p["id"] for p in page["items"]] == ["K-2|p-2"] and page["total"] == 3


def test_live_updates_carry_the_best_picks_first(env, monkeypatch):
    _, svc = env
    now = time.time()

    def ep(i, days, edge=0.01, age=60):
        return {"pair": f"K-{i}|p-{i}", "direction": "K:YES+P:NO", "start_ts": now - age, "edge": edge,
                "profit": 1.0, "cost": 100.0, "days": days}

    eps = [ep(i, days=30) for i in range(25)]  # resolve too late to be picks
    eps += [ep(100, days=2), ep(101, days=0.2), ep(102, days=0.2, edge=0.2), ep(103, days=0.2, age=0)]
    monkeypatch.setattr(svc.scanner.episodes, "snapshot", lambda: eps)
    st = svc.state()
    assert st["open_count"] == 29 and len(st["open"]) == 20 and st["open_picks"] == 2
    assert [e["pair"] for e in st["open"][:2]] == ["K-101|p-101", "K-100|p-100"]  # soonest first
    assert st["open"][2]["why"] is not None


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


def test_paper_endpoint(env):
    cfg, svc = env
    db = svc.scanner.db
    db.execute("INSERT INTO paper_trades (id, ts, pair, direction, k_side, p_side, planned_size, planned_profit, "
               "k_qty, p_qty, k_hold, p_hold, k_fees, p_fees, unwind_loss, k_out, p_out, locked_profit, status) "
               "VALUES ('a', ?, 'K-1|p-1', 'K:YES+P:NO', 'yes', 'no', 10, 0.5, 10, 8, 8, 8, 0.1, 0.1, 0.2, 4, 4, 0, "
               "'open')", (NOW - 60,))
    db.execute("INSERT INTO paper_trades (id, ts, pair, direction, k_side, p_side, planned_size, k_qty, p_qty, "
               "status) VALUES ('b', ?, 'K-1|p-1', 'K:YES+P:NO', 'yes', 'no', 10, 0, 0, 'missed')", (NOW - 30,))
    db.commit()
    client = TestClient(create_app(svc))
    data = client.get("/api/paper?hours=1").json()
    assert data["live"] is None  # the polling scanner doesn't paper trade
    assert [t["id"] for t in data["trades"]] == ["b", "a"] and data["trades"][1]["k_title"] == "A vs B | A wins"
    assert data["totals"]["sent"] == 2 and data["totals"]["missed"] == 1
    assert data["totals"]["fill_rate"] == pytest.approx(8 / 20)
    assert client.get("/api/state").json()["paper"] is None
