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
    ANSWER_SECTION_CAP,
    ANSWER_SOURCE_CAP,
    AnswerDocument,
    AnswerItem,
    AnswerSection,
    AnswerSource,
    TransportLeg,
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
_TRAIN_NO_PATTERN = re.compile(r"(?<![A-Z0-9])([GCDZTKLS]\d{1,5})(?!\d)", re.IGNORECASE)
_TRANSPORT_ITEM_LABELS = {"首选交通", "交通建议"}

_LABEL_BY_KIND = {
    "policy": "差旅标准",
    "weather": "天气信息",
    "memory": "出行记录",
    "preference": "偏好设置",
    "trip": "行程安排",
    "notice": "合规提示",
    "train": "车次信息",
    "general": "处理结果",
}

_VERDICT_LABELS = {
    "compliant": "符合制度要求",
    "non_compliant": "存在不合规项",
    "partial": "部分事项仍需核验",
    "unknown": "制度证据不足，暂时无法确认合规性",
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


def _train_payload(result: TaskResult) -> Dict:
    payload = _nested(result.data)
    if isinstance(payload.get("results"), dict):
        payload = payload["results"]
    return payload


def _train_rows(results: Iterable[TaskResult]) -> List[Dict]:
    """Collect normalized 12306 rows without duplicating round-trip aliases."""
    rows: List[Dict] = []
    seen = set()
    for result in results:
        if result.agent_name != "train_query" or result.status != "success":
            continue
        payload = _train_payload(result)
        candidates = payload.get("trains")
        if not isinstance(candidates, list):
            candidates = []
            for key in ("outbound", "return_trip"):
                leg_payload = payload.get(key)
                if isinstance(leg_payload, dict) and isinstance(leg_payload.get("trains"), list):
                    candidates.extend(leg_payload["trains"])
        for row in candidates:
            if not isinstance(row, dict):
                continue
            key = tuple(str(row.get(field) or "") for field in (
                "direction", "train_no", "from_station", "depart_time",
                "to_station", "arrive_time", "travel_date",
            ))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def _transport_leg(row: Dict) -> TransportLeg | None:
    service = _clean(row.get("train_no"), 40)
    origin = _clean(row.get("from_station"), 80)
    departure = _clean(row.get("depart_time"), 40)
    destination = _clean(row.get("to_station"), 80)
    arrival = _clean(row.get("arrive_time"), 40)
    if not all((service, origin, departure, destination, arrival)):
        return None
    raw_availability = row.get("seats") or {}
    availability = {
        _clean(name, 20): _clean(value, 20)
        for name, value in raw_availability.items()
        if name and value not in (None, "")
    } if isinstance(raw_availability, dict) else {}
    return TransportLeg(
        mode="train",
        direction=_clean(row.get("direction"), 20),
        service=service,
        origin=origin,
        departure_time=departure,
        destination=destination,
        arrival_time=arrival,
        duration=_clean(row.get("duration"), 40),
        travel_date=_clean(row.get("travel_date"), 40),
        availability=availability,
    )


def _selected_transport_legs(value: str, rows: List[Dict]) -> List[TransportLeg]:
    """Match planner-selected train numbers back to authoritative 12306 rows."""
    source = str(value or "")
    compact = re.sub(r"\s+", "", source).upper()
    selections = []
    for match in _TRAIN_NO_PATTERN.finditer(source.upper()):
        service = match.group(1).upper()
        prefix = source[:match.start()]
        direction = "返程" if prefix.rfind("返程") > prefix.rfind("去程") else "去程"
        key = (service, direction)
        if key not in selections:
            selections.append(key)

    legs: List[TransportLeg] = []
    for service, direction in selections[:4]:
        candidates = [
            row for row in rows
            if str(row.get("train_no") or "").upper() == service
        ]
        if not candidates:
            continue

        def score(row: Dict) -> int:
            points = 0
            if str(row.get("direction") or "") == direction:
                points += 2
            for fields in (("from_station", "depart_time"), ("to_station", "arrive_time")):
                needle = "".join(str(row.get(field) or "") for field in fields).upper()
                if needle and needle in compact:
                    points += 4
            return points

        selected = max(candidates, key=score)
        leg = _transport_leg(selected)
        if leg is not None:
            legs.append(leg)
    return legs


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
        by_goal: Dict[str, List[TaskResult]] = {}
        for result in result_list:
            by_goal.setdefault(result.goal_id or result.intent, []).append(result)

        sections: List[AnswerSection] = []
        notices: List[str] = []
        for task in sorted(task_list, key=lambda item: item.display_order):
            group = by_goal.get(task.task_id, [])
            if not group:  # compatibility for non-durable unit results
                group = by_goal.get(task.intent, [])
            primary = self._primary(group)
            if primary is None:
                continue  # 该意图结果全部被工作流 suppress，不单独成区。
            kind = section_kind_for_intent(task.intent) or "general"
            if primary.status != "success":
                label = _LABEL_BY_KIND.get(kind, "处理结果")
                message = primary.error_message or f"{label}暂时查询失败，请稍后重试。"
                sections.append(AnswerSection(kind=kind, title=label, status="error", body=message))
                sections[-1].goal_id = task.task_id
                notices.append(message)
                continue
            built = self._render(kind, primary)
            if kind == "trip":
                rows = _train_rows(group)
                if rows:
                    for section in built:
                        for item in section.items:
                            if item.label in _TRANSPORT_ITEM_LABELS:
                                item.transport_legs = _selected_transport_legs(item.value, rows)
            for section in built:
                section.goal_id = task.task_id
            sections.extend(built)
            if kind == "trip":
                notices.extend(self._trip_notices(primary))
            notices.extend(self._secondary_notices(group, primary))

        if not sections:
            sections.append(AnswerSection(kind="general", title="处理结果", body="已整理完成。"))

        sections = self._fit_section_cap(sections, notices)

        summary = self._summary(sections)
        document = AnswerDocument(
            title=f"{destination}差旅信息" if destination != "本次出差" else destination,
            summary=summary,
            sections=sections,
            notices=list(dict.fromkeys(notice for notice in notices if notice))[:10],
            sources=self.sources(result_list),
        )
        document.plain_text = render_plain_text(document)
        return document

    @staticmethod
    def _fit_section_cap(
        sections: List[AnswerSection], notices: List[str],
    ) -> List[AnswerSection]:
        """Never let a pathological section count crash AnswerDocument construction.

        Over the cap, first fold excess per-day trip sections into their trip
        overview section (``AnswerSection.items`` is unbounded, so this is
        lossless — long itineraries still render, days merge into the overview).
        Sections still over the cap afterwards (pathological multi-goal runs)
        are dropped from the tail and surfaced as a notice.
        """
        if len(sections) <= ANSWER_SECTION_CAP:
            return sections
        trip_overview = next(
            (
                section for section in sections
                if section.kind == "trip" and not section.title.startswith("第 ")
            ),
            None,
        )
        if trip_overview is not None:
            merged = False
            kept: List[AnswerSection] = []
            for section in sections:
                if (
                    section is not trip_overview
                    and section.kind == "trip"
                    and section.title.startswith("第 ")
                ):
                    trip_overview.items.extend(section.items)
                    merged = True
                else:
                    kept.append(section)
            if merged:
                notices.append("行程天数较多，逐日安排已合并到行程总览。")
            sections = kept
        if len(sections) > ANSWER_SECTION_CAP:
            dropped = sections[ANSWER_SECTION_CAP:]
            sections = sections[:ANSWER_SECTION_CAP]
            notices.append(f"内容较多，仅展示前 {ANSWER_SECTION_CAP} 个部分，其余已省略。")
            notices.extend(f"已省略：{section.title}" for section in dropped)
        return sections

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
                data.get("summary")
                or data.get("answer")
                or data.get("message")
                or data.get("conclusion")
                or _VERDICT_LABELS.get(str(data.get("verdict") or "").lower())
                or "",
                800,
            )
            if text:
                notices.append(text)
        return notices

    @staticmethod
    def _trip_notices(result: TaskResult) -> List[str]:
        """Expose useful itinerary notes without leaking machine sentinels."""
        data = _nested(result.data)
        itinerary = data.get("itinerary") if isinstance(data.get("itinerary"), dict) else data
        raw_notes = itinerary.get("notes") or []
        if isinstance(raw_notes, str):
            raw_notes = [raw_notes]
        notices = []
        for value in raw_notes:
            text = _clean(value, 800)
            if text and text.lower() not in {"unknown", "none", "null", "n/a"}:
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
            "train": cls._train_section,
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
    def _train_section(result: TaskResult) -> AnswerSection:
        payload = _nested(result.data)
        if "results" in payload and isinstance(payload.get("results"), dict):
            payload = payload["results"]
        summary = str(payload.get("summary") or payload.get("message") or "已取得车次信息。")
        note = _clean(payload.get("note"), 600)
        items = []
        for train in payload.get("trains") or []:
            if not isinstance(train, dict):
                continue
            route = " ".join(filter(None, [
                _clean(train.get("depart_time")),
                _clean(train.get("from_station")),
                "→",
                _clean(train.get("to_station")),
                _clean(train.get("arrive_time")),
                _clean(train.get("duration")),
            ]))
            seats = train.get("seats") or {}
            if isinstance(seats, dict):
                seat_detail = " · ".join(
                    f"{_clean(name, 20)} {_clean(value, 20)}"
                    for name, value in seats.items() if value
                )
            else:
                seat_detail = _clean(seats, 120)
            leg = _transport_leg(train)
            item = AnswerItem(
                label=_clean(train.get("train_no"), 60),
                value=_clean(route, 300),
                detail=_clean(seat_detail, 600),
                transport_legs=[leg] if leg is not None else [],
            ) if route and train.get("train_no") else None
            if item:
                items.append(item)
        body_parts = []
        if not items:
            body_parts.append(summary)
        if note:
            body_parts.append(note)
        return AnswerSection(
            kind="train",
            title="车次信息",
            body=" ".join(body_parts),
            items=items,
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
        if data.get("planning_ready") is False:
            missing = data.get("missing_fields") or []
            missing_text = "、".join(str(item) for item in missing if item)
            body = "还需要补充行程信息后才能生成完整方案。"
            if missing_text:
                body = f"还需要补充：{missing_text}。"
            return [AnswerSection(
                kind="trip",
                title="行程信息待补充",
                status="partial",
                body=body,
            )]
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
            data.get("summary") or data.get("conclusion") or data.get("answer")
            or _VERDICT_LABELS.get(str(data.get("verdict") or "").lower())
            or "合规检查完成。"
        )
        items = []
        for check in data.get("checks") or []:
            if not isinstance(check, dict):
                continue
            label = _clean(check.get("item") or check.get("rule") or check.get("label") or "检查项", 60)
            raw_status = str(check.get("status") or "").lower()
            status = _clean(_VERDICT_LABELS.get(raw_status) or raw_status, 60)
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
        partial = [section.title for section in sections if section.status == "partial"]
        failed = [section.title for section in sections if section.status == "error"]
        if partial and not successful and not failed:
            return "还需要补充部分信息后才能继续。"
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
        return sources[:ANSWER_SOURCE_CAP]
