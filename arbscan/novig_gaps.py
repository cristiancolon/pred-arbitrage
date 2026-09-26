"""Measure the gaps between Novig and the other venues, from public order books.

Until Novig's order books are streamed (which needs an API key), this samples them:
for each suggested pair (``novig_candidates``, mutual best matches only) whose game
starts within ``--hours``, it reads the Novig book and, within seconds, the other
venue's book, and walks both at the configured bankroll (``arb.walk``), after fees.
Novig charges nothing on a fill before the game starts. Each observation goes to
``novig_gaps``.

Novig throttles its public routes per IP at about 2 books a second, so one sweep of
600 markets takes about five minutes: long-lived gaps show up, sub-second ones don't
(and those couldn't be traded from here anyway).
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections import defaultdict

from .arb import Leg, walk
from .book import kalshi_ladders, pmus_ladders, top_n
from .config import Config
from .http import Api, ApiError, make_client
from .novig import Novig, ladders
from .venues import Kalshi, PolymarketUS

log = logging.getLogger(__name__)

CHUNK = 20  # Novig books read before fetching their counterparts
BOOK_LEVELS = 5


def pairs(db: sqlite3.Connection, horizon_s: float, now: float) -> list[dict]:
    """Confident, mutual-best Novig candidates whose game hasn't started and starts
    within ``horizon_s``, soonest first."""
    rows = [dict(r) for r in db.execute(
        "SELECT c.venue, c.other, c.novig, c.score, c.relation, n.start_ts, n.fee_coef AS n_coef, "
        "o.fee_coef AS o_coef, o.close_ts, n.title, x.yes_outcome, x.no_outcome, x.fee_when_live "
        "FROM novig_candidates c JOIN markets n ON n.venue = 'N' AND n.id = c.novig "
        "JOIN markets o ON o.venue = c.venue AND o.id = c.other "
        "JOIN novig_outcomes x ON x.market = c.novig "
        "WHERE c.confident = 1 AND n.start_ts > ? AND n.start_ts <= ? ORDER BY c.score DESC",
        (now, now + horizon_s))]
    best_other: dict[tuple[str, str], dict] = {}
    best_novig: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        best_other.setdefault((r["venue"], r["other"]), r)
        best_novig.setdefault((r["venue"], r["novig"], r["relation"]), r)
    out = [r for r in rows if best_other[(r["venue"], r["other"])] is r
           and best_novig[(r["venue"], r["novig"], r["relation"])] is r]
    out.sort(key=lambda r: r["start_ts"])
    return out


def directions(relation: str) -> list[tuple[str, str, str]]:
    """(label, Novig side, other side) for each way to buy the pair."""
    if relation == "same":
        return [("N:YES+O:NO", "yes", "no"), ("N:NO+O:YES", "no", "yes")]
    return [("N:YES+O:YES", "yes", "yes"), ("N:NO+O:NO", "no", "no")]


async def sweep(cfg: Config, db: sqlite3.Connection, nv: Novig, kalshi: Kalshi, pm: PolymarketUS,
                todo: list[dict]) -> int:
    budget = cfg.bankroll_usd / 3 if cfg.bankroll_usd > 0 else None  # $100 on each of three venues
    by_novig: dict[str, list[dict]] = defaultdict(list)
    for p in todo:
        by_novig[p["novig"]].append(p)
    ids = list(by_novig)
    n_rows = 0
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        nbooks: dict[str, tuple[float, dict]] = {}
        for mid in chunk:
            try:
                nbooks[mid] = (time.time(), await nv.book(mid))
            except ApiError as e:
                log.debug("novig book %s: %s", mid, e)
        want_k = sorted({p["other"] for mid in chunk for p in by_novig[mid] if p["venue"] == "K"})
        want_p = sorted({p["other"] for mid in chunk for p in by_novig[mid] if p["venue"] == "P"})
        kbooks = {t: kalshi_ladders(ob) for t, ob in (await kalshi.orderbooks(want_k)).items()} if want_k else {}
        pbooks = {}
        for slug in want_p:
            try:
                pbooks[slug] = pmus_ladders(await pm.book(slug))
            except ApiError as e:
                log.debug("pm book %s: %s", slug, e)
        now = time.time()
        rows = []
        for mid in chunk:
            if mid not in nbooks:
                continue
            seen, book = nbooks[mid]
            for p in by_novig[mid]:
                n_yes, n_no = ladders(book, p["yes_outcome"], p["no_outcome"])
                other = (kbooks if p["venue"] == "K" else pbooks).get(p["other"])
                if other is None:
                    continue
                o_yes, o_no = other
                pregame = p["start_ts"] > seen
                n_coef = 0.0 if (pregame and p["fee_when_live"]) else p["n_coef"]
                days = max(0.0, ((p["close_ts"] or p["start_ts"] + 4 * 3600) - now) / 86400)
                for label, ns, os_ in directions(p["relation"]):
                    nl = n_yes if ns == "yes" else n_no
                    ol = o_yes if os_ == "yes" else o_no
                    res = walk(Leg(nl, n_coef), Leg(ol, p["o_coef"]), cfg.min_edge, budget_a=budget, budget_b=budget)
                    if res.top_edge is None:
                        continue
                    rows.append((seen, now, p["venue"], p["other"], mid, label, res.top_edge, res.size, res.cost,
                                 res.profit, n_coef, days, int(pregame),
                                 json.dumps(top_n(nl, BOOK_LEVELS)), json.dumps(top_n(ol, BOOK_LEVELS))))
        db.executemany("INSERT INTO novig_gaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()
        n_rows += len(rows)
    return n_rows


async def run(cfg: Config, db: sqlite3.Connection, hours: float = 36.0, once: bool = False,
              every_s: float = 60.0) -> None:
    async with make_client() as client:
        nv = Novig(Api(client, cfg.novig_base, cfg.novig_rps, "novig"))
        kalshi = Kalshi(Api(client, cfg.kalshi_base, cfg.kalshi_rps, "kalshi"))
        pm = PolymarketUS(Api(client, cfg.pmus_base, cfg.pmus_rps, "pmus"))
        while True:
            t0 = time.time()
            todo = pairs(db, hours * 3600, t0)
            n = await sweep(cfg, db, nv, kalshi, pm, todo)
            log.info("novig gaps: %d pairs (%d Novig markets), %d observations in %.0fs", len(todo),
                     len({p["novig"] for p in todo}), n, time.time() - t0)
            if once:
                return
            await asyncio.sleep(max(0.0, every_s - (time.time() - t0)))


def report(db: sqlite3.Connection, hours: float = 24.0) -> None:
    since = time.time() - hours * 3600
    rows = [dict(r) for r in db.execute(
        "SELECT g.*, n.title, n.market_type FROM novig_gaps g JOIN markets n ON n.venue = 'N' AND n.id = g.novig "
        "WHERE g.ts >= ?", (since,))]
    if not rows:
        print("no Novig gap observations yet; run `arbscan novig-gaps`")
        return
    obs = len(rows)
    pos = [r for r in rows if r["profit"] > 0]
    print(f"{obs} observations of {len({(r['venue'], r['other'], r['novig']) for r in rows})} pairs "
          f"over {(max(r['ts'] for r in rows) - min(r['ts'] for r in rows)) / 60:.0f} min")
    for v, name in (("K", "Kalshi"), ("P", "Polymarket US")):
        vr = [r for r in rows if r["venue"] == v]
        vp = [r for r in vr if r["profit"] > 0]
        if vr:
            edges = sorted(r["top_edge"] for r in vr)
            print(f"  Novig–{name}: {len(vr)} obs, {len(vp)} profitable ({len(vp) / len(vr):.1%}); "
                  f"median top edge {100 * edges[len(edges) // 2]:+.1f}c, best {100 * edges[-1]:+.1f}c")
    by_pair: dict[tuple, list[dict]] = defaultdict(list)
    for r in pos:
        by_pair[(r["venue"], r["other"], r["novig"], r["direction"])].append(r)
    print(f"\n{len(by_pair)} pair-directions were profitable at least once; the best:")
    top = sorted(by_pair.items(), key=lambda kv: -max(x["profit"] for x in kv[1]))[:25]
    for (v, other, _, d), xs in top:
        best = max(xs, key=lambda x: x["profit"])
        print(f"  {v} {100 * best['top_edge']:+5.1f}c  ${best['profit']:6.2f} on {best['size']:4d}  "
              f"seen {len(xs)}x  {best['market_type']:<16.16} {best['title'][:58]}  <-> {other[:34]}")
