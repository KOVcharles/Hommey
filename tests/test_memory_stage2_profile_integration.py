"""PostgreSQL integration tests for profile versioning and conflict resolution."""
import os
import uuid

import pytest

from context.profile_repository import PendingProfileChangeError


TEST_DSN = os.getenv("HOMMEY_TEST_POSTGRES_DSN", "")


@pytest.fixture
def repository():
    if not TEST_DSN:
        pytest.skip("set HOMMEY_TEST_POSTGRES_DSN to run profile integration tests")

    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from context.profile_repository import PostgresProfileRepository
    from webui_new.auth.migrations import apply_all_migrations

    apply_all_migrations(TEST_DSN)
    pool = ConnectionPool(
        conninfo=TEST_DSN,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": False, "row_factory": dict_row},
        open=True,
    )
    yield PostgresProfileRepository(pool)
    pool.close()


def _delete_user(repository, user_id: str) -> None:
    with repository.pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_change_requests WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM user_profile_facts WHERE user_id = %s", (user_id,))
                cur.execute(
                    "DELETE FROM memory_versions WHERE user_id = %s AND namespace = 'profile'",
                    (user_id,),
                )


def test_conflict_stays_pending_until_confirmation(repository):
    user_id = f"profile-{uuid.uuid4().hex}"
    try:
        first = repository.propose_explicit_fact(
            user_id=user_id,
            fact_key="home_location",
            value="上海浦东新区",
            source_turn_id=uuid.uuid4(),
            source_excerpt="我现在住上海浦东新区",
        )
        same = repository.propose_explicit_fact(
            user_id=user_id,
            fact_key="home_location",
            value=" 上海浦东新区 ",
            source_turn_id=uuid.uuid4(),
        )
        pending = repository.propose_explicit_fact(
            user_id=user_id,
            fact_key="home_location",
            value="杭州西湖区",
            source_turn_id=uuid.uuid4(),
            source_excerpt="我搬到杭州西湖区了",
        )

        assert first.status == "created"
        assert same.status == "unchanged"
        assert pending.status == "pending"
        assert pending.change is not None
        assert repository.get_active_fact(user_id, "home_location").fact_value == "上海浦东新区"

        repeated = repository.propose_explicit_fact(
            user_id=user_id,
            fact_key="home_location",
            value="杭州西湖区",
            source_turn_id=uuid.uuid4(),
        )
        assert repeated.change.change_id == pending.change.change_id

        with pytest.raises(PendingProfileChangeError):
            repository.propose_explicit_fact(
                user_id=user_id,
                fact_key="home_location",
                value="北京朝阳区",
                source_turn_id=uuid.uuid4(),
            )

        confirmed = repository.resolve_change(
            user_id=user_id,
            change_id=pending.change.change_id,
            accepted=True,
        )
        assert confirmed.change.status == "confirmed"
        assert confirmed.fact.fact_value == "杭州西湖区"
        assert confirmed.fact.version == 2
        assert confirmed.fact.write_mode == "user_confirmed"

        # Resolution is idempotent and returns the already active version.
        replay = repository.resolve_change(
            user_id=user_id,
            change_id=pending.change.change_id,
            accepted=True,
        )
        assert replay.change.status == "confirmed"
        assert replay.fact.fact_id == confirmed.fact.fact_id

        with repository.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, version FROM user_profile_facts
                    WHERE user_id = %s AND fact_key = 'home_location'
                    ORDER BY version
                    """,
                    (user_id,),
                )
                versions = cur.fetchall()
        assert versions == [
            {"status": "superseded", "version": 1},
            {"status": "active", "version": 2},
        ]
    finally:
        _delete_user(repository, user_id)


def test_rejection_keeps_the_existing_fact_active(repository):
    user_id = f"profile-{uuid.uuid4().hex}"
    try:
        repository.propose_explicit_fact(
            user_id=user_id,
            fact_key="seat_preference",
            value="靠窗",
            source_turn_id=uuid.uuid4(),
        )
        pending = repository.propose_explicit_fact(
            user_id=user_id,
            fact_key="seat_preference",
            value="靠过道",
            source_turn_id=uuid.uuid4(),
        )

        rejected = repository.resolve_change(
            user_id=user_id,
            change_id=pending.change.change_id,
            accepted=False,
        )

        assert rejected.change.status == "rejected"
        active = repository.get_active_fact(user_id, "seat_preference")
        assert active.fact_value == "靠窗"
        assert active.version == 1
        assert repository.get_pending_change(user_id) is None
    finally:
        _delete_user(repository, user_id)
