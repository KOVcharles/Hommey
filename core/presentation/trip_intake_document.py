"""Structured presentation contract for incomplete business-trip intake."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.trip_intake import FIELD_SPECS, OPTIONAL_KEYS, REQUIRED_KEYS, evaluate_trip_intake


class TripRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = ""
    destination: str = ""


class TripProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: int = Field(ge=0)
    total: int = Field(gt=0)


class TripCollectedField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    value: str
    source: Literal["user", "memory", "inferred"] = "user"


class TripFieldPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str
    input_type: str
    help_text: str = ""
    examples: List[str] = Field(default_factory=list)
    options: List[str] = Field(default_factory=list)
    error: str = ""


class TripConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    message: str
    values: List[str] = Field(default_factory=list)


class TripIntakeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["trip_intake"] = "trip_intake"
    version: Literal["1.0"] = "1.0"
    status: Literal["collecting_required", "needs_clarification", "ready_to_plan"]
    title: str
    summary: str
    route: TripRoute
    progress: TripProgress
    collected: List[TripCollectedField] = Field(default_factory=list)
    missing_required: List[TripFieldPrompt] = Field(default_factory=list)
    optional: List[TripFieldPrompt] = Field(default_factory=list)
    conflicts: List[TripConflict] = Field(default_factory=list)
    next_question: str = ""
    input_placeholder: str = ""
    suggested_reply: str = ""
    plain_text: str = ""


_VALUE_KEYS = {
    "origin": "origin",
    "destination": "destination",
    "start_date": "start_date",
    "trip_length": "duration_days",
    "trip_purpose": "trip_purpose",
    "work_location": "work_location",
    "work_schedule": "work_schedule",
}


def _field_prompt(key: str, error: str = "") -> TripFieldPrompt:
    spec = FIELD_SPECS[key]
    return TripFieldPrompt(
        key=key,
        label=spec.label,
        input_type=spec.input_type,
        help_text=spec.help_text,
        examples=list(spec.examples),
        options=list(spec.options),
        error=error,
    )


def _suggested_reply(missing: List[str]) -> str:
    examples = {
        "origin": "从北京出发",
        "destination": "去南京",
        "start_date": "8月5日出发",
        "trip_length": "出差2天",
        "trip_purpose": "参加客户会议",
    }
    return "，".join(examples[key] for key in missing if key in examples)


def build_trip_intake_document(raw: Dict[str, Any]) -> TripIntakeDocument:
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    state = evaluate_trip_intake(data)
    missing = state["missing_required"]
    errors = {item["key"]: item["message"] for item in state["invalid_fields"]}
    for conflict in state["conflicts"]:
        errors.setdefault(conflict["key"], conflict["message"])

    collected = []
    for key in (*REQUIRED_KEYS, *OPTIONAL_KEYS):
        if key in missing:
            continue
        value_key = _VALUE_KEYS[key]
        value = data.get(value_key)
        if key == "trip_length" and not value:
            value = data.get("end_date") and f"{data['end_date']} 返程"
        elif key == "trip_length" and value:
            value = f"{value} 天"
        if value:
            collected.append(TripCollectedField(
                key=key,
                label=FIELD_SPECS[key].label,
                value=str(value),
                source="memory" if key == "origin" and data.get("origin_inferred") else "user",
            ))

    prompts = [_field_prompt(key, errors.get(key, "")) for key in missing]
    optional = [_field_prompt(key) for key in state["optional_info"]]
    completed = state["completion"]["completed"]
    total = state["completion"]["total"]
    if state["conflicts"] or state["invalid_fields"]:
        status = "needs_clarification"
        title = "有一项行程信息需要确认"
        summary = "修正后我会继续生成详细方案。"
    elif state["planning_ready"]:
        status = "ready_to_plan"
        title = "行程信息已齐全"
        summary = "正在继续生成详细的公司差旅方案。"
    else:
        status = "collecting_required"
        title = "行程框架已保存"
        summary = f"还差 {len(missing)} 项，即可生成详细方案。"

    next_question = prompts[0].help_text if prompts else ""
    placeholder = (
        f"请补充{prompts[0].label}，也可以一次说完所有信息"
        if prompts else "继续问 Hommey"
    )
    document = TripIntakeDocument(
        status=status,
        title=title,
        summary=summary,
        route=TripRoute(
            origin=str(data.get("origin") or ""),
            destination=str(data.get("destination") or ""),
        ),
        progress=TripProgress(completed=completed, total=total),
        collected=collected,
        missing_required=prompts,
        optional=optional,
        conflicts=[TripConflict.model_validate(item) for item in state["conflicts"]],
        next_question=next_question,
        input_placeholder=placeholder,
        suggested_reply=_suggested_reply(missing),
    )
    document.plain_text = render_trip_intake_text(document)
    return document


def render_trip_intake_text(document: TripIntakeDocument) -> str:
    lines = [document.title]
    route = " → ".join(value for value in (document.route.origin, document.route.destination) if value)
    if route:
        lines.append(route)
    lines.append(f"已完成 {document.progress.completed}/{document.progress.total} 项。{document.summary}")
    if document.collected:
        lines.append("已确认：" + "；".join(f"{item.label}：{item.value}" for item in document.collected))
    if document.missing_required:
        lines.append("需要补充：")
        for item in document.missing_required:
            detail = item.error or item.help_text
            lines.append(f"- {item.label}：{detail}")
    if document.optional:
        lines.append("可选补充：" + "、".join(item.label for item in document.optional))
    if document.suggested_reply:
        lines.append(f"可以直接回复：{document.suggested_reply}")
    return "\n".join(lines)


_HISTORY_LABEL_KEYS = {
    "出发地": "origin",
    "目的地": "destination",
    "出发日期": "start_date",
    "行程时长": "duration_days",
    "出差目的": "trip_purpose",
    "客户或会议地点": "work_location",
    "会面及工作时间": "work_schedule",
}


def recover_trip_intake_document(text: str) -> TripIntakeDocument | None:
    """Recover only the stable intake text emitted before migration 0016."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines or lines[0] not in {
        "行程框架已保存", "有一项行程信息需要确认", "行程信息已齐全",
    }:
        return None

    raw: Dict[str, Any] = {}
    route = next((line for line in lines[1:3] if "→" in line), "")
    if route:
        origin, destination = (part.strip() for part in route.split("→", 1))
        raw.update(origin=origin, destination=destination)

    confirmed = next((line for line in lines if line.startswith("已确认：")), "")
    parts = confirmed.removeprefix("已确认：").split("；") if confirmed else []
    for part in parts:
        label, separator, value = part.partition("：")
        key = _HISTORY_LABEL_KEYS.get(label.strip())
        value = value.strip()
        if not separator or not key or not value:
            continue
        if key == "duration_days":
            duration = re.search(r"(\d+)\s*天", value)
            if duration:
                raw[key] = int(duration.group(1))
            elif "返程" in value:
                raw["end_date"] = value.split()[0]
        else:
            raw[key] = value

    if not raw.get("origin") and not raw.get("destination"):
        return None
    return build_trip_intake_document(raw)
