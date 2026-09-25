"""SQLite writes on a background thread.

The streaming scanner records hundreds of rows a second. Committing them on the
event loop means a WAL checkpoint (an fsync to the SD card) or a lock held by the
hourly catalog rewrite stalls every feed for seconds. Instead the hot path queues
statements here, and a thread with its own connection executes and commits them.
"""

import logging
import queue
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

_COMMIT = object()
_STOP = object()
TRUNCATE_EVERY_S = 600  # shrink the WAL file back down now and then (off the event loop)


class DbWriter:
    def __init__(self, path: str):
        self.path = path
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.written = 0
        self.errors = 0
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
        self._thread.start()

    # The subset of sqlite3.Connection that the scanner's writers use.
    def execute(self, sql: str, args: tuple = ()) -> None:
        self.q.put((sql, args))

    def commit(self) -> None:
        self.q.put(_COMMIT)

    def flush(self, timeout: float = 30.0) -> None:
        """Block until everything queued so far is committed (for tests and shutdown)."""
        done = threading.Event()
        self.q.put(_COMMIT)
        self.q.put(done)
        done.wait(timeout)

    def close(self) -> None:
        self.q.put(_STOP)
        self._thread.join(timeout=30)

    @property
    def backlog(self) -> int:
        return self.q.qsize()

    def _run(self) -> None:
        db = sqlite3.connect(self.path, timeout=120)
        db.execute("PRAGMA synchronous=NORMAL")
        truncated = time.monotonic()
        try:
            while True:
                item = self.q.get()
                if item is _STOP:
                    break
                if item is _COMMIT:
                    db.commit()
                    if time.monotonic() - truncated > TRUNCATE_EVERY_S:
                        truncated = time.monotonic()
                        try:
                            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except sqlite3.Error as e:
                            log.debug("WAL checkpoint skipped: %s", e)
                elif isinstance(item, threading.Event):
                    item.set()
                else:
                    try:
                        db.execute(*item)
                        self.written += 1
                    except sqlite3.Error as e:
                        self.errors += 1
                        log.warning("database write failed: %s", e)
        finally:
            db.commit()
            db.close()
