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
    if data.get("work_schedule"):
        lines.append(f"工作时间：{data['work_schedule']}")

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
