"""`arbscan serve`: scanner + scheduled refresh job + dashboard, in one process."""

import asyncio
import contextlib
import logging
import os
import signal
import sqlite3

import uvicorn

from .config import Config
from .http import make_client
from .jobs import RefreshJob
from .scanner import make_scanner, run_loop
from .web.app import Hub, Service, create_app

log = logging.getLogger(__name__)


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling; ``serve`` owns shutdown."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _web(server: _Server, stop: asyncio.Event, url: str) -> None:
    task = asyncio.create_task(_serve_web(server, url))
    await stop.wait()
    server.should_exit = True
    await task


async def _serve_web(server: _Server, url: str) -> None:
    log.info("dashboard at %s", url)
    try:
        await server.serve()
    except SystemExit:  # uvicorn exits this way when it can't bind
        log.error("dashboard failed to start on %s (port in use?); the scanner keeps running", url)


async def serve(cfg: Config, config_path: str | None, db: sqlite3.Connection, run_scanner: bool = True) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    last_catalog = db.execute("SELECT MAX(updated) FROM markets").fetchone()[0]
    hub = Hub()
    job = RefreshJob(os.path.abspath(config_path) if config_path else None,
                     cfg.refresh_interval_h * 3600, last_catalog, hub.publish)
    async with make_client() as client:
        scanner = make_scanner(cfg, db, client)
        svc = Service(cfg, scanner, job, hub)
        scanner.listeners.append(svc.on_sweep)
        server = _Server(uvicorn.Config(create_app(svc), host=cfg.web_host, port=cfg.web_port,
                                        log_level="warning", access_log=False, lifespan="off"))
        host = "localhost" if cfg.web_host in ("0.0.0.0", "::") else cfg.web_host
        tasks = [
            job.scheduler(stop),
            svc.maintain(stop),
            _web(server, stop, f"http://{host}:{cfg.web_port}/"),
        ]
        if run_scanner:
            tasks.append(run_loop(scanner, stop))
        await asyncio.gather(*tasks)
    log.info("stopped")
