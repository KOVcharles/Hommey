"""Compose the complete trip without dropping evidence or specialist sections."""
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


def planning_facts(board):
    """Bounded factual input; leaves do not need access to provider payloads."""
    return {"anchor": board.anchor.model_dump(mode="json") if board.anchor else None,
            "trains": [row.model_dump(mode="json") for row in board.trains[:4]],
            "hotels": [row.model_dump(mode="json") for row in board.hotels],
            "weather": board.weather.model_dump(mode="json") if hasattr(board.weather, "model_dump") else board.weather,
            "commute": board.commute.model_dump(mode="json") if board.commute else None,
            "preference_note": board.preference_note, "next_actions": board.next_actions,
            "capability_selection": board.capability_selection.model_dump(mode="json") if hasattr(board.capability_selection, "model_dump") else board.capability_selection}
