"""Preserve the historical summary migration for existing databases."""

from pathlib import Path


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
