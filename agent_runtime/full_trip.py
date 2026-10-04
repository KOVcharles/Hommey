"""Compose the complete trip without dropping evidence or specialist sections."""
from datetime import date, datetime, time, timedelta, timezone

from core.presentation.answer_document import AnswerDocument, AnswerSection, render_plain_text
from .contracts import Finish
from .render import render


def merge_documents(output, extra):
    document = AnswerDocument.model_validate(output["answer_document"])
    addition = AnswerDocument.model_validate(extra["answer_document"])
    existing = {(s.goal_id, s.kind, s.title) for s in document.sections}
    document.sections.extend(s for s in addition.sections if (s.goal_id, s.kind, s.title) not in existing)
    sources = {(s.title, s.detail, s.updated_at): s for s in [*document.sources, *addition.sources]}
    document.sources = list(sources.values())[:30]
    document.notices = list(dict.fromkeys([*document.notices, *addition.notices]))[:10]
    document.plain_text = render_plain_text(document)[:12000]
    return {**output, "answer_document": document.model_dump(mode="json"), "response": document.plain_text}


def complete_output(output, results):
    if results:
        output = merge_documents(output, render(Finish(), results))
    document = AnswerDocument.model_validate(output["answer_document"])
    required = {"policy_rag", "trip_planner"}
    successful = {r.role for r in results if r.status == "success" and not r.missing_info
                  and (r.role != "policy_rag" or bool(r.evidence_refs and r.data.get("findings")))
                  and (r.role != "trip_planner" or bool(r.data.get("itinerary", {}).get("days")))}
    pending = list(dict.fromkeys([*document.notices, *(item for r in results for item in r.missing_info)]))
    for role, label in (("policy_rag", "差旅标准"), ("trip_planner", "每日安排")):
        matching = [r for r in results if r.role == role]
        if not matching or any(r.status in {"unavailable", "error"} for r in matching):
            pending.append(f"{label}本次未完成，已保留可用的车次和酒店。")
    if pending or not required <= successful or any(r.status != "success" for r in results):
        output["outcome"] = "partial"
    document.notices = list(dict.fromkeys([*document.notices, *pending]))[:10]
    if pending:
        document.sections.append(AnswerSection(kind="notice", title="出发前待确认", status="partial", body="\n".join(pending)[:4000]))
    document.plain_text = render_plain_text(document)[:12000]
    return {**output, "answer_document": document.model_dump(mode="json"), "response": document.plain_text}


def planning_train(row):
    """Make overnight dates explicit instead of asking the model to infer them."""
    value = row.model_dump(mode="json")
    try:
        departure_date = date.fromisoformat(row.travel_date)
        if row.arrival_day_offset < 0:
            raise ValueError("negative arrival offset")
        arrival_date = departure_date + timedelta(days=row.arrival_day_offset)
        tz = timezone(timedelta(hours=8))
        departure = datetime.combine(departure_date, time.fromisoformat(row.depart_time), tzinfo=tz)
        arrival = datetime.combine(arrival_date, time.fromisoformat(row.arrive_time), tzinfo=tz)
        if arrival < departure:
            raise ValueError("arrival before departure")
        value.update(departure_at=departure.isoformat(timespec="minutes"), arrival_at=arrival.isoformat(timespec="minutes"))
    except (ValueError, OverflowError):
        value["date_status"] = "invalid"
    return value


def planning_facts(board):
    """Bounded factual input; leaves do not need access to provider payloads."""
    return {"anchor": board.anchor.model_dump(mode="json") if board.anchor else None,
            "trains": [planning_train(row) for row in board.trains[:4]],
            "hotels": [row.model_dump(mode="json") for row in board.hotels],
            "weather": board.weather.model_dump(mode="json") if hasattr(board.weather, "model_dump") else board.weather,
            "commute": board.commute.model_dump(mode="json") if board.commute else None,
            "preference_note": board.preference_note, "next_actions": board.next_actions,
            "capability_selection": board.capability_selection.model_dump(mode="json") if hasattr(board.capability_selection, "model_dump") else board.capability_selection}
