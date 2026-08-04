import asyncio
import json
from pathlib import Path

from agentscope.message import Msg
from agents.orchestration_agent import OrchestrationAgent
from context.long_term_memory import FileLongTermMemory
from core.presentation import build_trip_intake_document
from core.trip_intake import evaluate_trip_intake
from webui_new.manager import HommeyWebInstance
from settings import RESILIENCE_CONFIG, TRIP_INTAKE_CONFIG


BASE_TRIP = {
    "origin": "北京",
    "destination": "南京",
    "start_date": None,
    "end_date": None,
    "duration_days": None,
    "trip_purpose": None,
    "work_location": None,
    "work_schedule": None,
}


def test_trip_intake_calculates_logical_required_progress():
    state = evaluate_trip_intake(BASE_TRIP)

    assert state["planning_ready"] is False
    assert state["completion"] == {"completed": 2, "total": 5}
    assert state["missing_required"] == ["start_date", "trip_length", "trip_purpose"]
    assert state["missing_info"] == ["start_date", "duration_days_or_end_date", "trip_purpose"]
    assert state["optional_info"] == ["work_location", "work_schedule"]


def test_trip_duration_or_return_date_satisfies_one_requirement():
    by_duration = evaluate_trip_intake({
        **BASE_TRIP,
        "start_date": "2026-08-05",
        "duration_days": 2,
        "trip_purpose": "客户会议",
    })
    by_end_date = evaluate_trip_intake({
        **BASE_TRIP,
        "start_date": "2026-08-05",
        "end_date": "2026-08-06",
        "trip_purpose": "客户会议",
    })

    assert by_duration["planning_ready"] is True
    assert by_end_date["planning_ready"] is True
    assert by_duration["completion"]["completed"] == 5


def test_invalid_and_conflicting_trip_fields_block_planning():
    invalid = evaluate_trip_intake({
        **BASE_TRIP,
        "start_date": "下个月某天",
        "duration_days": 0,
        "trip_purpose": "会议",
    })
    conflict = evaluate_trip_intake({
        **BASE_TRIP,
        "start_date": "2026-08-05",
        "end_date": "2026-08-09",
        "duration_days": 2,
        "trip_purpose": "会议",
    })

    assert {item["key"] for item in invalid["invalid_fields"]} == {"start_date", "trip_length"}
    assert invalid["planning_ready"] is False
    assert conflict["conflicts"][0]["key"] == "trip_length"
    assert "5 天" in conflict["conflicts"][0]["message"]
    assert conflict["planning_ready"] is False


def test_trip_intake_document_is_structured_and_has_plain_text_fallback():
    document = build_trip_intake_document(BASE_TRIP)

    assert document.type == "trip_intake"
    assert document.status == "collecting_required"
    assert document.route.origin == "北京"
    assert document.route.destination == "南京"
    assert document.progress.model_dump() == {"completed": 2, "total": 5}
    assert [item.label for item in document.missing_required] == ["出发日期", "行程时长", "出差目的"]
    assert document.missing_required[-1].options == ["客户拜访", "参加会议", "内部协作", "培训"]
    assert [item.label for item in document.optional] == ["客户或会议地点", "会面及工作时间"]
    assert "8月5日出发" in document.suggested_reply
    assert "已完成 2/5 项" in document.plain_text


def test_manager_builds_trip_intake_presentation_only_while_paused():
    paused = HommeyWebInstance._trip_intake_presentation({
        "results": [{
            "agent_name": "event_collection",
            "status": "success",
            "data": {**BASE_TRIP, **evaluate_trip_intake(BASE_TRIP)},
        }],
    })
    ready_trip = {
        **BASE_TRIP,
        "start_date": "2026-08-05",
        "duration_days": 2,
        "trip_purpose": "会议",
    }
    ready = HommeyWebInstance._trip_intake_presentation({
        "results": [{
            "agent_name": "event_collection",
            "status": "success",
            "data": {**ready_trip, **evaluate_trip_intake(ready_trip)},
        }],
    })

    assert paused["type"] == "trip_intake"
    assert ready is None


def test_stream_emits_presentation_document_without_duplicate_text():
    instance = object.__new__(HommeyWebInstance)
    document = build_trip_intake_document(BASE_TRIP).model_dump(mode="json")

    async def fake_process(*_args, **_kwargs):
        return {
            "response": document["plain_text"],
            "presentation_document": document,
            "agents": [{"name": "event_collection", "status": "success"}],
            "preferences_updated": False,
        }

    instance.process_message = fake_process

    async def collect():
        return [event async for event in instance.stream_message("帮我规划南京出差")]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == [
        "status", "agents", "presentation_document", "done",
    ]
    assert not any(event["type"] == "chunk" for event in events)


def test_legacy_planning_orchestrator_emits_live_task_progress():
    class ReplyAgent:
        async def reply(self, _message):
            return Msg(
                name="rag_knowledge",
                content=json.dumps({"answer": "制度证据"}, ensure_ascii=False),
                role="assistant",
            )

    orchestrator = OrchestrationAgent(
        agent_registry={"rag_knowledge": ReplyAgent()},
        memory_manager=None,
    )
    events = []

    async def progress(event):
        events.append(event.message_key)

    async def run():
        return await orchestrator.reply_with_progress(
            Msg(
                name="intention",
                content=json.dumps({
                    "agent_schedule": [{"agent_name": "rag_knowledge", "priority": 1}],
                }),
                role="assistant",
            ),
            progress,
        )

    asyncio.run(run())

    assert events == ["policy_searching", "task_completed"]
    assert RESILIENCE_CONFIG["request_timeout_sec"] >= 240
    assert TRIP_INTAKE_CONFIG["enabled"] is True


def test_file_memory_restores_presentation_document(tmp_path):
    memory = FileLongTermMemory("trip-card-user", storage_path=str(tmp_path))
    document = build_trip_intake_document(BASE_TRIP).model_dump(mode="json")
    memory.add_chat_message(
        "assistant",
        document["plain_text"],
        "session-1",
        {"request_id": "request-1", "presentation_document": document},
    )

    row = memory.get_chat_history(request_id="request-1")[0]
    assert row["presentation_document"]["type"] == "trip_intake"
    assert row["presentation_document"]["progress"] == {"completed": 2, "total": 5}


def test_frontend_has_non_cyclic_user_bubble_and_typed_renderer():
    root = Path(__file__).resolve().parents[1]
    css = (root / "webui_new/static/hommey.css").read_text(encoding="utf-8")
    app = (root / "webui_new/static/app.js").read_text(encoding="utf-8")
    card = (root / "webui_new/static/trip-intake-card.js").read_text(encoding="utf-8")

    assert ".message-row.user .msg-stack" in css
    assert "width: fit-content" in css
    assert "max-width: 100%" in css
    assert "renderUserMessageInto" in app
    assert "presentation_document" in app
    assert "collapseTripIntakeCards" in app
    assert "点击展开" in app
    assert "document.createElement" in card
    assert "innerHTML" not in card
