"""The background refresh job: ``catalog``, ``match`` and (with a Jev API key)
``review``, on a schedule or on demand.

Each stage runs as a niced subprocess so the scanner's event loop stays responsive
and the catalog's memory is returned to the OS when it finishes.
"""

import asyncio
import logging
import os
import re
import sys
import time
from collections import deque
from collections.abc import Callable

log = logging.getLogger(__name__)

STAGES = ("catalog", "match")
# Stage name -> `arbscan` subcommand.
COMMANDS = {"catalog": "catalog", "match": "match", "review": "autoreview"}
# After a failed run, try again this much later (or one interval, if shorter).
RETRY_AFTER_S = 15 * 60
# "2026-09-24 01:40:42,185 INFO arbscan.catalog: msg" -> "msg" ("WARNING: msg" for other levels)
_LOG_PREFIX = re.compile(r"^\S+ \S+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) [\w.]+: ")


def _keep_level(m: re.Match) -> str:
    return "" if m.group(1) == "INFO" else f"{m.group(1)}: "


class RefreshJob:
    def __init__(self, config_path: str | None, interval_s: float, last_catalog_ts: float | None,
                 notify: Callable[[str, dict], None], startup_delay_s: float = 10.0,
                 stages: tuple[str, ...] = STAGES):
        self.config_path = config_path
        self.interval_s = interval_s
        self.stages = stages
        self.startup_delay_s = startup_delay_s
        self.notify = notify
        self.state = "idle"  # idle | running | ok | failed
        self.stage: str | None = None
        self.stage_started: float | None = None
        self.started: float | None = None
        self.finished: float | None = None
        self.last_ok = last_catalog_ts
        self.error: str | None = None
        self.next_run: float | None = None
        self.log: deque[dict] = deque(maxlen=400)
        self.history: deque[dict] = deque(maxlen=20)
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict:
        return {
            "state": self.state, "stage": self.stage, "stage_started": self.stage_started,
            "started": self.started, "finished": self.finished, "last_ok": self.last_ok,
            "error": self.error, "next_run": self.next_run, "interval_s": self.interval_s,
            "stages": list(self.stages), "history": list(self.history),
        }

    def trigger(self) -> bool:
        if self.running:
            return False
        self._task = asyncio.create_task(self._run())
        return True

    def command(self, stage: str) -> list[str]:
        args = [sys.executable, "-m", "arbscan"]
        if self.config_path:
            args += ["-c", self.config_path]
        return args + [COMMANDS[stage]]

    def _line(self, text: str, stage: str | None) -> None:
        entry = {"ts": time.time(), "stage": stage, "text": text}
        self.log.append(entry)
        self.notify("log", entry)

    def _changed(self) -> None:
        self.notify("job", self.snapshot())

    async def _run(self) -> None:
        self.state, self.started, self.finished, self.error = "running", time.time(), None, None
        self._line("refresh started", None)
        timings: dict[str, float] = {}
        ok = True
        for stage in self.stages:
            self.stage, self.stage_started = stage, time.time()
            self._changed()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self.command(stage), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    preexec_fn=lambda: os.nice(10),
                )
                assert self._proc.stdout is not None
                async for raw in self._proc.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    if line:
                        self._line(_LOG_PREFIX.sub(_keep_level, line), stage)
                rc = await self._proc.wait()
            except Exception as e:  # e.g. the interpreter can't be spawned
                rc, self.error = -1, str(e)
            finally:
                self._proc = None
            timings[stage] = time.time() - self.stage_started
            if rc != 0:
                ok = False
                self.error = self.error or f"{stage} exited with code {rc}"
                self._line(self.error, stage)
                break
        self.finished = time.time()
        self.state = "ok" if ok else "failed"
        self.stage = None
        if ok:
            self.last_ok = self.finished
            self._line(f"refresh finished in {self.finished - self.started:.0f}s", None)
        self.history.appendleft({"started": self.started, "finished": self.finished, "ok": ok,
                                 "timings": timings, "error": self.error})
        self._schedule_next(failed=not ok)
        self._changed()

    def _schedule_next(self, failed: bool = False) -> None:
        if failed:
            self.next_run = time.time() + min(RETRY_AFTER_S, self.interval_s)
            return
        base = self.last_ok or 0.0
        self.next_run = max(time.time() + 30, base + self.interval_s)

    async def scheduler(self, stop: asyncio.Event) -> None:
        """Run on startup when the catalog is missing or stale, then every interval."""
        if self.last_ok is None or time.time() - self.last_ok > self.interval_s:
            self.next_run = time.time() + self.startup_delay_s  # let the scanner and web server start first
        else:
            self._schedule_next()
        self._changed()
        while not stop.is_set():
            delay = max(0.05, (self.next_run or time.time()) - time.time())
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(delay, 60))
                break
            except asyncio.TimeoutError:
                pass
            if self.next_run and time.time() >= self.next_run and not self.running:
                self.trigger()
            if self.running:
                # Wake on shutdown too, so stopping never waits for a long refresh.
                stopping = asyncio.create_task(stop.wait())
                await asyncio.wait({self._task, stopping}, return_when=asyncio.FIRST_COMPLETED)
                stopping.cancel()
        await self.cancel()

    async def cancel(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
