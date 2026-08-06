"""Unit tests for incremental session summaries (no database)."""

from pathlib import Path

from context.memory_repository import PostgresMemoryRepository


def _accumulate(rows, max_turns, max_chars):
    return PostgresMemoryRepository._accumulate_segment(
        list(rows), max_turns * 2, max_chars
    )


def test_accumulate_below_both_thresholds_returns_none():
    rows = [(i, "user", "x" * 50) for i in range(1, 9)]  # 8 msgs, 400 chars
    assert _accumulate(rows, max_turns=5, max_chars=6000) is None


def test_accumulate_stops_at_max_messages():
    rows = [(i, "user", "x") for i in range(1, 13)]  # 12 msgs, 12 chars
    chosen = _accumulate(rows, max_turns=5, max_chars=6000)
    assert chosen is not None
    assert len(chosen) == 10  # 5 turns * 2 messages
    assert chosen[-1][0] == 10


def test_accumulate_stops_at_max_chars():
    rows = [(i, "user", "x" * 2000) for i in range(1, 6)]  # each 2000 chars
    chosen = _accumulate(rows, max_turns=5, max_chars=6000)
    assert chosen is not None
    assert len(chosen) == 3  # 3 * 2000 = 6000 >= 6000
    assert chosen[-1][0] == 3


def test_accumulate_single_large_message_is_its_own_segment():
    rows = [(1, "user", "x" * 8000)]
    chosen = _accumulate(rows, max_turns=5, max_chars=6000)
    assert chosen is not None
    assert len(chosen) == 1
    assert chosen[0][0] == 1


def test_accumulate_preserves_message_order():
    rows = [(5, "user", "a"), (6, "assistant", "b"), (7, "user", "c"), (8, "assistant", "d")]
    chosen = _accumulate(rows, max_turns=2, max_chars=1000)
    assert [row[0] for row in chosen] == [5, 6, 7, 8]


def test_migration_0012_defines_summary_table_without_drops():
    migration = (
        Path(__file__).parents[1]
        / "webui_new/auth/migrations/0012_session_summaries.sql"
    ).read_text(encoding="utf-8").upper()

    assert "DROP TABLE" not in migration
    assert "CREATE TABLE IF NOT EXISTS SESSION_SUMMARIES" in migration
    assert (
        "UNIQUE (SESSION_ID, SOURCE_SEQUENCE_FROM, SOURCE_SEQUENCE_TO)"
        in migration
    )
    assert "SOURCE_SEQUENCE_FROM" in migration
    assert "SUMMARY_TEXT" in migration
