"""The background writer: it must never sit on the database's write lock."""

import sqlite3
import time

from arbscan import dbwriter
from arbscan.dbwriter import DbWriter
from arbscan.store import connect


def test_shrinking_the_log_never_holds_the_write_lock_while_someone_reads(tmp_path, monkeypatch):
    # Until 2026-11-11 the TRUNCATE checkpoint waited up to two minutes for a slow read (a
    # 90-day dashboard query takes ~30 s) with the write lock held, so the hourly catalog
    # and the pruner, which wait 30 s, failed with "database is locked" for days.
    monkeypatch.setattr(dbwriter, "TRUNCATE_EVERY_S", 0.0)
    path = str(tmp_path / "t.db")
    connect(path).close()
    w = DbWriter(path)
    for ts in range(5):
        w.execute("INSERT INTO sweeps (ts, n_pairs) VALUES (?, 1)", (ts,))
    w.flush()
    reader = sqlite3.connect(path)
    cur = reader.execute("SELECT ts FROM sweeps")  # a read under way: it pins the log
    cur.fetchone()  # more rows are left, so the read stays open
    w.execute("INSERT INTO sweeps (ts, n_pairs) VALUES (5, 1)")
    w.commit()  # the checkpoint falls due here
    time.sleep(0.2)
    other = sqlite3.connect(path, timeout=dbwriter.CHECKPOINT_WAIT_S + 2.0, isolation_level=None)
    t0 = time.monotonic()
    other.execute("BEGIN IMMEDIATE")
    other.execute("ROLLBACK")
    assert time.monotonic() - t0 < dbwriter.CHECKPOINT_WAIT_S + 1.0
    w.execute("INSERT INTO sweeps (ts, n_pairs) VALUES (6, 1)")
    w.flush()  # and the writer carries on
    assert w.errors == 0 and other.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0] == 7
    cur.close()
    reader.close()
    other.close()
    w.close()
