# tests/conftest.py
"""Shared pytest configuration for the tests/ directory.

Problem this solves: several test modules use pytest-asyncio with a
session-scoped event loop (`loop_scope="session"`) because they share the
singleton Redis client (`utils.redis_coordination.get_redis_coordination_client`)
and mixing event loops makes that client fail with "Event loop is closed".
However, many *synchronous* tests in the same session call `asyncio.run()`,
which in CPython closes its own private loop and then runs `set_event_loop(None)`,
wiping the policy's pointer to the session loop. Any later session-scoped test
then hits "Event loop is closed" inside pytest-asyncio.

Fix: capture the pytest-asyncio session loop in `set_event_loop` and re-assert it
via `asyncio.set_event_loop(...)` right before each non-function-scoped asyncio
test executes. Crucially, `get_event_loop` is left at the default behaviour:
pytest-asyncio's function-scoped `event_loop` fixture teardown finalizers call
`policy.get_event_loop()` and close whatever they get back, so if we restored the
session loop there they would close it (this is exactly what breaks the full
suite if the policy itself hands out the session loop).
"""
import asyncio

import pytest


class SessionLoopCapturePolicy(asyncio.DefaultEventLoopPolicy):
    """Capture the pytest-asyncio session loop without touching get_event_loop.

    Only `set_event_loop` is overridden, purely to remember the loop pytest-asyncio
    marks with `__pytest_asyncio = True` (the shared session loop created by its
    `_session_event_loop` fixture). `get_event_loop` keeps the default behaviour so
    the `event_loop` fixture teardown finalizers never see and close that loop.
    """

    _session_loop = None

    def set_event_loop(self, loop):
        if (
            loop is not None
            and not loop.is_closed()
            and getattr(loop, "__pytest_asyncio", False)
        ):
            SessionLoopCapturePolicy._session_loop = loop
        super().set_event_loop(loop)


def _session_loop():
    return getattr(SessionLoopCapturePolicy, "_session_loop", None)


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    asyncio.set_event_loop_policy(SessionLoopCapturePolicy())


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_pyfunc_call(pyfuncitem):
    # Re-assert the shared session loop before a session-scoped asyncio test runs.
    marker = pyfuncitem.get_closest_marker("asyncio")
    if marker is not None:
        scope = (
            marker.kwargs.get("loop_scope")
            or marker.kwargs.get("scope")
            or "function"
        )
        if scope != "function":
            loop = _session_loop()
            if loop is not None and not loop.is_closed():
                asyncio.set_event_loop(loop)
    yield
