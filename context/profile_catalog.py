"""Canonical field definitions and validation for versioned profile memory."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from utils.memory_safety import is_safe_preference_value


class UnknownProfileField(ValueError):
    """The caller attempted to persist a field outside the reviewed catalog."""


class InvalidProfileValue(ValueError):
    """The value does not satisfy the field's type or safety boundary."""


@dataclass(frozen=True)
class ProfileFieldDefinition:
    """Stable storage rules for one supported profile field."""

    namespace: str
    value_kind: Literal["scalar", "list"]
    max_length: int = 120
    max_items: int = 12


@dataclass(frozen=True)
class NormalizedProfileValue:
    """JSON value to store plus a deterministic comparison representation."""

    value: str | list[str]
    comparable: str


# Only reviewed, non-sensitive travel profile fields enter the versioned store.
PROFILE_FIELDS: dict[str, ProfileFieldDefinition] = {
    "home_location": ProfileFieldDefinition("profile", "scalar"),
    "usual_departure": ProfileFieldDefinition("profile", "scalar"),
    "transportation_preference": ProfileFieldDefinition("travel.preference", "scalar"),
    "hotel_brands": ProfileFieldDefinition("travel.preference", "list"),
    "hotel_area_preference": ProfileFieldDefinition("travel.preference", "scalar"),
    "airlines": ProfileFieldDefinition("travel.preference", "list"),
    "seat_preference": ProfileFieldDefinition("travel.preference", "scalar"),
    "meal_preference": ProfileFieldDefinition("travel.preference", "scalar"),
    "budget_level": ProfileFieldDefinition("travel.preference", "scalar"),
    "time_preference": ProfileFieldDefinition("travel.preference", "scalar"),
    "food_preference": ProfileFieldDefinition("travel.preference", "scalar"),
}


def normalize_profile_value(fact_key: str, value: Any) -> NormalizedProfileValue:
    """Validate and normalize one explicit profile value without inferring facts."""
    definition = PROFILE_FIELDS.get(str(fact_key or "").strip())
    if definition is None:
        raise UnknownProfileField(f"Unsupported profile field: {fact_key}")
    if not is_safe_preference_value(value):
        raise InvalidProfileValue("Sensitive information cannot be stored as a profile fact")

    if definition.value_kind == "list":
        raw_items = value if isinstance(value, (list, tuple)) else [value]
        if not raw_items or len(raw_items) > definition.max_items:
            raise InvalidProfileValue(
                f"{fact_key} must contain between 1 and {definition.max_items} items"
            )
        items = _clean_unique_strings(raw_items, max_length=definition.max_length)
        if not items:
            raise InvalidProfileValue(f"{fact_key} cannot be empty")
        comparable = json.dumps(
            sorted(item.casefold() for item in items),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return NormalizedProfileValue(items, comparable)

    if not isinstance(value, str):
        raise InvalidProfileValue(f"{fact_key} must be a string")
    clean_value = _clean_string(value, max_length=definition.max_length)
    return NormalizedProfileValue(clean_value, clean_value.casefold())


def get_profile_field(fact_key: str) -> ProfileFieldDefinition:
    """Return a catalog entry or fail closed for unknown fields."""
    try:
        return PROFILE_FIELDS[fact_key]
    except KeyError as exc:
        raise UnknownProfileField(f"Unsupported profile field: {fact_key}") from exc


def _clean_unique_strings(values: list[Any] | tuple[Any, ...], *, max_length: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise InvalidProfileValue("Profile list values must contain only strings")
        clean_value = _clean_string(value, max_length=max_length)
        key = clean_value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(clean_value)
    return result


def _clean_string(value: str, *, max_length: int) -> str:
    clean_value = re.sub(r"\s+", " ", value).strip()
    if not clean_value:
        raise InvalidProfileValue("Profile values cannot be empty")
    if len(clean_value) > max_length:
        raise InvalidProfileValue(f"Profile values cannot exceed {max_length} characters")
    return clean_value
