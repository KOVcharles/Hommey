"""Unit tests for the PostgreSQL long-term-memory compatibility adapter."""

from context.memory_repository import PostgresCompatibilityStore


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return {"trip_id": "trip-test"}


class RecordingTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingConnection:
    def __init__(self):
        self.cursor_obj = RecordingCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_obj

    def transaction(self):
        return RecordingTransaction()


class RecordingPool:
    def __init__(self):
        self.connection_obj = RecordingConnection()

    def connection(self):
        return self.connection_obj


def _memory_with_fake_pool():
    repository = type("Repository", (), {"pool": RecordingPool()})()
    return PostgresCompatibilityStore("test-user", repository)


def test_postgres_trip_stats_skip_destination_frequency_when_destination_missing():
    memory = _memory_with_fake_pool()

    memory.save_trip_history({"origin": "北京", "destination": None})

    stats_sql, stats_params = memory.pool.connection_obj.cursor_obj.calls[1]
    assert "jsonb_set" not in stats_sql
    assert stats_params == ("test-user",)


def test_postgres_trip_stats_update_destination_frequency_when_destination_present():
    memory = _memory_with_fake_pool()

    memory.save_trip_history({"origin": "北京", "destination": "南京"})

    stats_sql, stats_params = memory.pool.connection_obj.cursor_obj.calls[1]
    assert "jsonb_set" in stats_sql
    assert stats_params[0] == "test-user"
    assert stats_params[2:] == ("南京", "南京")
