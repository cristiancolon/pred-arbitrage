"""Storage cap: raw history older than the window is deleted in short batches, the size
cap trims further but never the last day, and the pruning thread leaves nothing behind."""

import asyncio
import sqlite3
import threading
import time

import pytest

from arbscan import retention
from arbscan.store import connect

NOW = float(int(time.time()))
DAY = 86400.0


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(retention, "MIN_REST_S", 0.0)
    monkeypatch.setattr(retention, "REST", 0.0)
    monkeypatch.setattr(retention, "BATCH_ROWS", 50)


def _db(tmp_path, days=10, per_hour=20):
    """Raw rows every 3 minutes for ``days`` up to NOW, plus rows that are always kept."""
    path = str(tmp_path / "a.db")
    db = connect(path)
    start = NOW - days * DAY
    ts = [start + i * 3600 / per_hour for i in range(int(days * 24 * per_hour))]
    db.executemany("INSERT INTO quotes (ts, pair, edge_a) VALUES (?, 'K|P', 0.01)", [(t,) for t in ts])
    db.executemany("INSERT INTO opportunities (ts, pair, direction, k_book, p_book) VALUES (?, 'K|P', 'a', ?, ?)",
                   [(t, "x" * 200, "y" * 200) for t in ts[::4]])
    db.executemany("INSERT INTO sweeps (ts, n_pairs) VALUES (?, 1)", [(t,) for t in ts[::2]])
    db.execute("INSERT INTO episodes (pair, direction, start_ts, end_ts, n_obs) VALUES ('K|P', 'a', ?, ?, 1)",
               (start, start + 1))
    db.execute("INSERT INTO paper_trades (id, ts, pair, direction, k_side, p_side, status) VALUES ('t1', ?, 'K|P', 'a', 'yes', 'no', 'settled')",
               (start,))
    db.commit()
    return path, db


def _span(db, table):
    return (db.execute(f"SELECT MIN(ts) FROM {table}").fetchone()[0], db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_boundary_finds_the_first_row_at_or_after(tmp_path):
    _, db = _db(tmp_path, days=1)
    rows = db.execute("SELECT rowid, ts FROM quotes ORDER BY rowid").fetchall()
    for i in (0, 1, 7, len(rows) - 1):
        assert retention.boundary(db, "quotes", rows[i][1]) == rows[i][0]
        assert retention.boundary(db, "quotes", rows[i][1] - 0.5) == rows[i][0]
    assert retention.boundary(db, "quotes", NOW + 1) == rows[-1][0] + 1
    db.execute("DELETE FROM quotes WHERE rowid % 3 = 0")  # gaps in rowid
    rows = db.execute("SELECT rowid, ts FROM quotes ORDER BY rowid").fetchall()
    b = retention.boundary(db, "quotes", rows[10][1] - 0.5)  # may land on a deleted rowid
    assert rows[9][0] < b <= rows[10][0]
    assert retention.boundary(db, "sweeps", 0) == db.execute("SELECT rowid FROM sweeps ORDER BY rowid LIMIT 1").fetchone()[0]
    db.execute("DELETE FROM sweeps")
    assert retention.boundary(db, "sweeps", NOW) is None


def test_prune_deletes_raw_rows_older_than_the_window(tmp_path):
    _, db = _db(tmp_path, days=10)
    before = {t: _span(db, t)[1] for t in retention.RAW}
    res = retention.prune(db, keep_days=7, max_gb=0, now=NOW)
    for t in retention.RAW:
        first, n = _span(db, t)
        assert first >= NOW - 7 * DAY
        assert n == pytest.approx(before[t] * 0.7, abs=2)
        assert res["deleted"][t] == before[t] - n
    assert res["cutoff"] == NOW - 7 * DAY and not res["floor_hit"]
    assert res["oldest"] >= NOW - 7 * DAY
    # Everything that isn't raw history stays.
    assert db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1
    # A second pass has nothing to do.
    assert sum(retention.prune(db, keep_days=7, max_gb=0, now=NOW)["deleted"].values()) == 0


def test_short_transactions(tmp_path, monkeypatch):
    _, db = _db(tmp_path, days=3)
    commits = []
    real = retention.delete_before

    class Counting:
        def __init__(self, db):
            self.db = db

        def execute(self, *a):
            return self.db.execute(*a)

        def commit(self):
            commits.append(1)
            self.db.commit()

    monkeypatch.setattr(retention, "MAX_BATCH_ROWS", 100)
    end = retention.boundary(db, "quotes", NOW - DAY)
    n = real(Counting(db), "quotes", end, threading.Event())
    assert n == 2 * 24 * 20 and len(commits) >= n // 100  # never more than 100 rows a transaction


def test_size_cap_trims_hour_by_hour_but_keeps_the_last_day(tmp_path, monkeypatch):
    _, db = _db(tmp_path, days=6)
    used = retention.used_bytes(db)
    monkeypatch.setattr(retention, "HEADROOM", 0)
    # A cap a little under what's in use: only the oldest hours go.
    res = retention.prune(db, keep_days=7, max_gb=used * 0.9 / retention.GB, now=NOW)
    assert 0 < res["deleted"]["quotes"] < 3 * 24 * 20
    assert res["used"] <= used * 0.9 and not res["floor_hit"]
    assert _span(db, "quotes")[0] > NOW - 6 * DAY
    # A cap nothing can meet: it stops at the last day and says so.
    res = retention.prune(db, keep_days=7, max_gb=1e-9, now=NOW)
    assert res["floor_hit"]
    first, n = _span(db, "quotes")
    assert NOW - DAY - 3600 <= first <= NOW - DAY + 3600 and n >= 23 * 20


def test_halt_stops_between_batches(tmp_path):
    _, db = _db(tmp_path, days=10)
    halt = threading.Event()
    halt.set()
    res = retention.prune(db, keep_days=1, max_gb=0, halt=halt, now=NOW)
    assert sum(res["deleted"].values()) == 0


def test_writer_is_never_blocked_for_long(tmp_path, monkeypatch):
    """A writer with a short busy timeout, like the scanner's, keeps inserting while a
    big prune runs, with the real rests between batches."""
    monkeypatch.setattr(retention, "MIN_REST_S", 0.005)
    monkeypatch.setattr(retention, "REST", 4.0)
    path, db = _db(tmp_path, days=30, per_hour=200)  # 144k quotes
    db.close()
    done, waits, errors = threading.Event(), [], []

    def writer():
        w = sqlite3.connect(path, timeout=2)
        while not done.is_set():
            t0 = time.monotonic()
            try:
                w.execute("INSERT INTO quotes (ts, pair) VALUES (?, 'K|P')", (NOW,))
                w.commit()
            except sqlite3.OperationalError as e:
                errors.append(e)
            waits.append(time.monotonic() - t0)
            time.sleep(0.002)
        w.close()

    t = threading.Thread(target=writer)
    t.start()
    p = sqlite3.connect(path, timeout=30)
    res = retention.prune(p, keep_days=1, max_gb=0, now=NOW)
    done.set()
    t.join()
    assert res["deleted"]["quotes"] == 29 * 24 * 200
    assert not errors and max(waits) < 1.0


def test_pruner_thread_runs_and_leaves_nothing_behind(tmp_path, monkeypatch):
    path, db = _db(tmp_path, days=10)
    db.close()
    monkeypatch.setattr(retention, "FIRST_S", 0.0)
    monkeypatch.setattr(retention, "EVERY_S", 3600.0)
    lines = []
    p = retention.Pruner(path, keep_days=7, max_gb=25, line=lambda text, stage: lines.append((stage, text)))

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(p.run(stop))
        while p.last is None:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)  # let the log line hop onto the loop
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(asyncio.wait_for(main(), 20))
    assert p.last["deleted"]["quotes"] == pytest.approx(3 * 24 * 20, abs=1)  # the thread's clock is a moment later
    assert lines and lines[0][0] == "prune" and "deleted" in lines[0][1]
    snap = p.snapshot()
    assert snap["enabled"] and snap["file_bytes"] > 0 and snap["oldest"] >= NOW - 7 * DAY - 1
    assert not p.thread.is_alive()
    assert not [th for th in threading.enumerate() if th.name == "prune"]


def test_pruner_off(tmp_path):
    p = retention.Pruner(str(tmp_path / "x.db"), keep_days=0, max_gb=0)

    async def main():
        stop = asyncio.Event()
        stop.set()
        await p.run(stop)

    asyncio.run(main())
    assert p.thread is None and not p.snapshot()["enabled"]
