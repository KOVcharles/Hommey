"""Deterministic frontend DTOs: service information and evidenced business results."""
from core.presentation.answer_document import (
    AnswerDocument, AnswerItem, AnswerSection, AnswerSource, TransportLeg, WeatherDay, render_plain_text,
)
from .profiles import PROFILES


REFUSAL = "我可以处理报销制度咨询、材料准备、差旅行程规划、交通天气查询和本人差旅记忆，不处理其他领域，也不执行预订、付款或提交操作。"
KINDS = {"trip_context": "trip", "policy_rag": "policy", "memory": "memory", "travel_info": "general", "trip_planner": "trip", "compliance": "policy"}


def service_introduction():
    capabilities = "、".join(profile.title for profile in PROFILES.values())
    return (f"我是 Hommey，报销与差旅助手，可以帮助你进行{capabilities}。\n"
            "你可以直接问某项费用如何报销、需要哪些材料，也可以提供出发地、目的地和日期来安排出差，或查询交通、天气。\n"
            "我提供咨询与准备支持，不代办预订、付款或提交审批/报销。")


def render(finish, results):
    if finish.kind == "refuse":
        return {"response": REFUSAL, "answer_document": None, "presentation_document": None}
    sections, sources, seen = [], [], set()
    if finish.kind == "help":
        sections.append(AnswerSection(kind="notice", title="我能帮你做什么", status="success",
                                      body=service_introduction()))
    for result in results:
        status = "success" if result.status == "success" else "error" if result.status in {"error", "unavailable"} else "partial"
        section = AnswerSection(kind=KINDS[result.role], goal_id=result.result_id,
            title=PROFILES[result.role].title, status=status, body=result.summary)
        if result.missing_info:
            section.body = (section.body + "\n待确认：" + "、".join(result.missing_info))[:4000]
        if result.role in {"policy_rag", "memory", "travel_info"}:
            for finding in result.data.get("findings", []):
                section.items.append(AnswerItem(label=finding["item"][:60],
                    value=finding["conclusion"][:300], detail=finding.get("applicability", "")[:500]))
        for source in result.sources:
            if source["id"] not in result.evidence_refs:
                continue
            if source["id"] not in seen:
                seen.add(source["id"])
                source_data = source.get("data")
                metadata = source_data.get("metadata", {}) if isinstance(source_data, dict) else {}
                metadata = metadata if isinstance(metadata, dict) else {}
                title = str(metadata.get("title") or metadata.get("file_name") or metadata.get("source") or source["kind"])
                title = title.replace("\\", "/").rsplit("/", 1)[-1]
                detail = " · ".join(f"{key}: {metadata[key]}" for key in ("page", "section", "version", "effective_date") if metadata.get(key))
                if title == "policy":
                    title = "报销与差旅制度"
                sources.append(AnswerSource(title=title[:200], detail=detail[:500], updated_at=source.get("retrieved_at", "")))
            data = source.get("data")
            if source["kind"] == "train" and isinstance(data, list) and result.role == "travel_info":
                section.kind = "train"
                for row in data[:6]:
                    try:
                        leg = TransportLeg(service=row["train_no"], origin=row["from_station"], destination=row["to_station"],
                            departure_time=row["depart_time"], arrival_time=row["arrive_time"], duration=row.get("duration", ""),
                            travel_date=row.get("travel_date", ""), availability={str(k): str(v) for k, v in row.get("seats", {}).items()})
                        section.items.append(AnswerItem(label=leg.service, value=f"{leg.origin} {leg.departure_time} → {leg.destination} {leg.arrival_time}"[:300],
                            detail=f"{leg.travel_date}，余票以实时查询为准；未提供票价", transport_legs=[leg]))
                    except (KeyError, ValueError, TypeError):
                        continue
            if source["kind"] == "weather" and isinstance(data, dict) and result.role == "travel_info":
                section.kind = "weather"
                for day in data.get("forecasts", [])[:4]:
                    section.days.append(WeatherDay(date=str(day.get("date", "未知"))[:40], condition=str(day.get("day_condition", ""))[:60],
                        low=str(day.get("low_c", ""))[:30], high=str(day.get("high_c", ""))[:30]))
        if result.role == "trip_planner":
            itinerary = result.data.get("itinerary", {})
            days = itinerary.get("days", []) if isinstance(itinerary, dict) else []
            for day in days[:90]:
                if isinstance(day, dict):
                    activities = [str(x) for x in day.get("activities", [])]
                    section.items.append(AnswerItem(label=str(day.get("date") or "行程")[:60],
                        value=f"{len(activities)} 项安排" if activities else "待补充", activities=activities))
        sections.append(section)
    if finish.kind in {"ask", "clarify"}:
        question = finish.question
        sections.append(AnswerSection(kind="notice", title="请说明你的需求" if finish.kind == "clarify" else "需要补充的信息",
                                      status="partial", body=question))
    document = AnswerDocument(title="报销与差旅助手", sections=sections, sources=sources[:30])
    document.plain_text = render_plain_text(document)
    return {"response": document.plain_text, "answer_document": document.model_dump(mode="json"), "presentation_document": None}
