import asyncio
import threading

import pytest

from utils.io_executor import IoExecutorSaturated, run_blocking, shutdown_io_executor


@pytest.mark.asyncio
async def test_executor_rejects_work_when_running_and_pending_capacity_is_full(monkeypatch):
    import settings

    await shutdown_io_executor()
    monkeypatch.setitem(settings.CONCURRENCY_CONFIG, "io_executor_max_workers", 1)
    monkeypatch.setitem(settings.CONCURRENCY_CONFIG, "io_executor_max_pending", 1)
    release = threading.Event()
    started = threading.Event()

    def blocking_job(value):
        started.set()
        release.wait(timeout=2)
        return value

    first = asyncio.create_task(run_blocking(blocking_job, 1))
    assert await asyncio.to_thread(started.wait, 1)
    second = asyncio.create_task(run_blocking(blocking_job, 2))
    await asyncio.sleep(0)

    try:
        with pytest.raises(IoExecutorSaturated):
            await run_blocking(blocking_job, 3)
    finally:
        release.set()

    assert await asyncio.gather(first, second) == [1, 2]
    await shutdown_io_executor()
