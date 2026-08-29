"""Small adapter from quick-trip fields to the ordinary chat workflow."""
from __future__ import annotations

from typing import Any


_CAPABILITY_LABELS = {
    "weather": "天气",
    "local_transport": "市内交通",
    "train": "高铁车次",
    "nearby_hotels": "工作地点附近酒店",
}


def build_quick_trip_message(
    trip_input: dict[str, Any], capability_selection: dict[str, list[str]] | None = None,
) -> str:
    """Create a grounded, readable utterance consumed by existing intent logic."""
    data = dict(trip_input or {})
    lines = [
        "请为我规划这次公司差旅。",
        f"出发地：{data.get('origin', '')}",
        f"目的地：{data.get('destination', '')}",
        f"出发日期：{data.get('start_date', '')}",
        f"返程日期：{data.get('end_date', '')}",
        f"行程天数：{data.get('duration_days', '')}天",
        f"出差目的：{data.get('trip_purpose', '')}",
    ]
    if data.get("work_location"):
        lines.append(f"工作地点：{data['work_location']}")
    if data.get("work_location_note"):
        lines.append(f"地点备注：{data['work_location_note']}")

    selection = capability_selection or {}
    included = [
        _CAPABILITY_LABELS[item] for item in selection.get("include", [])
        if item in _CAPABILITY_LABELS
    ]
    excluded = [
        _CAPABILITY_LABELS[item] for item in selection.get("exclude", [])
        if item in _CAPABILITY_LABELS
    ]
    if included:
        lines.append("请查询：" + "、".join(included) + "。")
    if excluded:
        lines.append("不需要查询：" + "、".join(excluded) + "。")
    return "\n".join(lines)


def inject_trip_entities(intention_data: dict[str, Any], trip_input: dict[str, Any]) -> None:
    """Give the authorized trip Goal the validated form facts at highest precedence."""
    facts = {
        key: value for key, value in dict(trip_input or {}).items()
        if key not in {"work_location_place_id", "work_location_note"} and value not in (None, "")
    }
    if not facts:
        return
    for group in intention_data.get("groups") or []:
        if group.get("intent") == "business_trip_planning":
            group.setdefault("entities", {}).update(facts)
    intention_data.setdefault("key_entities", {}).update(facts)
