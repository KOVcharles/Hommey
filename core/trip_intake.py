"""Deterministic business-trip intake rules shared across agents and clients."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class TripFieldSpec:
    key: str
    label: str
    required: bool
    priority: int
    input_type: str = "text"
    help_text: str = ""
    examples: tuple[str, ...] = ()
    options: tuple[str, ...] = ()


FIELD_SPECS: Dict[str, TripFieldSpec] = {
    "origin": TripFieldSpec(
        "origin", "出发地", True, 10, "location", "通常从哪座城市出发", ("北京", "上海"),
    ),
    "destination": TripFieldSpec(
        "destination", "目的地", True, 20, "location", "本次出差前往哪里", ("南京", "杭州"),
    ),
    "start_date": TripFieldSpec(
        "start_date", "出发日期", True, 30, "date", "告诉我日期或相对时间", ("8月5日", "下周一上午"),
    ),
    "trip_length": TripFieldSpec(
        "trip_length", "行程时长", True, 40, "duration_or_date",
        "填写出差天数，或直接告诉我返程日期", ("2天", "8月7日返程"),
        ("1天", "2天", "3天"),
    ),
    "trip_purpose": TripFieldSpec(
        "trip_purpose", "出差目的", True, 50, "choice", "这次出差主要要完成什么工作",
        ("拜访客户", "参加会议"), ("客户拜访", "参加会议", "内部协作", "培训"),
    ),
    "work_location": TripFieldSpec(
        "work_location", "客户或会议地点", False, 60, "location", "用于优化酒店区域和通勤安排",
        ("南京新街口某客户办公室",),
    ),
    "work_schedule": TripFieldSpec(
        "work_schedule", "会面及工作时间", False, 70, "text", "用于安排交通缓冲和每日计划",
        ("8月6日 10:00 开会",),
    ),
}

REQUIRED_KEYS = ("origin", "destination", "start_date", "trip_length", "trip_purpose")
OPTIONAL_KEYS = ("work_location", "work_schedule")


def remove_ungrounded_trip_locations(
    data: Dict[str, Any],
    trusted_sources: Iterable[Any],
) -> List[str]:
    """Clear origin/destination values absent from authoritative current context.

    The caller decides which sources are authoritative.  For a new turn these are
    the current user message, prior user messages in the same session, and the
    still-active trip's existing origin/destination fields—not preferences or
    completed-trip history.
    """
    trusted_text = "\n".join(str(value) for value in trusted_sources if value)
    rejected: List[str] = []
    for key in ("origin", "destination"):
        value = data.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized and normalized not in trusted_text:
            data[key] = None
            rejected.append(key)
    return rejected


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def _positive_duration(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def evaluate_trip_intake(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate readiness without trusting an LLM-produced missing-info list."""
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    invalid_fields: List[Dict[str, str]] = []
    conflicts: List[Dict[str, Any]] = []

    start_value = data.get("start_date")
    end_value = data.get("end_date")
    start = _parse_date(start_value)
    end = _parse_date(end_value)
    duration = _positive_duration(data.get("duration_days"))

    if start_value and start is None:
        invalid_fields.append({
            "key": "start_date",
            "message": "没有识别出明确的出发日期，请换一种日期表达。",
        })
    if end_value and end is None:
        invalid_fields.append({
            "key": "trip_length",
            "message": "没有识别出明确的返程日期，请填写日期或出差天数。",
        })
    if data.get("duration_days") not in (None, "") and duration is None:
        invalid_fields.append({
            "key": "trip_length",
            "message": "出差天数需要是大于 0 的整数。",
        })
    if start and end and end < start:
        conflicts.append({
            "key": "trip_length",
            "message": "返程日期早于出发日期，请确认行程时间。",
            "values": [str(start_value), str(end_value)],
        })
    if start and end and duration:
        calendar_days = (end - start).days + 1
        if calendar_days != duration:
            conflicts.append({
                "key": "trip_length",
                "message": f"日期跨度为 {calendar_days} 天，与填写的 {duration} 天不一致。",
                "values": [f"按日期计算 {calendar_days} 天", f"保留 {duration} 天"],
            })
    origin = str(data.get("origin") or "").strip()
    destination = str(data.get("destination") or "").strip()
    if origin and destination and origin == destination:
        conflicts.append({
            "key": "destination",
            "message": "出发地和目的地相同，请确认本次出差目的地。",
            "values": [origin, destination],
        })

    invalid_keys = {item["key"] for item in invalid_fields}
    conflict_keys = {item["key"] for item in conflicts}
    completed = {
        "origin": bool(origin),
        "destination": bool(destination),
        "start_date": bool(start_value) and "start_date" not in invalid_keys,
        "trip_length": bool(end_value or data.get("duration_days")) and "trip_length" not in invalid_keys,
        "trip_purpose": bool(str(data.get("trip_purpose") or "").strip()),
    }
    for key in conflict_keys:
        if key in completed:
            completed[key] = False

    missing_required = [key for key in REQUIRED_KEYS if not completed[key]]
    legacy_missing = [
        "duration_days_or_end_date" if key == "trip_length" else key
        for key in missing_required
        if key not in invalid_keys and key not in conflict_keys
    ]
    missing_optional = [key for key in OPTIONAL_KEYS if not data.get(key)]
    completed_count = sum(1 for key in REQUIRED_KEYS if completed[key])

    return {
        "missing_info": legacy_missing,
        "missing_required": missing_required,
        "optional_info": missing_optional,
        "invalid_fields": invalid_fields,
        "conflicts": conflicts,
        "completion": {"completed": completed_count, "total": len(REQUIRED_KEYS)},
        "planning_ready": completed_count == len(REQUIRED_KEYS) and not invalid_fields and not conflicts,
    }
