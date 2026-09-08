"""Opt-in PostgreSQL smoke test: migrations, scope, receipts, fencing and deletion."""
import os
from uuid import uuid4

import pytest

from agent_runtime.contracts import Scope, RuntimeStopped, ToolRejected
from agent_runtime.store import RunStore, trip_version


@pytest.mark.asyncio
async def test_postgres_atomic_writes_replay_fencing_and_session_deletion():
    dsn = os.getenv("HOMMEY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set HOMMEY_TEST_POSTGRES_DSN to an isolated test database")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from context.memory_repository import PostgresMemoryRepository, PostgresCompatibilityStore
    from webui_new.auth.migrations import apply_all_migrations
    from agent_runtime.services import BusinessServices
    from types import SimpleNamespace

    apply_all_migrations(dsn)
    pool = ConnectionPool(dsn, min_size=1, max_size=3, kwargs={"row_factory": dict_row}, open=True)
    user = "supervisor-test-" + uuid4().hex
    repo = PostgresMemoryRepository(pool)
    session = repo.get_or_create_session(user, idle_timeout_sec=600)
    scope = Scope(user_id=user, session_id=str(session.session_id), request_id=uuid4().hex)
    compat = PostgresCompatibilityStore(user, repo)
    store = RunStore(pool)
    try:
        opened = await store.call("begin", scope, "same-input")
        owner = opened["owner"]
        receipt = await store.call("apply", scope, owner, "operation-one", trip_version({}),
            {"origin": "北京", "destination": "上海"}, {"seat_preference": "靠窗"}, "update")
        stored_trip = compat.get_active_trip(scope.session_id)
        assert receipt["version"] == trip_version(stored_trip)
        assert compat.get_preference()["seat_preference"] == "靠窗"
        repeated = await store.call("apply", scope, owner, "operation-one", -1, {}, {}, "update")
        assert repeated == receipt
        with pytest.raises(ToolRejected):
            await store.call("apply", scope, owner, "stale", trip_version({}), {}, {"seat_preference": "过道"}, "update")
        assert compat.get_preference()["seat_preference"] == "靠窗"
        await store.call("save", scope, owner, {"results": {}}, None, "running")
        reopened = await store.call("begin", scope, "same-input")
        with pytest.raises(RuntimeStopped):
            await store.call("apply", scope, owner, "old-worker", receipt["version"], {}, {"seat_preference": "过道"}, "update")
        assert not await store.call("cancel", scope.model_copy(update={"session_id": str(uuid4())}))
        assert await store.call("cancel", scope)
        with pytest.raises(RuntimeStopped):
            await store.call("save", scope, reopened["owner"], {}, None, "completed")
        services = BusinessServices(SimpleNamespace(user_id=user, session_id=scope.session_id, long_term=compat,
            get_active_trip=lambda: compat.get_active_trip(scope.session_id)))
        context = await services.context(scope)
        assert context["trip"]["origin"] == "北京"
        from agent_runtime.contracts import MemorySearch
        _, records = await services.execute(scope, "search_memory", MemorySearch(query="上海", kind="trips"))
        assert records and all(r["record_state"] == "planned" for r in records)
        newer = scope.model_copy(update={"request_id": uuid4().hex})
        await store.call("begin", newer, "newer-input")
        with pytest.raises(ToolRejected):
            await store.call("begin", scope, "same-input")
        compat.delete_chat_session(scope.session_id)
        with pool.connection() as conn, conn.cursor() as cur:
            for table in ("supervisor_runs", "supervisor_trip_records", "active_trip_contexts"):
                cur.execute(f"SELECT count(*) AS n FROM {table} WHERE user_id=%s", (user,))
                assert cur.fetchone()["n"] == 0
    finally:
        with pool.connection() as conn, conn.cursor() as cur:
            for table in ("supervisor_runs", "supervisor_trip_records", "active_trip_contexts", "user_preferences", "user_travel_preferences", "conversation_sessions"):
                cur.execute(f"DELETE FROM {table} WHERE user_id=%s", (user,))
        pool.close()
