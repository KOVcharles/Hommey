"""Shared schema helpers for the typed one-row travel preference store."""
from __future__ import annotations

import json
from typing import Any, Mapping


PREFERENCE_SCALAR_COLUMNS = {
    "home_location": "home_location",
    "transportation_preference": "transportation_preference",
    "seat_preference": "seat_preference",
    "meal_preference": "meal_preference",
    "budget_level": "budget_level",
}

PREFERENCE_LIST_COLUMNS = {
    "hotel_brands": "hotel_brands",
    "airlines": "airlines",
}


def normalize_scalar_preference(value: Any) -> str:
    """Normalize a core scalar preference without discarding legacy lists."""
    if isinstance(value, (list, tuple, set)):
        parts = [str(item).strip() for item in value if str(item).strip()]
        normalized = "、".join(dict.fromkeys(parts))
    elif isinstance(value, dict):
        normalized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        normalized = str(value).strip()
    if not normalized:
        raise ValueError("Core preference value cannot be empty")
    return normalized


def normalize_list_preference(value: Any) -> list[Any]:
    """Normalize list preferences while preserving insertion order."""
    values = list(value) if isinstance(value, (list, tuple, set)) else [value]
    normalized: list[Any] = []
    seen: set[str] = set()
    for item in values:
        clean_item = item.strip() if isinstance(item, str) else item
        if clean_item in (None, ""):
            continue
        marker = json.dumps(clean_item, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            normalized.append(clean_item)
    return normalized


def preference_row_to_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one typed preference row into the legacy mapping API."""
    preferences = dict(row.get("extra_preferences") or {})
    for key in PREFERENCE_SCALAR_COLUMNS:
        if row.get(key) is not None:
            preferences[key] = row[key]
    for key in PREFERENCE_LIST_COLUMNS:
        if row.get(key):
            preferences[key] = row[key]
    return preferences
