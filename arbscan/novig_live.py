"""Novig pairs priced live, inside the streaming scanner.

Novig's books stream over its websocket (feeds.NovigFeed, with a read-only
``trading::read`` key); the other half of each pair is a Kalshi or Polymarket US
market whose book the scanner already streams (it subscribes to them for this). The
pairs are the matcher's confident, mutual-best Novig candidates (``novig_gaps.pairs``)
whose game hasn't started and starts within ``novig_horizon_h``, soonest first, up to the 2,048 markets
one Novig connection can watch. They aren't reviewed by Jev yet, so a window here is
a measurement, not a pick.

Whenever either book changes, the pair is priced in both directions after both
venues' taker fees (Novig's is zero before a game goes live on markets that charge
only when live) and walked at $100 a leg, one venue's share of the bankroll across
three venues. Runs of positive observations are saved to ``novig_windows``, the way
``episodes`` saves Kalshi-Polymarket windows. Nothing is traded.
"""

import logging
import time
from collections import defaultdict

from .arb import Leg, top_edge, walk
from .feeds import NovigFeed
from .novig_gaps import directions, pairs
from .scanner import PMUS_DEFAULT_COEF, Episodes

log = logging.getLogger(__name__)

RELOAD_S = 600.0  # re-read the pairs (the matcher rebuilds them every refresh)


class NovigLink:
    def __init__(self, scanner, feed: NovigFeed):
        self.s = scanner
        self.cfg = scanner.cfg
        self.feed = feed
        feed.on_update = self.on_novig
        self.windows = Episodes(scanner.out, reader=scanner.db, table="novig_windows")
        self.by_novig: dict[str, list[dict]] = defaultdict(list)
        self.by_other: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.loaded = 0.0
        self.evals = 0
        self.best: tuple[float, str, str] | None = None
        self.budget = self.cfg.bankroll_usd / 3 if self.cfg.bankroll_usd > 0 else None

    # --- which pairs ------------------------------------------------------------------

    def due(self) -> bool:
        return time.monotonic() - self.loaded >= RELOAD_S

    def reload(self) -> bool:
        """Re-read the pairs; True if the markets to stream changed."""
        self.loaded = time.monotonic()
        before = (set(self.by_novig), set(self.by_other))
        rows = sorted(pairs(self.s.db, self.cfg.novig_horizon_h * 3600, time.time()), key=lambda r: r["start_ts"])
        keep: list[str] = []
        self.by_novig.clear()
        self.by_other.clear()
        for r in rows:
            if r["novig"] not in self.by_novig:
                if len(keep) >= NovigFeed.MAX_MARKETS:
                    continue
                keep.append(r["novig"])
            r["id"] = f"{r['venue']}:{r['other']}|{r['novig']}"
            self.by_novig[r["novig"]].append(r)
            self.by_other[(r["venue"], r["other"])].append(r)
        watched = {r["id"] for rs in self.by_novig.values() for r in rs}
        for key in [k for k in self.windows.open if k[0] not in watched]:
            self.windows.close(key, time.time())
        self.feed.set_markets(self.by_novig)
        changed = before != (set(self.by_novig), set(self.by_other))
        if changed:
            log.info("novig: %d pairs on %d Novig markets (%d Kalshi, %d Polymarket US)", len(watched),
                     len(self.by_novig), len(self.tickers()), len(self.slugs()))
        return changed

    def tickers(self) -> set[str]:
        return {o for v, o in self.by_other if v == "K"}

    def slugs(self) -> set[str]:
        return {o for v, o in self.by_other if v == "P"}

    # --- pricing (the hot path) ------------------------------------------------------

    def on_novig(self, market: str) -> None:
        for p in self.by_novig.get(market, ()):
            self.evaluate(p)

    def on_other(self, venue: str, market: str) -> None:
        for p in self.by_other.get((venue, market), ()):
            self.evaluate(p)

    def _other(self, p: dict):
        """(yes_asks, no_asks, fee coefficient, resolution time) of the other venue, or
        None while it can't be priced."""
        if p["venue"] == "K":
            km, kb = self.s.kmeta.get(p["other"]), self.s.kfeed.books.get(p["other"])
            if km is None or km.status != "active" or kb is None or not kb.ready:
                return None
            return (*kb.ladders(), km.fee_coef, km.resolve_ts)
        pb = self.s.pfeed.books.get(p["other"])
        if pb is None or not pb.ready or not pb.open or p["other"] in self.s.pm_closed:
            return None
        coef = self.s.pm_coef.get(p["other"], PMUS_DEFAULT_COEF) * (1 - self.cfg.pmus_taker_rebate)
        return pb.yes_asks, pb.no_asks, coef, p["close_ts"]

    def evaluate(self, p: dict) -> None:
        ts = time.time()
        nb = self.feed.books.get(p["novig"])
        other = self._other(p) if nb is not None and nb.ready and nb.open else None
        if other is None:
            for label, _, _ in directions(p["relation"]):
                self.windows.observe((p["id"], label), ts, None, None)
            return
        o_yes, o_no, o_coef, resolve = other
        n_yes, n_no = nb.ladders(p["yes_outcome"], p["no_outcome"])
        pregame = not nb.live and ts < p["start_ts"]
        n_coef = 0.0 if pregame and p["fee_when_live"] else p["n_coef"]
        end = resolve or p["start_ts"] + 4 * 3600
        days = max(0.0, (end - ts) / 86400)
        for label, n_side, o_side in directions(p["relation"]):
            nl = n_yes if n_side == "yes" else n_no
            ol = o_yes if o_side == "yes" else o_no
            e = top_edge(nl[0][0] if nl else None, n_coef, ol[0][0] if ol else None, o_coef)
            if e is not None and (self.best is None or e > self.best[0]):
                self.best = (e, p["id"], label)
            if e is None or e <= self.cfg.depth_trigger_edge:
                self.windows.observe((p["id"], label), ts, None, days)
                continue
            res = walk(Leg(nl, n_coef), Leg(ol, o_coef), self.cfg.min_edge, budget_a=self.budget, budget_b=self.budget)
            self.windows.observe((p["id"], label), ts, res, days)
        self.evals += 1

    def snapshot(self) -> dict:
        ready = sum(1 for m in self.by_novig if (b := self.feed.books.get(m)) is not None and b.ready)
        return {**self.feed.stats.snapshot(), "markets": len(self.by_novig), "ready": ready,
                "pairs": sum(len(v) for v in self.by_novig.values()), "open_windows": len(self.windows.open),
                "errors": list(self.feed.errors)[-5:]}
