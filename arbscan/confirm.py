"""Checking a streamed Polymarket US book against the exchange before trading on it.

Polymarket US's market-data stream carries no sequence numbers, so nothing in it says
when an update was lost. On 2026-09-29 it went silent for single markets for minutes at
a time (the connection up, other markets on it flowing) while their real books moved
on: the scanner kept seeing an offer that was gone, Kalshi's price moved past it, and
three live trades bought the Kalshi leg against it and had to sell it back.

``BookCheck`` reads one market's book over REST, straight from the exchange (the route
is cached for 30 s at the edge; a unique query string gets past that, ~130 ms), and
brings the streamed book up to date if the exchange has a newer one: both carry the
book's ``transactTime``, so "newer" needs no guessing. The live trader asks for a read
a moment before a pick comes due, and sends nothing on a book that hasn't just been
read. A frozen book is repaired by the read, the pick priced on it disappears, and no
order goes out.
"""

import asyncio
import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

LEAD_S = 0.4  # a read is asked for this long before a pick comes due, so it costs the trade no time
MAX_AGE_S = 1.5  # a read sent longer ago than this no longer vouches for the book
MIN_GAP_S = 0.5  # a market read this recently isn't read again
RETRY_S = 2.0  # nor one whose read failed, for this long
READ_TIMEOUT_S = 2.0  # a read slower than this is given up (it would vouch for nothing by then)
FROZEN_S = 1.0  # the stream is called frozen if it was silent this long while the exchange moved on
READS_PER_S = 5.0  # reads asked for ahead of time, at most; one a trade is waiting on always goes


def _ts(text) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


class BookCheck:
    """``books``: the feed's Polymarket US books, by slug. ``read(slug)``: the market's
    ``marketData`` from the exchange (orders.PMTrading.book). ``on_checked(slug)`` is
    called after every read, so the pairs on that market are priced again."""

    def __init__(self, read, books: dict, on_checked=None):
        self.read, self.books, self.on_checked = read, books, on_checked
        self.checked: dict[str, float] = {}  # slug -> when its latest answered read was sent
        self.asked: dict[str, float] = {}  # slug -> when its latest read was sent, answered or not
        self.pending: set[str] = set()
        self.timers: dict[str, asyncio.TimerHandle] = {}
        self.tasks: set[asyncio.Task] = set()
        self.reads = self.behind = self.frozen = self.errors = 0
        self.last_frozen: dict | None = None
        self._next = 0.0

    def ok(self, slug: str) -> bool:
        """The streamed book was checked against the exchange a moment ago."""
        t = self.checked.get(slug)
        return t is not None and time.time() - t <= MAX_AGE_S

    def forget(self, slug: str) -> None:
        self.checked.pop(slug, None)
        self.asked.pop(slug, None)

    def ask(self, slug: str, ahead: bool = False) -> None:
        """Read ``slug``'s book now, unless a read is on its way or just came back."""
        now = time.time()
        if slug in self.pending or now - self.asked.get(slug, 0.0) < MIN_GAP_S:
            return  # a read that failed isn't repeated at once either
        if ahead:
            if now < self._next:
                return  # too many at once; the trade asks again when it gets there
            self._next = max(now, self._next) + 1.0 / READS_PER_S
        self.pending.add(slug)
        self.asked[slug] = now
        if len(self.asked) > 2000:  # markets long finished
            self.asked = {m: t for m, t in self.asked.items() if now - t < 60.0}
            self.checked = {m: t for m, t in self.checked.items() if now - t < 60.0}
        task = asyncio.get_running_loop().create_task(self._read(slug))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def ask_in(self, slug: str, delay: float) -> None:
        """Read ``slug``'s book ``delay`` seconds from now (a pick on it comes due soon after)."""
        if delay <= 0:
            self.ask(slug, ahead=True)
        elif slug not in self.timers:
            self.timers[slug] = asyncio.get_running_loop().call_later(delay, self._fire, slug)

    def _fire(self, slug: str) -> None:
        self.timers.pop(slug, None)
        self.ask(slug, ahead=True)

    async def _read(self, slug: str) -> None:
        sent = time.time()
        try:
            md = await asyncio.wait_for(self.read(slug), READ_TIMEOUT_S)
        except Exception as e:
            self._failed(slug, sent, f"{type(e).__name__}: {e}")
            return
        finally:
            self.pending.discard(slug)
        self.reads += 1
        if self.apply(slug, md, sent):
            self.checked[slug] = sent
        elif self.books.get(slug) is not None and self.books[slug].ready:
            self._failed(slug, sent, "the answer carried no transactTime")
        if self.on_checked is not None:
            self.on_checked(slug)

    def _failed(self, slug: str, sent: float, why: str) -> None:
        self.errors += 1
        self.asked[slug] = sent + RETRY_S - MIN_GAP_S
        if self.errors in (1, 10, 100) or self.errors % 1000 == 0:
            log.warning("book check: reading %s failed (%s); %d failed so far. No trade goes out on an "
                        "unchecked book", slug, why, self.errors)

    def apply(self, slug: str, md: dict, sent: float) -> bool:
        """Bring the streamed book up to the exchange's, if that is newer. False: the
        read can't vouch for the book (no book yet, or an answer without its time)."""
        book = self.books.get(slug)
        at = _ts((md or {}).get("transactTime"))
        if book is None or not book.ready or at is None:
            return False
        if book.exch_ts is None or at > book.exch_ts + 1e-4:
            silent = sent - book.recv_ts
            before = (book.yes_asks[:1], book.no_asks[:1])
            book.update(md, time.time())
            book.stamped = False  # not a stream update: it says nothing about the feed's lag
            self.behind += 1
            if silent > FROZEN_S and (book.yes_asks[:1], book.no_asks[:1]) != before:
                self.frozen += 1
                self.last_frozen = {"slug": slug, "ts": sent, "silent_s": silent}
                log.warning("polymarket book for %s was frozen: nothing streamed for %.0f s while the exchange's "
                            "book moved; refreshed from the exchange", slug, silent)
        return True

    def snapshot(self) -> dict:
        return {"reads": self.reads, "behind": self.behind, "frozen": self.frozen, "errors": self.errors,
                "last_frozen": self.last_frozen}
