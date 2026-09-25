"""Rate-limited JSON client with retry/backoff."""

import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Spaces requests at least 1/rate seconds apart; a 429 pushes the next slot out."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = time.monotonic()
            self._next = max(now, self._next) + self.interval

    def penalize(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


class Api:
    def __init__(self, client: httpx.AsyncClient, base: str, rps: float, name: str):
        self.client = client
        self.base = base.rstrip("/")
        self.limiter = RateLimiter(rps)
        self.name = name
        self.requests = 0
        self.errors = 0

    async def get(self, path: str, params: Any = None, attempts: int = 6) -> Any:
        return await self._send("GET", path, attempts, params=params)

    async def post(self, path: str, body: Any, attempts: int = 6) -> Any:
        return await self._send("POST", path, attempts, json=body)

    async def _send(self, method: str, path: str, attempts: int, **kwargs: Any) -> Any:
        url = self.base + path
        for attempt in range(attempts):
            await self.limiter.acquire()
            self.requests += 1
            try:
                r = await self.client.request(method, url, **kwargs)
            except httpx.TransportError as e:
                self.errors += 1
                wait = min(30.0, 2.0**attempt)
                log.warning("%s %s: %s; retrying in %.0fs", self.name, path, e, wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                self.errors += 1
                wait = min(30.0, 2.0**attempt)
                log.warning("%s %s: HTTP %d; backing off %.0fs", self.name, path, r.status_code, wait)
                self.limiter.penalize(wait)
                continue
            if r.status_code >= 400:
                self.errors += 1
                raise ApiError(f"{self.name} {path}: HTTP {r.status_code}: {r.text[:200]}", r.status_code)
            return r.json()
        raise ApiError(f"{self.name} {path}: gave up after {attempts} attempts")


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        headers={"User-Agent": "arbscan/0.1 (read-only scanner)"},
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )
