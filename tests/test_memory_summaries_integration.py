"""PostgreSQL integration tests for incremental session summaries.

Enable with HOMMEY_TEST_POSTGRES_DSN. Exercises the claim/insert/read path,
idempotency, the watermark-driven disjoint-range guarantee, and the C1
no-self-heal trade-off.
"""
import concurrent.futures
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
        max_size=4,
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
                    "session_summaries",
                    "conversation_sessions",
                    "memory_versions",
                    "user_statistics",
                ):
                    cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))


def _seed_session(repository, user_id, n_msgs, char_len=20):
    """Create a session and append n user/assistant pairs with distinct request ids."""
    session = repository.get_or_create_session(user_id, idle_timeout_sec=600)
    for i in range(n_msgs):
        role = "user" if i % 2 == 0 else "assistant"
        repository.append_message(
            user_id=user_id,
            session_id=session.session_id,
            role=role,
            content=f"msg-{i}-{'x' * char_len}",
            request_id=f"seed-{i}",
        )
    return session


def test_claim_below_threshold_returns_none(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 8)  # 8 msgs, 160 chars
        claimed = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        assert claimed is None
        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT summary_watermark FROM conversation_sessions WHERE user_id = %s",
                    (user_id,),
                )
                assert cur.fetchone()["summary_watermark"] == 0
    finally:
        _delete_user(repository, user_id)


def test_claim_after_threshold_advances_watermark_disjoint(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 20)
        first = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        assert first is not None
        assert (first.source_sequence_from, first.source_sequence_to) == (1, 10)
        assert first.segment_no == 1

        second = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        assert second is not None
        assert (second.source_sequence_from, second.source_sequence_to) == (11, 20)
        assert second.segment_no == 11  # segment_no == source_sequence_from (C2)
    finally:
        _delete_user(repository, user_id)


def test_insert_then_read_composes_in_order(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 20)
        first = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        repository.insert_session_summary(
            user_id=user_id,
            summary_id=first.summary_id,
            session_id=first.session_id,
            segment_no=first.segment_no,
            summary_text="segment one summary",
            source_sequence_from=first.source_sequence_from,
            source_sequence_to=first.source_sequence_to,
            source_message_count=first.source_message_count,
            prompt_version="test-v1",
        )
        second = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        repository.insert_session_summary(
            user_id=user_id,
            summary_id=second.summary_id,
            session_id=second.session_id,
            segment_no=second.segment_no,
            summary_text="segment two summary",
            source_sequence_from=second.source_sequence_from,
            source_sequence_to=second.source_sequence_to,
            source_message_count=second.source_message_count,
            prompt_version="test-v1",
        )

        summaries = repository.get_session_summaries(user_id, session.session_id)
        assert [row["summary_text"] for row in summaries] == [
            "segment one summary",
            "segment two summary",
        ]
        assert summaries[0]["segment_no"] == 1
        assert summaries[1]["source_sequence_to"] == 20
    finally:
        _delete_user(repository, user_id)


def test_idempotent_double_insert_updates_row(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 10)
        first = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        repository.insert_session_summary(
            user_id=user_id,
            summary_id=first.summary_id,
            session_id=first.session_id,
            segment_no=first.segment_no,
            summary_text="original text",
            source_sequence_from=first.source_sequence_from,
            source_sequence_to=first.source_sequence_to,
            source_message_count=first.source_message_count,
        )
        repository.insert_session_summary(
            user_id=user_id,
            summary_id=uuid.uuid4(),
            session_id=first.session_id,
            segment_no=first.segment_no,
            summary_text="updated text",
            source_sequence_from=first.source_sequence_from,
            source_sequence_to=first.source_sequence_to,
            source_message_count=first.source_message_count,
        )
        summaries = repository.get_session_summaries(user_id, session.session_id)
        assert len(summaries) == 1
        assert summaries[0]["summary_text"] == "updated text"
    finally:
        _delete_user(repository, user_id)


def test_claim_without_insert_advances_past_range_no_self_heal(repository):
    """Documents the C1 trade-off: a crash between claim and insert means the
    claimed range is skipped (watermark moved on), not re-summarized."""
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 20)
        first = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        assert first is not None
        # deliberately no insert_session_summary call
        second = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        assert second is not None
        assert (second.source_sequence_from, second.source_sequence_to) == (11, 20)
    finally:
        _delete_user(repository, user_id)


def test_concurrent_claims_are_disjoint(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 30)

        def claim():
            return repository.claim_summary_range(
                user_id, session.session_id, max_turns=5, max_chars=6000
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(claim)
            f2 = pool.submit(claim)
            r1, r2 = f1.result(), f2.result()

        assert r1 is not None and r2 is not None
        ranges = sorted(
            [(r1.source_sequence_from, r1.source_sequence_to),
             (r2.source_sequence_from, r2.source_sequence_to)]
        )
        (lo_from, lo_to), (hi_from, hi_to) = ranges
        assert lo_to < hi_from  # disjoint, watermark-driven

        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT summary_watermark FROM conversation_sessions WHERE user_id = %s",
                    (user_id,),
                )
                assert cur.fetchone()["summary_watermark"] == hi_to
    finally:
        _delete_user(repository, user_id)


def test_delete_session_removes_summaries(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 10)
        claimed = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        repository.insert_session_summary(
            user_id=user_id,
            summary_id=claimed.summary_id,
            session_id=claimed.session_id,
            segment_no=claimed.segment_no,
            summary_text="to be deleted",
            source_sequence_from=claimed.source_sequence_from,
            source_sequence_to=claimed.source_sequence_to,
            source_message_count=claimed.source_message_count,
        )
        from context.memory_repository import PostgresCompatibilityStore

        store = PostgresCompatibilityStore(user_id, repository)
        store.delete_chat_session(str(session.session_id))
        assert repository.get_session_summaries(user_id) == []
    finally:
        _delete_user(repository, user_id)


def test_delete_all_removes_summaries(repository):
    user_id = f"integration-sum-{uuid.uuid4().hex}"
    try:
        session = _seed_session(repository, user_id, 10)
        claimed = repository.claim_summary_range(
            user_id, session.session_id, max_turns=5, max_chars=6000
        )
        repository.insert_session_summary(
            user_id=user_id,
            summary_id=claimed.summary_id,
            session_id=claimed.session_id,
            segment_no=claimed.segment_no,
            summary_text="gone",
            source_sequence_from=claimed.source_sequence_from,
            source_sequence_to=claimed.source_sequence_to,
            source_message_count=claimed.source_message_count,
        )
        from context.memory_repository import PostgresCompatibilityStore

        store = PostgresCompatibilityStore(user_id, repository)
        store.delete_all()
        assert repository.get_session_summaries(user_id) == []
    finally:
        _delete_user(repository, user_id)
