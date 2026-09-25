"""HTTP API + live event stream for the dashboard.

Live state (latest quotes, open opportunities, sweep stats, refresh job) comes from
the in-process scanner and job; history comes from SQLite through short-lived
connections on worker threads, so a slow query never stalls the scanner.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import bankroll, jev
from ..config import Config
from ..jobs import RefreshJob
from ..pairs import remove_pairs
from ..scanner import Scanner
from ..store import open_db
from . import queries

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
CLOSEST_N = 8
OPEN_N = 20  # open windows sent with every live update, best picks first; the rest are on Opportunities
PAIR_STATUSES = ("live", "paused", "finished")
PAIR_SORTS = {
    "best": lambda p: max((e for e in (p.get("edges") or {}).values() if e is not None), default=None),
    "k_title": lambda p: p.get("k_title"),
    "relation": lambda p: p.get("relation"),
    "days": lambda p: p.get("days"),
    "status": lambda p: p.get("status"),
}


def _encode(data: Any) -> bytes:
    # orjson is ~10x faster than json, which matters because the dashboard shares
    # the event loop with the price feeds.
    return orjson.dumps(data, default=str, option=orjson.OPT_NON_STR_KEYS)


def _dumps(data: Any) -> str:
    return _encode(data).decode()


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return _encode(content)


class Hub:
    """Fan-out of server-sent events to every connected browser."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def publish(self, event: str, data: Any) -> None:
        if not self.subscribers:
            return
        msg = f"event: {event}\ndata: {_dumps(data)}\n\n"
        for q in self.subscribers:
            if q.full():  # a stalled client; drop its oldest message
                q.get_nowait()
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


class Service:
    def __init__(self, cfg: Config, scanner: Scanner, job: RefreshJob, hub: Hub, discovery=None):
        self.cfg = cfg
        self.scanner = scanner
        self.job = job
        self.hub = hub
        self.discovery = discovery
        self.pipeline: dict = {}
        self.titles: dict[str, dict] = {}
        self.rules = bankroll.PickRules.from_config(cfg)

    async def read(self, fn: Callable, *args) -> Any:
        def run():
            db = open_db(self.cfg.db_path)
            try:
                return fn(db, *args)
            finally:
                db.close()
        return await asyncio.to_thread(run)

    async def write(self, fn: Callable, *args) -> Any:
        def run():
            db = open_db(self.cfg.db_path, readonly=False)
            try:
                return fn(db, *args)
            finally:
                db.close()
        return await asyncio.to_thread(run)

    def paired(self) -> set[tuple[str, str]]:
        return {(p.kalshi, p.pm) for p in self.scanner.pairs.pairs}

    def _names(self, pid: str) -> dict:
        t = self.titles.get(pid)
        if t:
            return t
        k, p = pid.split("|", 1)
        return {"k_title": k, "p_title": p}

    def state(self) -> dict:
        sc = self.scanner
        statuses = [s.get("status") for s in sc.pair_state.values()]
        ranked = []
        for pid, s in sc.pair_state.items():
            if s.get("status") != "live":
                continue
            for direction, e in (s.get("edges") or {}).items():
                if e is not None:
                    ranked.append((e, pid, direction, s))
        ranked.sort(key=lambda x: -x[0])
        closest = [{"pair": pid, "direction": d, "edge": e, "relation": s.get("relation"), **self._names(pid)}
                   for e, pid, d, s in ranked[:CLOSEST_N]]
        now = time.time()
        open_eps = []
        for ep in sc.episodes.snapshot():
            # Judged on the latest quotes: that's what you could act on now.
            rate = bankroll.annualized(ep["profit"], ep["cost"], ep["days"])
            why = self.rules.reason(ep["edge"], now - ep["start_ts"], ep["days"], rate)
            open_eps.append({**ep, "rate": rate, "why": why})
        open_eps.sort(key=lambda e: (e["why"] is None, e["rate"] or 0.0, e["profit"]), reverse=True)
        return {
            "now": time.time(),
            "scanner": {
                "started": sc.started, "sweeps": sc.sweep_count, "last": sc.last_sweep, "last_error": sc.last_error,
                "poll_interval_s": self.cfg.poll_interval_s, "mode": sc.mode,
                "feeds": sc.feed_state() if hasattr(sc, "feed_state") else None,
                "pairs": {"total": len(sc.pairs.pairs), "live": statuses.count("live"),
                          "paused": statuses.count("paused"), "finished": len(sc.finished)},
            },
            "open": [{**self._names(ep["pair"]), **ep} for ep in open_eps[:OPEN_N]],
            "open_count": len(open_eps),
            "open_picks": sum(e["why"] is None for e in open_eps),
            "rules": self.rules.describe(),
            "closest": closest,
            "job": self.job.snapshot(),
            "discovery": self.discovery.snapshot() if self.discovery else None,
            "pipeline": self.pipeline,
            "features": {"jev": bool(jev.api_key(self.cfg))},
            "bankroll": self.cfg.bankroll_usd,
            "paper": self._paper_brief(),
        }

    def _paper_brief(self) -> dict | None:
        p = getattr(self.scanner, "paper", None)
        if p is None:
            return None
        return {"realized": p.realized, "locked": sum(t["locked_profit"] for t in p.open.values()),
                "open": len(p.open), "cash": dict(p.cash), "sent": p.stats.get("sent", 0)}

    def on_sweep(self) -> None:
        if self.hub.subscribers:
            self.hub.publish("state", self.state())

    async def maintain(self, stop: asyncio.Event) -> None:
        """Refresh cached catalog/review counts and pair titles every few seconds."""
        while not stop.is_set():
            try:
                pairs = self.scanner.pairs.pairs
                ids = sorted({p.id for p in pairs} | {k[0] for k in self.scanner.episodes.open})
                auto = sum(1 for p in pairs if p.note.startswith("auto:"))
                self.pipeline = await self.read(queries.pipeline, self.paired(), auto)
                self.titles = await self.read(queries.titles, ids)
                if not self.scanner.last_sweep or time.time() - self.scanner.last_sweep["ts"] > 10:
                    self.on_sweep()  # keep the UI current when the scanner is idle
            except Exception:
                log.exception("dashboard cache refresh failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass


def _float(req: Request, key: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(hi, max(lo, float(req.query_params.get(key, default))))
    except ValueError:
        return default


def create_app(svc: Service) -> Starlette:
    async def index(_: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def state(_: Request) -> Response:
        return JSON(svc.state())

    async def stream(request: Request) -> Response:
        q = svc.hub.subscribe()

        async def events():
            try:
                yield f"event: state\ndata: {_dumps(svc.state())}\n\n"
                yield f"event: job\ndata: {_dumps(svc.job.snapshot())}\n\n"
                while True:
                    try:
                        yield await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                svc.hub.unsubscribe(q)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def overview(request: Request) -> Response:
        return JSON(await svc.read(queries.overview, _float(request, "hours", 24, 0.25, 24 * 90),
                                   svc.cfg.bankroll_usd, svc.rules))

    async def pairs(request: Request) -> Response:
        """One page of watched pairs, filtered and sorted here: the full list runs to
        thousands of rows, too many to send to a browser every few seconds."""
        qp = request.query_params
        sc = svc.scanner
        rows = []
        for p in sc.pairs.pairs:
            st = sc.pair_state.get(p.id, {})
            status = "finished" if p.id in sc.finished else st.get("status", "pending")
            rows.append({"id": p.id, "kalshi": p.kalshi, "pm": p.pm, "relation": p.relation, "note": p.note,
                         "status": status, **{k: v for k, v in st.items() if k not in ("status", "relation")},
                         **svc._names(p.id)})
        counts = {"all": len(rows), **{s: sum(1 for r in rows if r["status"] == s) for s in PAIR_STATUSES}}
        if qp.get("status") in PAIR_STATUSES:
            rows = [r for r in rows if r["status"] == qp["status"]]
        if qp.get("relation") in ("same", "inverse"):
            rows = [r for r in rows if r["relation"] == qp["relation"]]
        needle = (qp.get("q") or "").strip().lower()
        if needle:
            rows = [r for r in rows if needle in f"{r.get('k_title')} {r.get('p_title')} {r['id']}".lower()]
        key = PAIR_SORTS.get(qp.get("sort") or "best", PAIR_SORTS["best"])
        known = [r for r in rows if key(r) is not None]
        known.sort(key=key, reverse=qp.get("dir", "desc") != "asc")
        rows = known + [r for r in rows if key(r) is None]  # blanks last either way
        offset = int(_float(request, "offset", 0, 0, 1e9))
        limit = int(_float(request, "limit", 100, 1, 500))
        return JSON({"items": rows[offset:offset + limit], "total": len(rows), "offset": offset,
                     "counts": counts, "now": time.time()})

    async def pair(request: Request) -> Response:
        pid = request.query_params.get("id", "")
        if "|" not in pid:
            return JSON({"error": "id must be KALSHI_TICKER|pm-slug"}, status_code=400)
        data = await svc.read(queries.pair_detail, pid, _float(request, "hours", 24, 0.25, 24 * 90))
        p = next((x for x in svc.scanner.pairs.pairs if x.id == pid), None)
        data["relation"] = p.relation if p else None
        data["live"] = svc.scanner.pair_state.get(pid)
        data["open"] = [ep for ep in svc.scanner.episodes.snapshot() if ep["pair"] == pid]
        return JSON(data)

    async def remove(request: Request) -> Response:
        body = await request.json()
        ids = set(body.get("ids") or [])
        if body.get("finished"):
            ids |= set(svc.scanner.finished)
        n = await asyncio.to_thread(remove_pairs, svc.cfg.pairs_path, ids)
        return JSON({"removed": n})

    async def opportunities(request: Request) -> Response:
        return JSON(await svc.read(queries.opportunities, _float(request, "hours", 24, 0.25, 24 * 90), svc.rules,
                                   request.query_params.get("view") != "all"))

    async def paper(request: Request) -> Response:
        data = await svc.read(queries.paper, _float(request, "hours", 24, 0.25, 24 * 90))
        trader = getattr(svc.scanner, "paper", None)
        data["live"] = trader.snapshot() if trader is not None else None
        data["rules"] = svc.rules.describe()
        return JSON(data)

    async def jobs(_: Request) -> Response:
        return JSON({"job": svc.job.snapshot(), "log": list(svc.job.log)})

    async def refresh(_: Request) -> Response:
        started = svc.job.trigger()
        return JSON({"started": started, "job": svc.job.snapshot()}, status_code=202 if started else 409)

    middleware = []
    if svc.cfg.web_token:
        middleware.append(Middleware(TokenGate, token=svc.cfg.web_token))
    middleware.append(Middleware(NoCacheStatic))

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/state", state),
            Route("/api/stream", stream),
            Route("/api/overview", overview),
            Route("/api/pairs", pairs),
            Route("/api/pair", pair),
            Route("/api/pairs/remove", remove, methods=["POST"]),
            Route("/api/opportunities", opportunities),
            Route("/api/paper", paper),
            Route("/api/jobs", jobs),
            Route("/api/jobs/refresh", refresh, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC)),
        ],
        middleware=middleware,
    )


class NoCacheStatic:
    """Static files revalidate on every load (cheap with ETags), so an upgrade
    shows up without a hard refresh."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/static/"):
            return await self.app(scope, receive, send)

        async def send_no_cache(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_no_cache)


class TokenGate:
    """Optional shared-secret gate: open the dashboard once with ?token=..., which
    sets a cookie for later requests."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        via_query = request.query_params.get("token") == self.token
        if not via_query and request.cookies.get("arbscan_token") != self.token:
            response = Response("arbscan: open this page with ?token=<web_token>", status_code=401)
            return await response(scope, receive, send)

        async def send_cookie(message):
            if via_query and message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(
                    "Set-Cookie", f"arbscan_token={self.token}; HttpOnly; SameSite=Strict; Max-Age=2592000; Path=/")
            await send(message)

        await self.app(scope, receive, send_cookie)
