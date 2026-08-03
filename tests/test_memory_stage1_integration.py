"""PostgreSQL integration tests; enable with HOMMEY_TEST_POSTGRES_DSN."""
import os
import uuid

import pytest


TEST_DSN = os.getenv("HOMMEY_TEST_POSTGRES_DSN", "")


@pytest.fixture
def repository():
    if not TEST_DSN:
        pytest.skip("set HOMMEY_TEST_POSTGRES_DSN to run PostgreSQL memory integration tests")
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from context.memory_repository import PostgresMemoryRepository
    from webui_new.auth.migrations import apply_all_migrations

    apply_all_migrations(TEST_DSN)
    pool = ConnectionPool(
        conninfo=TEST_DSN,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": False, "row_factory": dict_row},
        open=True,
    )
    repo = PostgresMemoryRepository(pool)
    yield repo
    pool.close()


def _delete_user(repository, user_id):
    with repository.pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for table in (
                    "conversation_messages",
                    "conversation_sessions",
                    "memory_versions",
                    "user_statistics",
                    "attachments",
                ):
                    cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))


def test_session_resumes_then_rotates_after_idle(repository):
    user_id = f"integration-{uuid.uuid4().hex}"
    try:
        first = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        resumed = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        assert resumed.session_id == first.session_id

        with repository.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE conversation_sessions SET last_active_at = NOW() - INTERVAL '601 seconds' WHERE session_id = %s",
                        (first.session_id,),
                    )

        rotated = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        assert rotated.session_id != first.session_id
        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, close_reason FROM conversation_sessions WHERE session_id = %s",
                    (first.session_id,),
                )
                old = cur.fetchone()
        assert old == {"status": "closed", "close_reason": "idle"}
    finally:
        _delete_user(repository, user_id)


def test_existing_session_can_be_reactivated_without_creating_another(repository):
    user_id = f"integration-{uuid.uuid4().hex}"
    try:
        first = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        second = repository.rotate_session(user_id)

        activated = repository.activate_session(user_id, first.session_id)

        assert activated.session_id == first.session_id
        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id FROM conversation_sessions WHERE user_id = %s AND status = 'active'",
                    (user_id,),
                )
                active = cur.fetchall()
        assert active == [{"session_id": first.session_id}]
        assert second.session_id != activated.session_id
    finally:
        _delete_user(repository, user_id)


def test_request_role_and_session_sequence_are_idempotent(repository):
    user_id = f"integration-{uuid.uuid4().hex}"
    try:
        session = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        user_message = repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role="user",
            content="hello",
            request_id="same-request",
        )
        duplicate = repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role="user",
            content="should-not-overwrite",
            request_id="same-request",
        )
        assistant = repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role="assistant",
            content="world",
            request_id="same-request",
            turn_id=user_message.turn_id,
        )

        assert user_message.inserted is True
        assert duplicate.inserted is False
        assert duplicate.message_id == user_message.message_id
        assert assistant.turn_id == user_message.turn_id
        assert assistant.sequence_no == 2
        assert repository.get_message_version(user_id) == 2
    finally:
        _delete_user(repository, user_id)


def test_message_and_attachment_binding_commit_together(repository):
    user_id = f"integration-{uuid.uuid4().hex}"
    attachment_id = f"att_{uuid.uuid4().hex}"
    try:
        session = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO attachments (
                        id, user_id, filename, kind, size_bytes, object_key, status
                    ) VALUES (%s, %s, 'policy.txt', 'document', 10, %s, 'ready')
                    """,
                    (attachment_id, user_id, f"{user_id}/{attachment_id}"),
                )

        message = repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role="user",
            content="请总结附件",
            request_id="attachment-request",
            attachment_ids=[attachment_id],
        )

        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT message_id FROM conversation_message_attachments
                    WHERE attachment_id = %s
                    """,
                    (attachment_id,),
                )
                link = cur.fetchone()
        assert link["message_id"] == message.message_id
    finally:
        _delete_user(repository, user_id)


def test_invalid_attachment_rolls_back_user_message(repository):
    user_id = f"integration-{uuid.uuid4().hex}"
    try:
        session = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        with pytest.raises(ValueError):
            repository.append_message(
                user_id=user_id,
                session_id=session.session_id,
                role="user",
                content="不应写入",
                request_id="invalid-attachment-request",
                attachment_ids=["att_missing"],
            )

        rows = repository.get_messages(user_id, request_id="invalid-attachment-request")
        assert rows == []
    finally:
        _delete_user(repository, user_id)


def test_idempotent_retry_cannot_change_attachment_set(repository):
    user_id = f"integration-{uuid.uuid4().hex}"
    attachment_ids = [f"att_{uuid.uuid4().hex}" for _ in range(2)]
    try:
        session = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO attachments (
                        id, user_id, filename, kind, size_bytes, object_key, status
                    ) VALUES (%s, %s, 'policy.txt', 'document', 10, %s, 'ready')
                    """,
                    [
                        (attachment_id, user_id, f"{user_id}/{attachment_id}")
                        for attachment_id in attachment_ids
                    ],
                )

        repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role="user",
            content="请总结附件",
            request_id="same-attachment-request",
            attachment_ids=[attachment_ids[0]],
        )
        with pytest.raises(ValueError):
            repository.append_message(
                user_id=user_id,
                session_id=session.session_id,
                role="user",
                content="请总结另一个附件",
                request_id="same-attachment-request",
                attachment_ids=[attachment_ids[1]],
            )
    finally:
        _delete_user(repository, user_id)


def test_delete_session_removes_attachment_links_but_keeps_private_object_metadata(repository):
    from context.memory_repository import PostgresCompatibilityStore

    user_id = f"integration-{uuid.uuid4().hex}"
    attachment_id = f"att_{uuid.uuid4().hex}"
    try:
        session = repository.get_or_create_session(user_id, idle_timeout_sec=600)
        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO attachments (
                        id, user_id, filename, kind, size_bytes, object_key, status
                    ) VALUES (%s, %s, 'policy.txt', 'document', 10, %s, 'ready')
                    """,
                    (attachment_id, user_id, f"{user_id}/{attachment_id}"),
                )
        repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role="user",
            content="delete this session",
            request_id="delete-session-request",
            attachment_ids=[attachment_id],
        )

        PostgresCompatibilityStore(user_id, repository).delete_chat_session(
            str(session.session_id)
        )

        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM conversation_message_attachments WHERE attachment_id = %s",
                    (attachment_id,),
                )
                links = cur.fetchone()["count"]
                cur.execute("SELECT COUNT(*) AS count FROM attachments WHERE id = %s", (attachment_id,))
                attachments = cur.fetchone()["count"]
        assert links == 0
        assert attachments == 1
    finally:
        _delete_user(repository, user_id)


def test_redis_cache_roundtrip_and_ttl_when_test_service_is_enabled():
    redis_host = os.getenv("HOMMEY_TEST_REDIS_HOST", "")
    if not redis_host:
        pytest.skip("set HOMMEY_TEST_REDIS_HOST to run Redis memory integration tests")

    redis_port = int(os.getenv("HOMMEY_TEST_REDIS_PORT", "56379"))
    from context.short_term_memory import ShortTermMemory

    user_id = f"integration-{uuid.uuid4().hex}"
    memory = ShortTermMemory(
        user_id=user_id,
        session_id="redis-test",
        backend="redis",
        redis_host=redis_host,
        redis_port=redis_port,
        redis_ttl_sec=120,
    )
    try:
        memory.add_message("user", "hello")
        memory.add_message("assistant", "world")

        assert [row["content"] for row in memory.get_recent_context(1)] == ["hello", "world"]
        assert 0 < memory.redis_client.ttl(memory.redis_key) <= 120
        assert 0 < memory.redis_client.ttl(memory.redis_version_key) <= 120
    finally:
        memory.clear()


def test_memory_service_resumes_session_and_messages_across_instances(monkeypatch):
    if not TEST_DSN:
        pytest.skip("set HOMMEY_TEST_POSTGRES_DSN to run PostgreSQL memory integration tests")

    from context.memory_service import MemoryService
    from context.postgres_pool import close_all_postgres_pools
    from settings import MEMORY_CONFIG
    from webui_new.auth.migrations import apply_all_migrations

    apply_all_migrations(TEST_DSN)
    monkeypatch.setitem(MEMORY_CONFIG["long_term"], "backend", "postgres")
    monkeypatch.setitem(MEMORY_CONFIG["long_term"], "postgres_dsn", TEST_DSN)
    redis_host = os.getenv("HOMMEY_TEST_REDIS_HOST", "")
    if redis_host:
        monkeypatch.setitem(MEMORY_CONFIG["short_term"], "backend", "redis")
        monkeypatch.setitem(MEMORY_CONFIG["short_term"], "redis_host", redis_host)
        monkeypatch.setitem(
            MEMORY_CONFIG["short_term"],
            "redis_port",
            int(os.getenv("HOMMEY_TEST_REDIS_PORT", "56379")),
        )
    else:
        monkeypatch.setitem(MEMORY_CONFIG["short_term"], "backend", "memory")

    close_all_postgres_pools()
    user_id = f"service-integration-{uuid.uuid4().hex}"
    first = MemoryService(user_id, requested_session_id="ignored")
    try:
        first.append_message("user", "跨实例问题", {"request_id": "resume-request"})
        first.append_message("assistant", "跨实例回答", {"request_id": "resume-request"})

        second = MemoryService(user_id, requested_session_id="also-ignored")
        assert second.session_id == first.session_id
        assert [row["content"] for row in second.get_recent_context(1)] == [
            "跨实例问题",
            "跨实例回答",
        ]
        assert second.get_recorded_response("resume-request") == "跨实例回答"
    finally:
        first.long_term.delete_all()
        close_all_postgres_pools()
