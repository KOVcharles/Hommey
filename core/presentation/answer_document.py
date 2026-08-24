"""Semantic answer document rendered as cards, text, or other client surfaces."""
from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

# 结构性上限（单一数据源）：fallback 渲染按 goal / 按行程天数产出分区，
# 上限必须高于任何现实组合，否则长行程 / 多 goal 会把整条管线打崩。
ANSWER_SECTION_CAP = 60
ANSWER_SOURCE_CAP = 30


class TransportLeg(BaseModel):
    """Machine-readable transport facts used by rich ticket/route cards."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["train", "flight", "other"] = "train"
    direction: str = Field(default="", max_length=20)
    service: str = Field(min_length=1, max_length=40)
    origin: str = Field(min_length=1, max_length=80)
    departure_time: str = Field(min_length=1, max_length=40)
    destination: str = Field(min_length=1, max_length=80)
    arrival_time: str = Field(min_length=1, max_length=40)
    duration: str = Field(default="", max_length=40)
    travel_date: str = Field(default="", max_length=40)
    availability: Dict[str, str] = Field(default_factory=dict)


class AnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=600)
    # Additive field: old clients keep rendering value/detail, while rich
    # clients can avoid reparsing model-authored punctuation.
    transport_legs: List[TransportLeg] = Field(default_factory=list, max_length=4)


class WeatherDay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str = Field(min_length=1, max_length=40)
    condition: str = Field(default="", max_length=60)
    low: str = Field(default="", max_length=30)
    high: str = Field(default="", max_length=30)
    precipitation: str = Field(default="", max_length=60)


class AnswerSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["policy", "weather", "memory", "preference", "trip", "notice", "train", "general"]
    goal_id: str = Field(default="", max_length=64)
    title: str = Field(min_length=1, max_length=80)
    status: Literal["success", "partial", "error"] = "success"
    body: str = Field(default="", max_length=4000)
    items: List[AnswerItem] = Field(default_factory=list)
    days: List[WeatherDay] = Field(default_factory=list)


class AnswerSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=500)
    url: str = Field(default="", max_length=1000)
    updated_at: str = Field(default="", max_length=80)


class DepartureCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    detail: str = Field(default="", max_length=500)
    status: Literal["ready", "pending", "required", "optional"]
    status_label: str = Field(min_length=1, max_length=30)
    action_label: str = Field(default="", max_length=30)
    action_value: str = Field(default="", max_length=300)


class DepartureWeather(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(default="", max_length=60)
    temperature: str = Field(default="", max_length=60)
    humidity: str = Field(default="", max_length=60)
    advice: str = Field(default="", max_length=500)
    preparation: List[str] = Field(default_factory=list, max_length=8)


class PreDepartureChecklist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="出发前确认", min_length=1, max_length=80)
    summary: str = Field(default="关键事项确认后，行程和报销会更稳妥。", max_length=200)
    pending_count: int = Field(default=0, ge=0)
    critical_items: List[DepartureCheckItem] = Field(default_factory=list, max_length=8)
    weather: DepartureWeather | None = None
    reimbursement_items: List[DepartureCheckItem] = Field(default_factory=list, max_length=12)


class RetrievalPresentation(BaseModel):
    """User-facing summary of an explicitly requested retrieval mode."""

    model_config = ConfigDict(extra="forbid")

    requested_mode: Literal["enhanced"] = "enhanced"
    effective_mode: Literal["standard", "enhanced"] = "standard"
    status: Literal["enhanced", "fallback"] = "fallback"
    fallback_reason: str = Field(default="", max_length=120)


class AnswerDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(default="", max_length=500)
    sections: List[AnswerSection] = Field(min_length=1, max_length=ANSWER_SECTION_CAP)
    notices: List[str] = Field(default_factory=list, max_length=10)
    sources: List[AnswerSource] = Field(default_factory=list, max_length=ANSWER_SOURCE_CAP)
    pre_departure: PreDepartureChecklist | None = None
    retrieval: RetrievalPresentation | None = None
    plain_text: str = Field(default="", max_length=12000)


def render_plain_text(document: AnswerDocument) -> str:
    """Render a stable text representation for memory, export, and old clients."""
    lines = [document.title]
    if document.summary:
        lines.extend(["", document.summary])
    for section in document.sections:
        lines.extend(["", section.title])
        if section.body:
            lines.append(section.body)
        for item in section.items:
            value = f"{item.label}：{item.value}"
            if item.detail:
                value += f"（{item.detail}）"
            lines.append(f"- {value}")
        for day in section.days:
            temperatures = "～".join(value for value in (day.low, day.high) if value)
            values = [day.date, day.condition, temperatures, day.precipitation]
            lines.append("- " + "，".join(value for value in values if value))
    if document.pre_departure:
        checklist = document.pre_departure
        lines.extend(["", checklist.title])
        for item in checklist.critical_items:
            lines.append(f"- [{item.status_label}] {item.label}：{item.detail}")
        if checklist.weather:
            weather = checklist.weather
            weather_values = [weather.condition, weather.temperature, weather.humidity]
            lines.append("- 天气准备：" + "，".join(value for value in weather_values if value))
            if weather.advice:
                lines.append(f"  {weather.advice}")
            if weather.preparation:
                lines.append("  建议携带：" + "、".join(weather.preparation))
        for item in checklist.reimbursement_items:
            lines.append(f"- [{item.status_label}] {item.label}：{item.detail}")
    if document.notices:
        lines.extend(["", "提醒"])
        lines.extend(f"- {notice}" for notice in document.notices)
    if document.sources:
        lines.extend(["", "来源与更新时间"])
        for source in document.sources:
            values = [source.title, source.detail, source.updated_at, source.url]
            lines.append("- " + " · ".join(value for value in values if value))
    return "\n".join(lines).strip()
