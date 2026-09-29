"""Storage cap: delete raw streaming history once it's older than a window.

Nearly all of the database is raw streaming history: ``quotes`` (every top-of-book
change, ~250 bytes a row with its index), ``opportunities`` and ``sweeps``. On
2026-09-29 that was 11.8 of 12.1 GB, growing ~2 GB a weekday and ~4.5 GB a weekend
day, so a week of it is ~20 GB. Everything else (windows, trades, orders, results, the
catalog) is small and always kept.

Once an hour a thread inside ``arbscan serve`` deletes the raw rows older than
``keep_raw_days``. If the database still holds more than ``max_db_gb`` (less an hour's
headroom), it then deletes the oldest hours beyond that too, but never the last day.
Small, frequent passes keep each one light: at the weekend peak an hour is ~0.2 GB.

It stays out of the trading pipeline's way:
- Trading never waits on the database: every write goes through the scanner's
  ``DbWriter`` thread, and readers aren't blocked by a writer (WAL).
- It deletes a short rowid range per transaction (~0.1 s) and rests 4x as long between
  them, so the ``DbWriter`` waits a fraction of a second for the lock at worst.
- It finds the cut-off with a binary search on rowid (rows are appended in time
  order), so it never scans a table.
- It's a niced thread, not a process: nothing it starts can outlive the service, and it
  stops within one batch when the service does.

SQLite reuses the freed pages, so the file stops growing rather than shrinking.
"""

import asyncio
import logging
import os
import sqlite3
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

RAW = ("quotes", "opportunities", "sweeps")  # appended in time order, each with a ts column
EVERY_S = 3600.0
FIRST_S = 600.0  # first pass 10 minutes after start, clear of the restart's metadata burst
FLOOR_S = 86400.0  # the size cap never cuts raw history below a day
STEP_S = 3600.0  # the size cap trims an hour at a time
HEADROOM = 1e9  # the size cap aims this far under max_db_gb (a peak hour adds ~0.2 GB)
BATCH_ROWS = 2000  # rows per transaction to start with, adapted to BATCH_TARGET_S
MAX_BATCH_ROWS = 50_000
BATCH_TARGET_S = 0.1
REST = 4.0  # rest this many times as long as each batch took
MIN_REST_S = 0.05
GB = 1e9


def _first(db: sqlite3.Connection, table: str, desc: bool = False):
    # One end at a time: SQLite answers min(rowid) or max(rowid) alone from the b-tree,
    # but both in one query scans the table.
    return db.execute(f"SELECT rowid, ts FROM {table} ORDER BY rowid {'DESC' if desc else ''} LIMIT 1").fetchone()


def boundary(db: sqlite3.Connection, table: str, ts: float) -> int | None:
    """A rowid splitting the table at ``ts``: every row below it is older, the first row
    at or above it isn't (one past the last row if all are older; None if empty)."""
    lo = _first(db, table)
    if lo is None:
        return None
    a, b = lo[0], _first(db, table, desc=True)[0] + 1
    while a < b:
        m = (a + b) // 2
        r = db.execute(f"SELECT rowid, ts FROM {table} WHERE rowid >= ? ORDER BY rowid LIMIT 1", (m,)).fetchone()
        if r is None or r[1] >= ts:
            b = m
        else:
            a = r[0] + 1
    return a


def delete_before(db: sqlite3.Connection, table: str, end: int, halt: threading.Event) -> int:
    """Delete the rows with rowid < ``end``, oldest first, one short transaction at a time."""
    n, size = 0, BATCH_ROWS
    while not halt.is_set():
        first = _first(db, table)
        if first is None or first[0] >= end:
            break
        t0 = time.monotonic()
        n += db.execute(f"DELETE FROM {table} WHERE rowid >= ? AND rowid < ?",
                        (first[0], min(end, first[0] + size))).rowcount
        db.commit()
        took = time.monotonic() - t0
        if took < BATCH_TARGET_S / 2:
            size = min(size * 2, MAX_BATCH_ROWS)
        elif took > BATCH_TARGET_S * 2:
            size = max(size // 2, 100)
        halt.wait(max(MIN_REST_S, took * REST))
    return n


def used_bytes(db: sqlite3.Connection) -> int:
    """Bytes in use: the file less its free pages, which new rows fill first."""
    page = db.execute("PRAGMA page_size").fetchone()[0]
    return (db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]) * page


def oldest(db: sqlite3.Connection) -> float | None:
    """When the oldest raw row still kept was recorded."""
    firsts = [r[1] for r in (_first(db, t) for t in RAW) if r is not None]
    return min(firsts) if firsts else None


def prune(db: sqlite3.Connection, keep_days: float, max_gb: float, halt: threading.Event | None = None,
          now: float | None = None) -> dict:
    """Delete raw rows older than ``keep_days``, then more, an hour at a time, while the
    database holds more than ``max_gb`` less the headroom (0 turns either off)."""
    halt = halt or threading.Event()
    now = time.time() if now is None else now
    deleted = dict.fromkeys(RAW, 0)

    def trim(ts: float) -> None:
        for t in RAW:
            end = boundary(db, t, ts)
            if end is not None and not halt.is_set():
                deleted[t] += delete_before(db, t, end, halt)

    cutoff = now - keep_days * 86400 if keep_days > 0 else None
    if cutoff is not None:
        trim(cutoff)
    floor_hit = False
    if max_gb > 0:
        target = max_gb * GB - HEADROOM
        while not halt.is_set() and used_bytes(db) > target:
            first = oldest(db)
            if first is None:
                break
            ts = first + STEP_S
            if ts > now - FLOOR_S:
                floor_hit = True
                break
            cutoff = ts
            trim(ts)
    return {"deleted": deleted, "cutoff": cutoff, "floor_hit": floor_hit, "used": used_bytes(db),
            "oldest": oldest(db)}


class Pruner:
    """Runs ``prune`` every hour on its own thread inside ``arbscan serve``. ``line(text,
    stage)`` writes to the refresh job's log (called on the event loop)."""

    def __init__(self, db_path: str, keep_days: float, max_gb: float,
                 line: Callable[[str, str | None], None] | None = None):
        self.db_path = db_path
        self.keep_days, self.max_gb = keep_days, max_gb
        self.line = line
        self.halt = threading.Event()
        self.thread: threading.Thread | None = None
        self.last: dict | None = None
        self.next_at: float | None = None
        self.used: int | None = None
        self.oldest: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def enabled(self) -> bool:
        return self.keep_days > 0 or self.max_gb > 0

    def snapshot(self) -> dict:
        """Cheap (a stat, no queries): the dashboard state includes it on every sweep."""
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return {"enabled": self.enabled, "keep_days": self.keep_days, "max_gb": self.max_gb, "file_bytes": size,
                "used_bytes": self.used, "oldest": self.oldest, "last": self.last, "next": self.next_at}

    async def run(self, stop: asyncio.Event) -> None:
        if not self.enabled:
            return
        self._loop = asyncio.get_running_loop()
        self.thread = threading.Thread(target=self._thread, name="prune", daemon=True)
        self.thread.start()
        await stop.wait()
        self.halt.set()
        await asyncio.to_thread(self.thread.join, 30)

    def _say(self, text: str) -> None:
        log.info("%s", text)
        if self.line is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self.line, text, "prune")
            except RuntimeError:  # the loop has closed
                pass

    def _thread(self) -> None:
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)  # this thread only
        except (AttributeError, OSError):
            pass
        self._measure()
        wait = FIRST_S
        while True:
            self.next_at = time.time() + wait
            if self.halt.wait(wait):
                break
            self.run_once()
            wait = EVERY_S

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _measure(self) -> None:
        try:
            db = self._connect()
            try:
                self.used, self.oldest = used_bytes(db), oldest(db)
            finally:
                db.close()
        except sqlite3.Error as e:
            log.warning("storage check failed: %s", e)

    def run_once(self, now: float | None = None) -> dict | None:
        t0 = time.time()
        try:
            db = self._connect()
            try:
                res = prune(db, self.keep_days, self.max_gb, self.halt, now)
            finally:
                db.close()
        except sqlite3.Error as e:
            self.last = {"ts": t0, "error": str(e)}
            self._say(f"pruning failed, will retry in an hour: {e}")
            return None
        took = time.time() - t0
        self.used, self.oldest = res["used"], res["oldest"]
        self.last = {"ts": t0, "took_s": took, **res}
        n = sum(res["deleted"].values())
        if n:
            parts = ", ".join(f"{v:,} {t}" for t, v in res["deleted"].items() if v)
            self._say(f"deleted {parts} older than {time.strftime('%m-%d %H:%M', time.localtime(res['cutoff']))} "
                      f"in {took:.0f}s; {res['used'] / GB:.1f} GB in use")
        if res["floor_hit"]:
            self._say(f"the database holds {res['used'] / GB:.1f} GB, over the {self.max_gb:g} GB cap, "
                      "with only the last day of raw history left: something else is growing")
        return res
