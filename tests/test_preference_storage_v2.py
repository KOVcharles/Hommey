from context.long_term_memory import LegacyAutocommitPostgresLongTermMemory
from context.memory_repository import PostgresCompatibilityStore


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))

    def fetchone(self):
        return self.connection.fetchone_result

    def fetchall(self):
        return self.connection.fetchall_result


class RecordingTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingConnection:
    def __init__(self, *, fetchone_result=None, fetchall_result=None):
        self.calls = []
        self.fetchone_result = fetchone_result
        self.fetchall_result = fetchall_result or []

    def cursor(self):
        return RecordingCursor(self)

    def transaction(self):
        return RecordingTransaction()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingPool:
    def __init__(self, connection):
        self._connection = connection

    def connection(self):
        return self._connection


class RepositoryStub:
    def __init__(self, connection):
        self.pool = RecordingPool(connection)


def _memory(connection=None):
    memory = object.__new__(LegacyAutocommitPostgresLongTermMemory)
    memory.user_id = "7"
    memory.conn = connection or RecordingConnection()
    memory._jsonb = lambda value: value
    return memory


def test_migration_builds_one_row_per_user_without_jsonb_max():
    sql = (
        __import__("pathlib").Path("webui_new/auth/migrations/0011_user_travel_preferences.sql")
        .read_text(encoding="utf-8")
    )

    assert "CREATE TABLE IF NOT EXISTS user_travel_preferences" in sql
    assert "user_id TEXT PRIMARY KEY" in sql
    assert "jsonb_object_agg(pref_type, pref_value)" in sql
    assert "extra_preferences JSONB" in sql
    assert "MAX(pref_value)" not in sql


def test_save_scalar_preference_writes_wide_table_and_legacy_mirror():
    memory = _memory()

    memory.save_preference("home_location", "北京")

    assert len(memory.conn.calls) == 2
    wide_sql, wide_params = memory.conn.calls[0]
    legacy_sql, legacy_params = memory.conn.calls[1]
    assert "INSERT INTO user_travel_preferences" in wide_sql
    assert "home_location" in wide_sql
    assert wide_params == ("7", "北京", "home_location")
    assert "INSERT INTO user_preferences" in legacy_sql
    assert legacy_params == ("7", "home_location", "北京")


def test_save_list_preference_normalizes_and_deduplicates_values():
    memory = _memory()

    memory.save_preference("hotel_brands", ["全季", "亚朵", "全季", ""])

    wide_sql, wide_params = memory.conn.calls[0]
    assert "hotel_brands" in wide_sql
    assert wide_params == ("7", ["全季", "亚朵"], "hotel_brands")
    assert memory.conn.calls[1][1] == ("7", "hotel_brands", ["全季", "亚朵"])


def test_save_unknown_preference_uses_jsonb_extension_and_legacy_mirror():
    memory = _memory()

    memory.save_preference("food", "不吃辣")

    wide_sql, wide_params = memory.conn.calls[0]
    assert "extra_preferences" in wide_sql
    assert wide_params == ("7", "food", "不吃辣", "food")
    assert memory.conn.calls[1][1] == ("7", "food", "不吃辣")


def test_get_preferences_merges_typed_columns_and_extensions():
    connection = RecordingConnection(
        fetchone_result={
            "home_location": "北京",
            "transportation_preference": "高铁",
            "hotel_brands": ["全季"],
            "airlines": [],
            "seat_preference": "商务座",
            "meal_preference": None,
            "budget_level": None,
            "extra_preferences": {"food": "不吃辣"},
        }
    )
    memory = _memory(connection)

    preferences = memory.get_preference()

    assert preferences == {
        "food": "不吃辣",
        "home_location": "北京",
        "transportation_preference": "高铁",
        "seat_preference": "商务座",
        "hotel_brands": ["全季"],
    }
    assert memory.get_preference("home_location") == "北京"


def test_get_preferences_falls_back_to_legacy_row_when_wide_row_missing():
    connection = RecordingConnection(
        fetchone_result=None,
        fetchall_result=[{"pref_type": "home_location", "pref_value": "广州"}],
    )
    memory = _memory(connection)

    assert memory.get_preference() == {"home_location": "广州"}
    assert "FROM user_preferences" in connection.calls[1][0]


def test_pooled_runtime_store_writes_typed_row_and_legacy_mirror():
    connection = RecordingConnection()
    store = PostgresCompatibilityStore("7", RepositoryStub(connection))

    store.save_preference("home_location", "北京")

    assert len(connection.calls) == 2
    assert "INSERT INTO user_travel_preferences" in connection.calls[0][0]
    assert connection.calls[0][1] == ("7", "北京", "home_location")
    assert "INSERT INTO user_preferences" in connection.calls[1][0]


def test_pooled_runtime_store_reads_typed_row():
    connection = RecordingConnection(
        fetchone_result={
            "home_location": "北京",
            "transportation_preference": "高铁",
            "hotel_brands": ["亚朵"],
            "airlines": [],
            "seat_preference": "商务座",
            "meal_preference": None,
            "budget_level": None,
            "extra_preferences": {"food": "不吃辣"},
        }
    )
    store = PostgresCompatibilityStore("7", RepositoryStub(connection))

    preferences = store.get_preference()

    assert preferences["home_location"] == "北京"
    assert preferences["hotel_brands"] == ["亚朵"]
    assert preferences["food"] == "不吃辣"
