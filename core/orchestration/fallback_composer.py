"""Deterministic AnswerDocument builder used when the Composer LLM fails.

Rendering is data-driven over the seven declarative section kinds; the only
intent-specific knowledge is the kind itself (from the skill's AnswerSpec).
Workflow intermediates suppressed by the skill's ``suppress_agents`` are
folded into the intent's primary section or surfaced as notices.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Dict, Iterable, List

from core.intent_catalog import (
    primary_agent_for_intent,
    section_kind_for_intent,
    suppress_agents_for_intent,
)
from core.presentation import (
    AnswerDocument,
    AnswerItem,
    AnswerSection,
    AnswerSource,
    WeatherDay,
    render_plain_text,
)

from .models import IntentTask, TaskResult

_FORECAST_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})[:：]\s*([^，；;]+?)\s*[，,]\s*"
    r"([^，；;~～]+)\s*[~～]\s*([^，；;°]+)°?C"
    r"(?:[，,]\s*最高降水概率\s*([^；;。]+))?"
)
_CURRENT_PATTERN = re.compile(
    r"当前天气[:：]\s*([^，。]+)[，,]\s*气温\s*([^，。]+)[，,]\s*湿度\s*([^，。]+)"
)

_LABEL_BY_KIND = {
    "policy": "差旅标准",
    "weather": "天气信息",
    "memory": "出行记录",
    "preference": "偏好设置",
    "trip": "行程安排",
    "notice": "合规提示",
    "general": "处理结果",
}


def is_suppressed(result: TaskResult) -> bool:
    """True when the skill's AnswerSpec hides this workflow intermediate agent."""
    return result.agent_name in suppress_agents_for_intent(result.intent)


def renderable_results(results: Iterable[TaskResult]) -> List[TaskResult]:
    return [result for result in results if not is_suppressed(result)]


def context_results(results: Iterable[TaskResult]) -> List[TaskResult]:
    return [result for result in results if is_suppressed(result)]


def _nested(data: Dict) -> Dict:
    return data.get("data") if isinstance(data.get("data"), dict) else data


def _clean(value, limit=4000):
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def _item(label, value, detail=""):
    value = _clean(value, 300)
    if not value:
        return None
    return AnswerItem(label=_clean(label, 60), value=value, detail=_clean(detail, 600))


class FallbackComposer:
    def compose(
        self,
        tasks: Iterable[IntentTask],
        results: Iterable[TaskResult],
    ) -> AnswerDocument:
        task_list = list(tasks)
        result_list = list(results)
        destination = next(
            (
                str(task.entities.get("destination"))
                for task in task_list
                if task.entities.get("destination")
            ),
            "本次出差",
        )
        by_intent: Dict[str, List[TaskResult]] = {}
        for result in result_list:
            by_intent.setdefault(result.intent, []).append(result)

        sections: List[AnswerSection] = []
        notices: List[str] = []
        for task in sorted(task_list, key=lambda item: item.display_order):
            primary = self._primary(by_intent.get(task.intent, []))
            if primary is None:
                continue  # 该意图结果全部被工作流 suppress，不单独成区。
            kind = section_kind_for_intent(task.intent) or "general"
            if primary.status != "success":
                label = _LABEL_BY_KIND.get(kind, "处理结果")
                message = primary.error_message or f"{label}暂时查询失败，请稍后重试。"
                sections.append(AnswerSection(kind=kind, title=label, status="error", body=message))
                notices.append(message)
                continue
            built = self._render(kind, primary)
            sections.extend(built)
            notices.extend(self._secondary_notices(by_intent[task.intent], primary))

        if not sections:
            sections.append(AnswerSection(kind="general", title="处理结果", body="已整理完成。"))

        summary = self._summary(sections)
        document = AnswerDocument(
            title=f"{destination}差旅信息" if destination != "本次出差" else destination,
            summary=summary,
            sections=sections,
            notices=list(dict.fromkeys(notice for notice in notices if notice)),
            sources=self.sources(result_list),
        )
        document.plain_text = render_plain_text(document)
        return document

    @staticmethod
    def _primary(group: List[TaskResult]) -> TaskResult | None:
        renderable = [result for result in group if not is_suppressed(result)]
        if not renderable:
            return None
        intent = renderable[0].intent
        primary_agent = primary_agent_for_intent(intent)
        for result in renderable:
            if result.agent_name == primary_agent:
                return result
        return renderable[-1]  # 终端步骤

    @staticmethod
    def _secondary_notices(group: List[TaskResult], primary: TaskResult) -> List[str]:
        notices = []
        for result in group:
            if result is primary or is_suppressed(result):
                continue
            data = _nested(result.data)
            text = _clean(
                data.get("verdict")
                or data.get("summary")
                or data.get("answer")
                or data.get("message")
                or data.get("conclusion")
                or "",
                800,
            )
            if text:
                notices.append(text)
        return notices

    @classmethod
    def _render(cls, kind: str, result: TaskResult) -> List[AnswerSection]:
        renderer = {
            "policy": cls._policy_section,
            "weather": cls._weather_section,
            "memory": cls._memory_section,
            "preference": cls._preference_section,
            "trip": cls._trip_sections,
            "notice": cls._notice_section,
            "general": cls._general_section,
        }.get(kind)
        if renderer is None:
            return []
        built = renderer(result)
        if isinstance(built, AnswerSection):
            return [built]
        return built or []

    @staticmethod
    def _policy_section(result: TaskResult) -> AnswerSection:
        data = _nested(result.data)
        answer = data.get("answer") or data.get("content") or "已取得适用的差旅制度信息。"
        if isinstance(answer, dict):
            answer = answer.get("answer") or str(answer)
        return AnswerSection(kind="policy", title="差旅标准", body=_clean(answer))

    @staticmethod
    def _weather_section(result: TaskResult) -> AnswerSection:
        payload = _nested(result.data)
        if "results" in payload and isinstance(payload.get("results"), dict):
            payload = payload["results"]
        summary = str(payload.get("summary") or payload.get("message") or "已取得天气信息。")
        items = []
        current = _CURRENT_PATTERN.search(summary)
        if current:
            items.append(AnswerItem(
                label="当前",
                value=f"{current.group(1).strip()} · {current.group(2).strip()}",
                detail=f"湿度 {current.group(3).strip()}",
            ))
        days = [
            WeatherDay(
                date=match.group(1),
                condition=match.group(2).strip(),
                low=f"{match.group(3).strip()}°C",
                high=f"{match.group(4).strip()}°C",
                precipitation=(match.group(5) or "").strip(),
            )
            for match in _FORECAST_PATTERN.finditer(summary)
        ]
        return AnswerSection(
            kind="weather",
            title="目的地天气",
            body="" if items or days else summary,
            items=items,
            days=days,
        )

    @staticmethod
    def _memory_section(result: TaskResult) -> AnswerSection:
        data = _nested(result.data)
        body = _clean(
            data.get("answer") or data.get("response") or data.get("content")
            or "已从出行记录中找到相关信息。"
        )
        return AnswerSection(kind="memory", title="出行记录", body=body)

    @staticmethod
    def _preference_section(result: TaskResult) -> AnswerSection:
        data = _nested(result.data)
        prefs = data.get("preferences") or data.get("preference") or data.get("changes") or []
        if isinstance(prefs, dict):
            prefs = [prefs]
        items = []
        for pref in prefs:
            if not isinstance(pref, dict):
                continue
            value = _clean(pref.get("value") or pref.get("value_text"), 300)
            if not value:
                continue
            label = _clean(pref.get("type") or pref.get("category") or "偏好", 60)
            detail = _clean(pref.get("action") or "", 120) if pref.get("action") not in (None, "", "set") else ""
            items.append(AnswerItem(label=label, value=value, detail=detail))
        body = "已记录您的差旅偏好。" if items else _clean(data.get("answer") or "偏好已更新。")
        return AnswerSection(kind="preference", title="偏好设置", body=body, items=items)

    @staticmethod
    def _trip_sections(result: TaskResult) -> List[AnswerSection]:
        data = _nested(result.data)
        itinerary = data.get("itinerary") if isinstance(data.get("itinerary"), dict) else data
        items = []
        transport = itinerary.get("transport_recommendation")
        if isinstance(transport, dict):
            items.extend(filter(None, [
                _item("首选交通", transport.get("preferred"), transport.get("reason")),
                _item("备选交通", transport.get("alternative"), transport.get("verification")),
            ]))
        elif transport:
            items.append(_item("交通建议", transport))
        items.extend(filter(None, [
            _item("行程时长", itinerary.get("duration")),
            _item("住宿建议", itinerary.get("lodging_advice")),
            _item("预算参考", itinerary.get("estimated_budget")),
        ]))
        title = _clean(itinerary.get("title") or "行程安排", 80)
        sections = [AnswerSection(kind="trip", title=title, items=[item for item in items if item])]

        for index, day in enumerate(itinerary.get("daily_plans") or [], 1):
            if not isinstance(day, dict):
                continue
            slots = day.get("activities") or day.get("time_slots") or []
            day_items = []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                activity = slot.get("activity") or slot.get("location")
                if not activity:
                    continue
                details = " · ".join(
                    _clean(value, 180) for value in (slot.get("description"), slot.get("transport")) if value
                )
                time = _clean(slot.get("time"), 60)
                day_items.append(AnswerItem(
                    label=time or "安排",
                    value=_clean(activity, 300),
                    detail=details,
                ))
            meals = day.get("meals") or {}
            if isinstance(meals, dict):
                for label, value in (("午餐", meals.get("lunch")), ("晚餐", meals.get("dinner"))):
                    if value:
                        day_items.append(AnswerItem(label=label, value=_clean(value, 300), detail=""))
            if not day_items:
                day_text = day.get("summary") or day.get("plan")
                if day_text:
                    day_items.append(AnswerItem(label="计划", value=_clean(day_text, 300), detail=""))
            sections.append(AnswerSection(
                kind="trip",
                title=f"第 {day.get('day') or index} 天",
                items=day_items,
            ))
        return sections

    @staticmethod
    def _notice_section(result: TaskResult) -> AnswerSection:
        data = _nested(result.data)
        body = _clean(
            data.get("verdict") or data.get("summary") or data.get("conclusion")
            or data.get("answer") or "合规检查完成。"
        )
        items = []
        for check in data.get("checks") or []:
            if not isinstance(check, dict):
                continue
            label = _clean(check.get("item") or check.get("rule") or check.get("label") or "检查项", 60)
            status = _clean(check.get("status") or "", 60)
            detail = _clean(check.get("detail") or check.get("message") or "", 600)
            if not status and not detail:
                continue
            items.append(AnswerItem(label=label, value=status or "已检查", detail=detail))
        return AnswerSection(kind="notice", title="合规检查", body=body, items=items)

    @staticmethod
    def _general_section(result: TaskResult) -> AnswerSection:
        data = _nested(result.data)
        body = _clean(
            data.get("response") or data.get("answer") or data.get("content")
            or data.get("message") or ""
        )
        return AnswerSection(kind="general", title="处理结果", body=body)

    @staticmethod
    def _summary(sections: Iterable[AnswerSection]) -> str:
        successful = [section.title for section in sections if section.status == "success"]
        failed = [section.title for section in sections if section.status == "error"]
        if successful and not failed:
            return "已整理好" + "和".join(successful) + "。"
        if successful:
            return "已取得" + "、".join(successful) + "；" + "、".join(failed) + "暂时不可用。"
        return "相关信息暂时未能取得，请稍后重试。"

    @staticmethod
    def sources(results: Iterable[TaskResult]) -> List[AnswerSource]:
        sources = []
        seen = set()
        weather_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        for result in results:
            for source in result.evidence:
                title = str(
                    source.get("title") or source.get("file") or source.get("source") or "数据来源"
                )
                detail = " · ".join(
                    str(value) for value in (
                        source.get("section"),
                        source.get("page") and f"第{source['page']}页",
                    ) if value
                )
                url = str(source.get("url") or "")
                key = (title, detail, url)
                if key in seen:
                    continue
                seen.add(key)
                sources.append(AnswerSource(
                    title=title,
                    detail=detail,
                    url=url,
                    updated_at=weather_time
                    if section_kind_for_intent(result.intent) == "weather"
                    else "",
                ))
        return sources
