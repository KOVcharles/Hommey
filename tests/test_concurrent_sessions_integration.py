"""Real PostgreSQL + Redis regression for BUG-2026-09-15-01.

Use docker/docker-compose.test.yml and HOMMEY_TEST_POSTGRES_DSN.
Only the slow supervisor is replaced, with events controlling exact overlap.
"""
import asyncio
import os
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from agent_runtime.services import BusinessServices
from agent_runtime.contracts import Scope, MemorySearch
from context.memory_manager import MemoryManager
from context.memory_repository import PostgresCompatibilityStore, PostgresMemoryRepository
from settings import MEMORY_CONFIG
from utils.io_executor import run_blocking
from utils import redis_coordination
from webui_new.auth import require_path_user
from webui_new.core.errors import AppError, register_error_handlers
from webui_new.manager import HommeyWebInstance, WebHommeyManager
from webui_new.routes.chat import create_chat_router
from webui_new.routes.users import create_users_router


@pytest.fixture(scope="module")
def pool():
    dsn = os.getenv("HOMMEY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set HOMMEY_TEST_POSTGRES_DSN; requires dedicated PostgreSQL/Redis")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from webui_new.auth.migrations import apply_all_migrations
    apply_all_migrations(dsn)
    with ConnectionPool(dsn, min_size=1, max_size=6, kwargs={"row_factory": dict_row}) as pool:
        yield pool


@pytest_asyncio.fixture
async def setup(pool, monkeypatch):
    import context.memory_service as memory_module
    import redis.asyncio as redis
    client = redis.Redis(host="127.0.0.1", port=int(os.getenv("HOMMEY_REDIS_PORT", "56379")), decode_responses=True)
    await client.ping()
    monkeypatch.setattr(redis_coordination, "get_redis_coordination_client", lambda: client)
    monkeypatch.setattr(memory_module, "get_postgres_pool", lambda *_args: pool)
    monkeypatch.setitem(MEMORY_CONFIG, "long_term", {"backend": "postgres", "postgres_dsn": "test"})
    monkeypatch.setitem(MEMORY_CONFIG, "short_term", {"backend": "memory", "max_turns": 10})
    uid = "concurrent-" + uuid.uuid4().hex
    managers = [WebHommeyManager(), WebHommeyManager()]
    for manager in managers:
        instance = HommeyWebInstance(uid)
        instance.memory_manager = MemoryManager(uid)
        instance.initialized = True
        manager._instances[uid] = instance
    repo = PostgresMemoryRepository(pool)
    sessions = [str(repo.create_session(uid).session_id) for _ in range(2)]
    yield uid, managers, sessions, repo
    store = PostgresCompatibilityStore(uid, repo)
    store.clear_chat_history()
    with pool.connection() as conn, conn.cursor() as cur:
        for table in ("conversation_sessions", "memory_versions", "user_statistics"):
            cur.execute(f"DELETE FROM {table} WHERE user_id=%s", (uid,))
    await client.aclose()


def http_app(manager):
    app = FastAPI()
    app.dependency_overrides[require_path_user] = lambda: object()
    register_error_handlers(app)
    app.include_router(create_chat_router(manager))
    app.include_router(create_users_router(manager))
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("different_workers", [False, True])
async def test_two_sessions_generate_while_history_and_creation_remain_available(setup, different_workers):
    uid, managers, (sa, sb), repo = setup
    first, second = managers if different_workers else (managers[0], managers[0])
    entered = {sa: asyncio.Event(), sb: asyncio.Event()}
    release = asyncio.Event()

    async def run(scope, text, **kwargs):
        entered[scope.session_id].set()
        await release.wait()
        return {"response": "reply:" + text}

    for manager in managers:
        manager.get(uid).supervisor = SimpleNamespace(run=run)
    # Distinct active trips make context contamination observable.
    store = PostgresCompatibilityStore(uid, repo)
    store.upsert_active_trip({"destination": "上海"}, sa)
    store.upsert_active_trip({"destination": "北京"}, sb)
    tasks = [asyncio.create_task(first.process_message(uid, "A", session_id=sa, request_id="ra"))]
    try:
        await asyncio.wait_for(entered[sa].wait(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_app(second)), base_url="http://test") as client:
            listed = await asyncio.wait_for(client.get(f"/api/{uid}/sessions"), 2)
            assert listed.status_code == 200
            assert {s["session_id"] for s in listed.json()["sessions"]} == {sa}
            assert "active_session_id" not in listed.json()
            history = await asyncio.wait_for(client.get(f"/api/{uid}/sessions/{sa}"), 2)
            assert history.json()["messages"][0]["content"] == "A"
            selected = await client.post(f"/api/{uid}/sessions/{sa}/activate")
            assert selected.status_code == 200
            created = await asyncio.wait_for(client.post(f"/api/{uid}/sessions"), 2)
            assert created.status_code == 200 and created.json()["session_id"] not in (sa, sb)
            trip = await client.get(f"/api/{uid}/trip/active", params={"session_id": sb})
            assert trip.json()["active_trip"]["destination"] == "北京"
            busy = await client.post(f"/api/{uid}/chat", json={"message": "duplicate", "session_id": sa})
            assert busy.status_code == 409
            assert busy.json()["error"]["code"] == "SESSION_BUSY"
            assert not busy.json()["error"].get("retryable", False)
            assert (await client.delete(f"/api/{uid}/sessions/{sa}")).status_code == 409
            assert (await client.delete(f"/api/{uid}/history")).status_code == 409
        tasks.append(asyncio.create_task(second.process_message(uid, "B", session_id=sb, request_id="rb")))
        await asyncio.wait_for(entered[sb].wait(), 3)
        assert not tasks[0].done(), "A must still be running when B reaches the supervisor"
    finally:
        release.set()
        outputs = await asyncio.gather(*tasks)
    assert [r["response"] for r in outputs] == ["reply:A", "reply:B"]
    for sid, text, rid in ((sa, "A", "ra"), (sb, "B", "rb")):
        rows = repo.get_messages(uid, session_id=sid)
        assert [r["content"] for r in rows] == [text, "reply:" + text]
        assert len({r["turn_id"] for r in rows}) == 1
        assert len({r["request_id"] for r in rows}) == 1
        services = BusinessServices(second.get(uid).memory_manager)
        context = await services.context(Scope(user_id=uid, session_id=sid, request_id=rid))
        assert context["trip"]["destination"] == ("上海" if sid == sa else "北京")
    assert managers[0].get(uid).memory_manager.current_request_id is None
    assert managers[0].get(uid).memory_manager.session_id is None
    assert not hasattr(managers[0].get(uid), "session_id")


@pytest.mark.asyncio
async def test_streams_overlap_and_cancel_independently(setup):
    uid, managers, (sa, sb), repo = setup
    entered = {sa: asyncio.Event(), sb: asyncio.Event()}
    cancelled = {sa: asyncio.Event(), sb: asyncio.Event()}
    release = asyncio.Event()

    async def run(scope, text, **kwargs):
        entered[scope.session_id].set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled[scope.session_id].set()
            raise
        return {"response": text}

    for manager in managers:
        manager.get(uid).supervisor = SimpleNamespace(run=run)

    async def consume(manager, sid):
        return [e async for e in manager.stream_message(uid, sid, session_id=sid)]

    tasks = [asyncio.create_task(consume(m, sid)) for m, sid in zip(managers, (sa, sb))]
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered.values())), 3)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert cancelled[sa].is_set() and not cancelled[sb].is_set()
        assert not tasks[1].done()
        # A's lease is released; B's lease still protects it across managers.
        await managers[1].run_session_operation(uid, sa, lambda instance: instance.delete_chat_session(sa))
        with pytest.raises(AppError, match="当前会话"):
            await managers[0].run_session_operation(uid, sb, lambda instance: instance.delete_chat_session(sb))
    finally:
        release.set()
        outputs = await asyncio.gather(*tasks, return_exceptions=True)
    assert outputs[1][-1]["type"] == "done"
    assert repo.get_messages(uid, session_id=sa) == []
    assert len(repo.get_messages(uid, session_id=sb)) == 2


@pytest.mark.asyncio
async def test_history_lease_excludes_new_writes_and_deleted_sessions_cannot_resume(setup):
    uid, managers, (sa, sb), repo = setup
    first, second = managers
    async with first._coordination_scope(uid, history=True, acquire_global_slot=False):
        with pytest.raises(AppError) as err:
            await second.run_session_operation(uid, None, lambda instance: instance.start_new_chat_session())
        assert err.value.code == "SESSION_BUSY"
    repo.close_session(uid, sa, reason="switched")
    bound = await run_blocking(first.get(uid).memory_manager.for_session, sa)
    assert bound.session_id == sa  # legacy histories remain resumable
    await first.run_session_operation(uid, sa, lambda instance: instance.delete_chat_session(sa))
    with pytest.raises(ValueError):
        await run_blocking(first.get(uid).memory_manager.for_session, sa)
    repo.close_session(uid, sb, reason="idle")
    await first.run_session_operation(uid, None, lambda instance: instance.clear_chat_history(), history=True)
    with pytest.raises(ValueError):
        await run_blocking(second.get(uid).memory_manager.for_session, sb)


@pytest.mark.asyncio
async def test_bound_memories_and_active_trip_search_do_not_leak_sessions(setup):
    uid, managers, (sa, sb), repo = setup
    parent = managers[0].get(uid).memory_manager
    a, b = await asyncio.gather(run_blocking(parent.for_session, sa), run_blocking(parent.for_session, sb))
    a.add_message("user", "A", {"request_id": "a"})
    b.add_message("user", "B", {"request_id": "b"})
    a.add_message("assistant", "answer A")
    b.add_message("assistant", "answer B")
    a.update_active_trip({"destination": "上海"})
    b.update_active_trip({"destination": "北京"})
    assert [r["content"] for r in a.short_term.get_recent_context()] == ["A", "answer A"]
    assert [r["content"] for r in b.short_term.get_recent_context()] == ["B", "answer B"]
    assert a._current_turn_id != b._current_turn_id
    assert parent._current_turn_id is None
    assert b.get_recorded_response("a") is None
    with pytest.raises(ValueError, match="different session"):
        b.add_message("user", "wrong retry", {"request_id": "a"})
    services = BusinessServices(parent)
    rows = await run_blocking(services._memory_search, Scope(user_id=uid, session_id=sa, request_id="a"), MemorySearch(kind="trips", query="", limit=12))
    assert len(rows) == 1 and rows[0]["data"]["context_data"]["destination"] == "上海"
    with pytest.raises(ValueError):
        await run_blocking(parent.for_session, str(uuid.uuid4()))
    other = MemoryManager("other-" + uid)
    with pytest.raises(ValueError):
        await run_blocking(other.for_session, sa)
