"""Unit tests for the stage-2A profile catalog and additive migration."""
from pathlib import Path

import pytest

from context.profile_catalog import (
    InvalidProfileValue,
    UnknownProfileField,
    normalize_profile_value,
)


def test_scalar_profile_values_are_trimmed_and_compared_case_insensitively():
    normalized = normalize_profile_value("seat_preference", "  靠窗  ")

    assert normalized.value == "靠窗"
    assert normalized.comparable == "靠窗"


def test_list_profile_values_are_deduplicated_without_losing_display_order():
    first = normalize_profile_value("hotel_brands", [" 汉庭 ", "如家", "汉庭"])
    reordered = normalize_profile_value("hotel_brands", ["如家", "汉庭"])

    assert first.value == ["汉庭", "如家"]
    assert first.comparable == reordered.comparable


@pytest.mark.parametrize(
    "fact_key,value,error_type",
    [
        ("unknown_field", "value", UnknownProfileField),
        ("seat_preference", ["靠窗"], InvalidProfileValue),
        ("home_location", "杭州市文一路123号", InvalidProfileValue),
        ("hotel_brands", [], InvalidProfileValue),
    ],
)
def test_profile_catalog_fails_closed_for_unknown_unsafe_or_invalid_values(
    fact_key, value, error_type
):
    with pytest.raises(error_type):
        normalize_profile_value(fact_key, value)


def test_stage2a_migration_is_additive_and_enforces_single_active_state():
    migration = (
        Path(__file__).parents[1]
        / "webui_new/auth/migrations/0008_memory_profile_stage2a.sql"
    ).read_text(encoding="utf-8").upper()

    assert "DROP TABLE" not in migration
    assert "CREATE TABLE IF NOT EXISTS USER_PROFILE_FACTS" in migration
    assert "CREATE TABLE IF NOT EXISTS MEMORY_CHANGE_REQUESTS" in migration
    assert "WHERE STATUS = 'ACTIVE'" in migration
    assert "WHERE STATUS = 'PENDING'" in migration
    assert "SOURCE_TURN_ID UUID NOT NULL" in migration
    assert "USER_PREFERENCES" in migration  # The compatibility table is explicitly retained.
