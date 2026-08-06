"""Deterministic card adapter for successful legacy orchestration results.

The orchestration payload remains the source of truth. This module only maps
known fields into the presentation-neutral AnswerDocument contract; it never
invents business facts.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from .answer_document import (
    AnswerDocument,
    AnswerItem,
    AnswerSection,
    AnswerSource,
    DepartureCheckItem,
    DepartureWeather,
    PreDepartureChecklist,
    WeatherDay,
    render_plain_text,
)


_FORECAST_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})[:：]\s*([^，；;]+?)\s*[，,]\s*"
    r"([^，；;~～]+)\s*[~～]\s*([^，；;°]+)°?C"
    r"(?:[，,]\s*最高降水概率\s*([^；;。]+))?"
)
_CURRENT_PATTERN = re.compile(
    r"当前天气[:：]\s*([^，。]+)[，,]\s*气温\s*([^，。]+)[，,]\s*湿度\s*([^，。]+)"
)
_TRIP_WEATHER_PATTERN = re.compile(
    r"天气[:：]\s*([^，,。]+)[，,]\s*(\d+)\s*[~～-]\s*(\d+)\s*°?C"
)
_TITLE_BY_AGENT = {
    "itinerary_planning": "出差方案已整理",
    "trip_compliance": "差旅合规检查",
    "rag_knowledge": "差旅制度信息",
    "information_query": "出行信息",
    "preference": "差旅偏好已更新",
    "memory_query": "差旅记录",
    "event_collection": "行程信息",
}


def _nested(data: Dict[str, Any]) -> Dict[str, Any]:
    nested = data.get("data")
    return nested if isinstance(nested, dict) else data


def _clean(value: Any, limit: int = 4000) -> str:
    text = str(value or "").replace("**", "").replace("`", "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _item(label: str, value: Any, detail: Any = "") -> AnswerItem | None:
    clean_value = _clean(value, 300)
    if not clean_value:
        return None
    return AnswerItem(label=_clean(label, 60), value=clean_value, detail=_clean(detail, 600))


def _compact_items(values: Iterable[AnswerItem | None]) -> List[AnswerItem]:
    return [value for value in values if value is not None]


def _short_detail(value: Any, limit: int = 220) -> str:
    text = _clean(value, limit * 2)
    clauses = re.split(r"(?<=[。；])", text)
    selected = ""
    for clause in clauses:
        if selected and len(selected) + len(clause) > limit:
            break
        selected += clause
        if len(selected) >= 70:
            break
    return _clean(selected or text, limit)


def _check_item(
    key: str,
    label: str,
    detail: Any,
    status: str,
    status_label: str,
    action_label: str = "",
    action_value: str = "",
) -> DepartureCheckItem:
    return DepartureCheckItem(
        key=key,
        label=label,
        detail=_clean(detail, 500),
        status=status,
        status_label=status_label,
        action_label=action_label,
        action_value=action_value,
    )


def _itinerary_sections(data: Dict[str, Any]) -> List[AnswerSection]:
    payload = _nested(data)
    itinerary = payload.get("itinerary")
    if not isinstance(itinerary, dict):
        return []

    transport = itinerary.get("transport_recommendation")
    transport_items: List[AnswerItem | None] = []
    if isinstance(transport, dict):
        transport_items.extend([
            _item("首选交通", transport.get("preferred"), transport.get("reason")),
            _item("备选交通", transport.get("alternative"), transport.get("verification")),
        ])
    elif transport:
        transport_items.append(_item("交通建议", transport))

    overview = _compact_items([
        _item("行程时长", itinerary.get("duration")),
        _item("住宿建议", _short_detail(itinerary.get("lodging_advice"), 220)),
        _item("预算参考", itinerary.get("estimated_budget")),
        *transport_items,
    ])
    sections = [AnswerSection(
        kind="trip",
        title="行程概览",
        items=overview,
    )]

    for index, day in enumerate(itinerary.get("daily_plans") or [], 1):
        if not isinstance(day, dict):
            continue
        slots = day.get("activities") or day.get("time_slots") or []
        items = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            activity = slot.get("activity") or slot.get("location")
            details = " · ".join(
                _clean(value, 180)
                for value in (slot.get("description"), slot.get("transport"))
                if value
            )
            item = _item(slot.get("time") or "安排", activity, details)
            if item:
                items.append(item)
        meals = day.get("meals") or {}
        if isinstance(meals, dict):
            items.extend(_compact_items([
                _item("午餐", meals.get("lunch")),
                _item("晚餐", meals.get("dinner")),
            ]))
        sections.append(AnswerSection(
            kind="trip",
            title=f"第 {day.get('day') or index} 天",
            items=items,
        ))

    return sections


def _split_reimbursement_items(raw_items: Any) -> List[str]:
    values = raw_items if isinstance(raw_items, list) else [raw_items]
    items = []
    for value in values:
        for part in re.split(r"[；;\n]", _clean(value, 2000)):
            clean_part = part.strip(" •-\t")
            if clean_part:
                items.append(clean_part)
    return list(dict.fromkeys(items))[:10]


def _reimbursement_item(value: str, index: int) -> DepartureCheckItem:
    categories = (
        (("高铁", "火车", "机票", "车票", "航班"), "交通票据"),
        (("酒店", "住宿"), "住宿发票"),
        (("餐饮", "餐费", "餐补"), "餐饮凭证"),
        (("市内交通", "出租车", "地铁"), "市内交通"),
        (("会议", "签到"), "会议材料"),
        (("审批", "出差申请"), "出差审批"),
        (("报销单",), "报销单"),
    )
    label = "报销材料"
    for keywords, candidate in categories:
        if any(keyword in value for keyword in keywords):
            label = candidate
            break
    optional = any(keyword in value for keyword in ("如需", "如有", "按需"))
    return _check_item(
        key=f"reimbursement_{index}",
        label=label,
        detail=value,
        status="optional" if optional else "required",
        status_label="按需" if optional else "需保留",
    )


def _weather_preparation(notes: List[str], results: List[Dict[str, Any]]) -> DepartureWeather | None:
    note = next((value for value in notes if "天气" in value), "")
    condition = temperature = humidity = ""
    match = _TRIP_WEATHER_PATTERN.search(note)
    if match:
        condition = match.group(1).strip()
        temperature = f"{match.group(2)}–{match.group(3)}°C"

    for result in results:
        if result.get("agent_name") != "information_query" or result.get("status") != "success":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        payload = _nested(data)
        info = payload.get("results") if isinstance(payload.get("results"), dict) else payload
        summary = _clean(info.get("summary") or info.get("message"))
        current = _CURRENT_PATTERN.search(summary)
        if current:
            condition = condition or current.group(1).strip()
            temperature = temperature or current.group(2).strip()
            humidity = f"湿度 {current.group(3).strip()}"
        if condition or temperature:
            break

    if not any((note, condition, temperature, humidity)):
        return None
    preparation = []
    source = note
    if any(keyword in source for keyword in ("雨", "阵雨", "降水")):
        preparation.append("雨具")
    if "防晒" in source or any(keyword in condition for keyword in ("晴", "Clear")):
        preparation.append("防晒用品")
    if any(keyword in source for keyword in ("防暑", "高温")):
        preparation.append("补水防暑")
    if "湿" in source:
        preparation.append("透气衣物")
    return DepartureWeather(
        condition=condition,
        temperature=temperature,
        humidity=humidity,
        advice=_short_detail(note, 260),
        preparation=list(dict.fromkeys(preparation)),
    )


def _pre_departure_checklist(results: List[Dict[str, Any]]) -> PreDepartureChecklist | None:
    planning_result = next(
        (
            result for result in results
            if result.get("agent_name") == "itinerary_planning" and result.get("status") == "success"
        ),
        None,
    )
    if not planning_result:
        return None
    data = planning_result.get("data") if isinstance(planning_result.get("data"), dict) else {}
    payload = _nested(data)
    itinerary = payload.get("itinerary")
    if not isinstance(itinerary, dict):
        return None

    notes = [_clean(value, 800) for value in itinerary.get("notes") or [] if value]
    missing = itinerary.get("missing_info") or []
    if isinstance(missing, str):
        missing = [missing]
    missing = [_clean(value, 300) for value in missing if value]
    critical_items: List[DepartureCheckItem] = []

    meeting_missing = [
        value for value in missing
        if any(keyword in value for keyword in ("会议", "客户", "工作地点", "工作时间"))
    ]
    meeting_note = next(
        (value for value in notes if "确认" in value and any(keyword in value for keyword in ("会议", "客户"))),
        "",
    )
    if meeting_missing or meeting_note:
        detail = "；".join(meeting_missing[:2]) or meeting_note
        critical_items.append(_check_item(
            "meeting",
            "会议安排",
            _short_detail(detail),
            "pending",
            "待补充",
            "去补充",
            "会议地点和时间：",
        ))

    transport = itinerary.get("transport_recommendation")
    if isinstance(transport, dict) and transport.get("preferred"):
        verification = transport.get("verification") or ""
        policy_note = next(
            (value for value in notes if any(keyword in value for keyword in ("交通等级", "商务座", "商务舱")) and "确认" in value),
            "",
        )
        needs_check = bool(verification or policy_note)
        detail_parts = [f"建议方案：{_clean(transport.get('preferred'), 180)}"]
        if verification:
            detail_parts.append(_short_detail(verification, 180))
        if policy_note:
            detail_parts.append(_short_detail(policy_note, 150))
        critical_items.append(_check_item(
            "transport",
            "交通票务",
            " ".join(detail_parts),
            "pending" if needs_check else "ready",
            "待核验" if needs_check else "已准备",
            "补充标准" if policy_note else "",
            "公司允许的交通等级是：" if policy_note else "",
        ))

    lodging = itinerary.get("lodging_advice")
    if lodging:
        policy_note = next(
            (value for value in notes if any(keyword in value for keyword in ("住宿标准", "酒店价格"))),
            "",
        )
        needs_check = any(keyword in str(lodging) + policy_note for keyword in ("未明确", "确认", "核验", "是否符合"))
        detail = _short_detail(lodging, 200)
        if policy_note and policy_note not in detail:
            detail += " " + _short_detail(policy_note, 140)
        critical_items.append(_check_item(
            "lodging",
            "酒店预订",
            detail,
            "pending" if needs_check else "ready",
            "待核验" if needs_check else "已准备",
            "补充标准" if needs_check else "",
            "公司住宿标准是：" if needs_check else "",
        ))

    booking_note = next(
        (value for value in notes if "预订" in value and any(keyword in value for keyword in ("尽快", "提前", "出发日"))),
        "",
    )
    if booking_note:
        critical_items.append(_check_item(
            "booking",
            "预订进度",
            _short_detail(booking_note),
            "pending",
            "待处理",
        ))

    reimbursement = [
        _reimbursement_item(value, index)
        for index, value in enumerate(
            _split_reimbursement_items(itinerary.get("reimbursement_checklist") or []),
            1,
        )
    ]
    pending_count = sum(item.status == "pending" for item in critical_items)
    return PreDepartureChecklist(
        pending_count=pending_count,
        critical_items=critical_items,
        weather=_weather_preparation(notes, results),
        reimbursement_items=reimbursement,
    )


def _policy_section(data: Dict[str, Any]) -> AnswerSection:
    payload = _nested(data)
    answer = payload.get("answer") or payload.get("content") or "制度信息已取得。"
    if isinstance(answer, dict):
        answer = answer.get("answer") or str(answer)
    return AnswerSection(kind="policy", title="适用差旅制度", body=_clean(answer))


def _weather_section(data: Dict[str, Any]) -> AnswerSection:
    payload = _nested(data)
    results = payload.get("results") if isinstance(payload.get("results"), dict) else payload
    summary = _clean(results.get("summary") or results.get("message") or results.get("error"))
    current = _CURRENT_PATTERN.search(summary)
    items = _compact_items([
        _item("当前", f"{current.group(1).strip()} · {current.group(2).strip()}", f"湿度 {current.group(3).strip()}")
        if current else None
    ])
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
        title="目的地天气与出行信息",
        body="" if items or days else summary,
        items=items,
        days=days,
    )


def _preference_section(data: Dict[str, Any]) -> AnswerSection:
    payload = _nested(data)
    preferences = payload.get("preferences") or []
    if isinstance(preferences, dict):
        preferences = preferences.get("preferences") or []
    labels = {
        "home_location": "常驻地",
        "transportation_preference": "交通偏好",
        "hotel_brands": "酒店偏好",
        "airlines": "航空公司",
        "seat_preference": "座位偏好",
        "meal_preference": "餐食偏好",
        "budget_level": "预算等级",
    }
    items = []
    for preference in preferences if isinstance(preferences, list) else []:
        if not isinstance(preference, dict):
            continue
        value = preference.get("value")
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        item = _item(
            labels.get(preference.get("type"), preference.get("type") or "偏好"),
            value,
            "追加记录" if preference.get("action") == "append" else "已更新",
        )
        if item:
            items.append(item)
    return AnswerSection(kind="preference", title="已保存的设置", items=items)


def _event_section(data: Dict[str, Any]) -> AnswerSection:
    payload = _nested(data)
    values = [
        ("出发地", payload.get("origin")),
        ("目的地", payload.get("destination")),
        ("出发日期", payload.get("start_date")),
        ("返程日期", payload.get("end_date")),
        ("行程天数", payload.get("duration_days") and f"{payload['duration_days']} 天"),
        ("出差目的", payload.get("trip_purpose")),
        ("工作地点", payload.get("work_location")),
        ("工作时间", payload.get("work_schedule")),
    ]
    return AnswerSection(
        kind="trip",
        title="已确认的行程信息",
        items=_compact_items(_item(label, value) for label, value in values),
    )


def _compliance_section(data: Dict[str, Any]) -> AnswerSection:
    payload = _nested(data)
    labels = {
        "compliant": "符合制度",
        "non_compliant": "存在不合规项",
        "partial": "部分项目待确认",
        "unknown": "暂时无法确认",
    }
    items = [_item("结论", labels.get(payload.get("verdict"), payload.get("verdict") or "待确认"))]
    for check in payload.get("checks") or []:
        if isinstance(check, dict):
            items.append(_item(check.get("item") or "检查项", check.get("status") or "待确认", check.get("reason")))
    return AnswerSection(
        kind="notice",
        title="合规结论",
        body=_clean(payload.get("summary")),
        items=_compact_items(items),
        status="partial" if payload.get("verdict") in {"partial", "unknown"} else "success",
    )


def _generic_section(agent_name: str, data: Dict[str, Any]) -> AnswerSection:
    payload = _nested(data)
    value = next(
        (payload.get(key) for key in ("answer", "result", "content", "message", "summary", "text") if payload.get(key)),
        "相关信息已整理。",
    )
    return AnswerSection(
        kind="memory" if agent_name == "memory_query" else "general",
        title=_TITLE_BY_AGENT.get(agent_name, "处理结果"),
        body=_clean(value),
    )


def _sources(results: Iterable[Dict[str, Any]]) -> List[AnswerSource]:
    sources = []
    seen = set()
    for result in results:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        payload = _nested(data)
        raw_sources = payload.get("sources") or []
        if isinstance(payload.get("results"), dict):
            raw_sources = raw_sources or payload["results"].get("sources") or []
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            title = _clean(source.get("title") or source.get("file") or source.get("source") or "数据来源", 200)
            detail = " · ".join(
                _clean(value, 200)
                for value in (source.get("section"), source.get("page") and f"第{source['page']}页")
                if value
            )
            url = _clean(source.get("url"), 1000)
            key = (title, detail, url)
            if key in seen:
                continue
            seen.add(key)
            sources.append(AnswerSource(title=title, detail=detail, url=url))
    return sources


def build_legacy_answer_document(result_data: Dict[str, Any], plain_text: str = "") -> AnswerDocument | None:
    """Build a dense card for legacy business-agent output.

    Chitchat intentionally stays conversational. Incomplete trip intake has its
    own richer document and is handled before this adapter is called.
    """
    results = result_data.get("results") or []
    if not isinstance(results, list):
        return None
    planning_complete = any(
        item.get("agent_name") == "itinerary_planning" and item.get("status") == "success"
        for item in results if isinstance(item, dict)
    )
    sections: List[AnswerSection] = []
    notices: List[str] = []
    visible_agents: List[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        agent_name = str(result.get("agent_name") or "")
        if agent_name == "chitchat":
            continue
        if planning_complete and agent_name in {"event_collection", "rag_knowledge", "information_query"}:
            continue
        visible_agents.append(agent_name)
        status = result.get("status")
        if status == "error":
            message = _clean(result.get("error_message") or result.get("message") or "这部分暂时不可用，请稍后重试。")
            sections.append(AnswerSection(kind="notice", title=_TITLE_BY_AGENT.get(agent_name, "处理结果"), status="error", body=message))
            notices.append(message)
            continue
        if status != "success":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if agent_name == "itinerary_planning":
            sections.extend(_itinerary_sections(data))
        elif agent_name == "rag_knowledge":
            sections.append(_policy_section(data))
        elif agent_name == "information_query":
            sections.append(_weather_section(data))
        elif agent_name == "preference":
            sections.append(_preference_section(data))
        elif agent_name == "event_collection":
            sections.append(_event_section(data))
        elif agent_name == "trip_compliance":
            sections.append(_compliance_section(data))
        else:
            sections.append(_generic_section(agent_name, data))

    sections = [section for section in sections if section.body or section.items or section.days][:12]
    if not sections:
        return None
    primary = next((agent for agent in visible_agents if agent in _TITLE_BY_AGENT), visible_agents[0] if visible_agents else "")
    itinerary_title = ""
    itinerary_duration = ""
    if planning_complete:
        planning_result = next(
            (result for result in results if result.get("agent_name") == "itinerary_planning"),
            {},
        )
        planning_data = planning_result.get("data") if isinstance(planning_result.get("data"), dict) else {}
        itinerary = _nested(planning_data).get("itinerary")
        if isinstance(itinerary, dict):
            itinerary_title = _clean(itinerary.get("title"), 100)
            itinerary_duration = _clean(itinerary.get("duration"), 100)
    document = AnswerDocument(
        title=itinerary_title or _TITLE_BY_AGENT.get(primary, "差旅信息已整理"),
        summary=(
            f"{itinerary_duration} · 交通、住宿、日程与报销准备已整理。"
            if itinerary_duration else
            f"已将 {len(sections)} 组关键信息整理为卡片，详细文字可按需展开。"
        ),
        sections=sections,
        notices=list(dict.fromkeys(notices)),
        sources=_sources(results),
        pre_departure=_pre_departure_checklist(results),
    )
    document.plain_text = plain_text.strip() or render_plain_text(document)
    return document
