"""Application-owned bounded executor for blocking I/O off the event loop.

RAG embedding (sync HTTP) and the synchronous psycopg/redis repositories are
blocking calls.  Running them on asyncio's default executor would let a burst
of requests amplify the thread pool and starve the event loop; instead we route
all blocking I/O through one bounded, lazily-created executor so queue depth is
measurable and the loop keeps scheduling lock heartbeats.

``run_blocking`` mirrors ``asyncio.to_thread`` (contextvars are copied so the
per-request execution budget remains visible inside the worker thread), but
submits to the bounded executor instead of the default one.
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_admission: threading.BoundedSemaphore | None = None
_lock = threading.Lock()


class IoExecutorSaturated(RuntimeError):
    """The bounded I/O executor has no running or queued capacity left."""


def get_io_executor() -> ThreadPoolExecutor:
    global _executor, _admission
    if _executor is None:
        with _lock:
            if _executor is None:
                from settings import CONCURRENCY_CONFIG

                workers = max(
                    1, int(CONCURRENCY_CONFIG.get("io_executor_max_workers", 16))
                )
                pending = max(
                    0, int(CONCURRENCY_CONFIG.get("io_executor_max_pending", 32))
                )
                _executor = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="hommey-io"
                )
                # ThreadPoolExecutor's own queue is unbounded.  This admission
                # semaphore caps running + queued jobs before submit(), so a burst
                # is rejected instead of growing memory without limit.
                _admission = threading.BoundedSemaphore(workers + pending)
                logger.info(
                    "Opened bounded I/O executor (max_workers=%d, max_pending=%d)",
                    workers,
                    pending,
                )
    return _executor


async def run_blocking(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous ``func`` in the bounded I/O executor and await its result.

    Copies the current context so contextvars (e.g. the execution budget set by
    ``execution_budget_scope``) stay readable inside the worker thread.
    """
    loop = asyncio.get_running_loop()
    if kwargs:
        func = functools.partial(func, **kwargs)
    ctx = contextvars.copy_context()
    executor = get_io_executor()
    admission = _admission
    if admission is None or not admission.acquire(blocking=False):
        raise IoExecutorSaturated("bounded I/O executor is saturated")
    try:
        future = executor.submit(ctx.run, func, *args)
    except BaseException:
        admission.release()
        raise

    # Release only when the underlying thread job is really complete.  Releasing
    # in an await-finally would be too early when the caller is cancelled, because
    # a running synchronous database call cannot be cancelled by asyncio.
    future.add_done_callback(lambda _future: admission.release())
    return await asyncio.wrap_future(future, loop=loop)


async def shutdown_io_executor() -> None:
    """Shut down the bounded executor during graceful shutdown (idempotent)."""
    global _executor, _admission
    with _lock:
        executor, _executor = _executor, None
        _admission = None
    if executor is not None:
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
        logger.info("Closed bounded I/O executor")
