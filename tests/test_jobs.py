import asyncio
import os
import sys
import time

from arbscan.jobs import RefreshJob


def _job(script: str) -> tuple[RefreshJob, list]:
    events = []
    job = RefreshJob(None, 3600, None, lambda kind, data: events.append((kind, data)), startup_delay_s=0)
    job.command = lambda stage: [sys.executable, "-c", script.replace("STAGE", stage)]
    return job, events


def test_refresh_runs_both_stages():
    job, events = _job("print('2026-09-24 01:40:42,185 INFO arbscan.STAGE: hello from STAGE', flush=True); "
                       "print('2026-09-24 01:40:43,001 WARNING arbscan.http: slow', flush=True)")

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(job.scheduler(stop))
        while not job.history:
            await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(asyncio.wait_for(main(), 20))
    assert job.state == "ok" and job.history[0]["ok"]
    assert set(job.history[0]["timings"]) == {"catalog", "match"}
    assert [l["text"] for l in job.log if l["stage"]] == [
        "hello from catalog", "WARNING: slow", "hello from match", "WARNING: slow"]
    assert job.next_run > time.time() + 3000  # next run one interval later
    assert any(kind == "log" for kind, _ in events) and any(kind == "job" for kind, _ in events)


def test_shutdown_stops_a_running_stage_promptly():
    job, _ = _job("import os, time; print(os.getpid(), flush=True); time.sleep(60)")

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(job.scheduler(stop))
        while not any(l["stage"] == "catalog" for l in job.log):
            await asyncio.sleep(0.05)
        pid = int(next(l["text"] for l in job.log if l["stage"] == "catalog"))
        t0 = time.monotonic()
        stop.set()
        await task
        return pid, time.monotonic() - t0

    pid, took = asyncio.run(asyncio.wait_for(main(), 30))
    assert took < 5
    assert job.state == "failed"  # terminated mid-stage
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_review_stage_and_retry_after_failure():
    job = RefreshJob("/x/config.toml", 6 * 3600, None, lambda *a: None, startup_delay_s=0,
                     stages=("catalog", "match", "review"))
    assert job.command("review")[-3:] == ["-c", "/x/config.toml", "autoreview"]
    assert job.snapshot()["stages"] == ["catalog", "match", "review"]
    job.command = lambda stage: [sys.executable, "-c", "raise SystemExit(1)"]

    async def main():
        job.trigger()
        await job._task

    asyncio.run(main())
    assert job.state == "failed"
    # A failed run retries in 15 minutes instead of re-downloading the catalog every 30 s.
    assert 14 * 60 < job.next_run - time.time() <= 15 * 60
