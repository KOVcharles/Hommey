"""Unit tests for stage-1 memory identifiers, migrations, and cache fallback."""

from datetime import datetime, timezone
from pathlib import Path
import uuid

from context.memory_repository import SessionRecord, stable_uuid
from context.memory_service import MemoryService
from webui_new.auth.migrations import _remap_legacy_version_collisions


class MigrationCursor:
    def __init__(self, rows):
        self.rows = {row[0]: row for row in rows}
        self.result = []

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("SELECT VERSION, NAME, CHECKSUM"):
            self.result = [self.rows[key] for key in params if key in self.rows]
        elif normalized.startswith("UPDATE SCHEMA_MIGRATIONS"):
            new_version, new_name, old_version, old_name = params
            row = self.rows.get(old_version)
            if row and row[1] == old_name:
                self.rows.pop(old_version)
                self.rows[new_version] = (new_version, new_name, row[2])
        elif normalized.startswith("DELETE FROM SCHEMA_MIGRATIONS"):
            self.rows.pop(params[0], None)
        else:  # pragma: no cover - keeps the fake strict
            raise AssertionError(sql)

    def fetchall(self):
        return self.result


def test_public_request_ids_map_to_stable_database_uuids():
    first = stable_uuid("request-from-http-header", namespace="request:u1")
    second = stable_uuid("request-from-http-header", namespace="request:u1")
    other_user = stable_uuid("request-from-http-header", namespace="request:u2")

    assert isinstance(first, uuid.UUID)
    assert first == second
    assert first != other_user


def test_stage1_migration_defines_fact_source_without_dropping_legacy_data():
    migration = (
        Path(__file__).parents[1]
        / "webui_new/auth/migrations/0006_memory_stage1.sql"
    ).read_text(encoding="utf-8").upper()

    assert "DROP TABLE" not in migration
    assert "CREATE TABLE IF NOT EXISTS CONVERSATION_SESSIONS" in migration
    assert "CREATE TABLE IF NOT EXISTS CONVERSATION_MESSAGES" in migration
    assert "CREATE TABLE IF NOT EXISTS MEMORY_VERSIONS" in migration
    assert "UNIQUE (USER_ID, REQUEST_ID, ROLE)" in migration
    assert "UNIQUE (SESSION_ID, SEQUENCE_NO)" in migration
    assert "RETENTION_UNTIL" in migration


def test_migration_versions_are_unique():
    migration_dir = Path(__file__).parents[1] / "webui_new/auth/migrations"
    versions = [path.stem.split("_", 1)[0] for path in migration_dir.glob("*.sql")]
    assert len(versions) == len(set(versions))


def test_legacy_answer_migration_versions_are_remapped_before_validation():
    cursor = MigrationCursor([
        ("0006", "0006_answer_documents.sql", "answer-checksum"),
        ("0007", "0007_presentation_documents.sql", "presentation-checksum"),
        ("0008", "0008_user_travel_preferences.sql", "preference-checksum"),
    ])

    assert _remap_legacy_version_collisions(cursor) == 3
    assert set(cursor.rows) == {"0009", "0010", "0011"}
    assert cursor.rows["0009"][1] == "0009_answer_documents.sql"


def test_main_memory_migration_versions_are_not_remapped():
    cursor = MigrationCursor([
        ("0006", "0006_memory_stage1.sql", "memory-checksum"),
        ("0007", "0007_conversation_message_attachments.sql", "attachment-checksum"),
        ("0008", "0008_memory_profile_stage2a.sql", "profile-checksum"),
    ])

    assert _remap_legacy_version_collisions(cursor) == 0
    assert set(cursor.rows) == {"0006", "0007", "0008"}


class BrokenCache:
    def get_recent_context(self, _turns):
        raise ConnectionError("redis unavailable")

    def get_statistics(self):
        raise ConnectionError("redis unavailable")

    def replace_messages(self, *_args, **_kwargs):
        raise ConnectionError("redis unavailable")


class FakeRepository:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.session = SessionRecord(
            session_id=uuid.uuid4(),
            user_id="u1",
            status="active",
            started_at=now,
            last_active_at=now,
            message_count=2,
            last_sequence=2,
            summary_watermark=0,
        )

    def get_session(self, _user_id, _session_id):
        return self.session

    def get_message_version(self, _user_id):
        return 2

    def get_messages(self, _user_id, **_kwargs):
        return [
            {
                "role": "user",
                "content": "从数据库恢复的问题",
                "timestamp": "2026-07-17T10:00:00+00:00",
                "request_id": "r1",
                "turn_id": "t1",
                "sequence_no": 1,
            },
            {
                "role": "assistant",
                "content": "从数据库恢复的回答",
                "timestamp": "2026-07-17T10:00:01+00:00",
                "request_id": "r1",
                "turn_id": "t1",
                "sequence_no": 2,
            },
        ]


def test_recent_context_falls_back_to_postgres_when_redis_is_unavailable():
    service = object.__new__(MemoryService)
    service.user_id = "u1"
    service.session_id = "session-1"
    service.max_turns = 10
    service.cache_backend = "redis"
    service.repository = FakeRepository()
    service._cache = BrokenCache()

    rows = service.get_recent_context(1)

    assert [row["content"] for row in rows] == [
        "从数据库恢复的问题",
        "从数据库恢复的回答",
    ]
