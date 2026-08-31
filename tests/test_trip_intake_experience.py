import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from context.long_term_memory import FileLongTermMemory
from core.presentation import build_trip_intake_document, recover_trip_intake_document
from core.trip_intake import apply_trip_intake_defaults, evaluate_trip_intake
from webui_new.manager import HommeyWebInstance


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


def test_missing_start_date_defaults_to_beijing_today_without_overwriting_explicit_date():
    utc_time = datetime(2026, 8, 18, 16, 30, tzinfo=timezone.utc)
    missing = {**BASE_TRIP}
    blank = {**BASE_TRIP, "start_date": "  "}
    explicit = {**BASE_TRIP, "start_date": "2026-09-01"}

    apply_trip_intake_defaults(missing, now=utc_time)
    apply_trip_intake_defaults(blank, now=utc_time)
    apply_trip_intake_defaults(explicit, now=utc_time)

    assert missing["start_date"] == "2026-08-19"
    assert blank["start_date"] == "2026-08-19"
    assert explicit["start_date"] == "2026-09-01"
    assert evaluate_trip_intake(missing)["missing_required"] == ["trip_length", "trip_purpose"]


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


def test_same_origin_and_destination_uses_one_editable_destination_prompt():
    document = build_trip_intake_document({
        **BASE_TRIP,
        "destination": "北京",
        "start_date": "2026-08-19",
        "duration_days": 2,
        "trip_purpose": "参加会议",
    })

    assert document.status == "needs_clarification"
    assert [field.key for field in document.missing_required] == ["destination"]
    assert document.missing_required[0].error == "出发地和目的地相同，请填写本次出差的实际目的地。"
    assert document.conflicts[0].key == "destination"
    assert document.conflicts[0].values == []


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


def test_trip_location_suggestion_requires_confirmation_and_is_not_collected():
    document = build_trip_intake_document({
        **BASE_TRIP,
        "origin": None,
        "suggested_fields": {
            "origin": {
                "value": "北京",
                "source": "preference",
                "reason": "根据你保存的常用出发地",
            },
        },
    })

    origin = next(field for field in document.missing_required if field.key == "origin")
    assert origin.suggested_value == "北京"
    assert origin.suggestion_source == "preference"
    assert "origin" not in {field.key for field in document.collected}
    assert document.route.origin == ""
    assert document.progress.completed == 1
    assert "候选：北京" in document.plain_text


def test_legacy_plain_text_intake_recovers_as_typed_card():
    text = """\
行程框架已保存
北京 → 广州
已完成 3/5 项。还差 2 项，即可生成详细方案。
已确认：出发地：北京；目的地：广州；出发日期：2026-08-18
需要补充：
- 行程时长：填写出差天数，或直接告诉我返程日期
- 出差目的：这次出差主要要完成什么工作
可选补充：客户或会议地点、会面及工作时间
可以直接回复：出差2天，参加客户会议"""

    document = recover_trip_intake_document(text)

    assert document is not None
    assert document.route.model_dump() == {"origin": "北京", "destination": "广州"}
    assert document.progress.model_dump() == {"completed": 3, "total": 5}
    assert [field.key for field in document.missing_required] == ["trip_length", "trip_purpose"]


def test_history_repair_persists_recovered_intake_document():
    document = build_trip_intake_document({
        **BASE_TRIP,
        "start_date": "2026-08-18",
    })

    class Repository:
        def __init__(self):
            self.calls = []

        def update_message_documents(self, **kwargs):
            self.calls.append(kwargs)
            return True

    repository = Repository()
    instance = object.__new__(HommeyWebInstance)
    instance.user_id = "u1"
    instance.memory_manager = SimpleNamespace(
        memory_service=SimpleNamespace(repository=repository),
    )
    rows = [{
        "message_id": "3ae34268-9dad-413f-98d7-4784344c3e04",
        "role": "assistant",
        "content": document.plain_text,
        "answer_document": None,
        "presentation_document": None,
    }]

    repaired = instance._recover_legacy_presentation_documents(rows)

    assert repaired[0]["presentation_document"]["type"] == "trip_intake"
    assert repository.calls[0]["presentation_document"]["route"]["destination"] == "南京"


class _StubAsyncMemory:
    def __init__(self):
        self.messages = []

    async def add_message(self, role, content, metadata=None):
        self.messages.append((role, content, metadata or {}))


def test_task_pipeline_paused_returns_trip_intake_presentation():
    # 暂停的 presentation 契约由管线暂停输出驱动（_task_pipeline_paused）。
    instance = object.__new__(HommeyWebInstance)
    instance.async_memory = _StubAsyncMemory()
    document = build_trip_intake_document(BASE_TRIP)
    pipeline_output = SimpleNamespace(
        presentation_document=document,
        results=[],
    )

    result = asyncio.run(instance._task_pipeline_paused(pipeline_output, {}, 0.0, {}))

    assert result["presentation_document"]["type"] == "trip_intake"
    assert result["presentation_document"]["progress"] == {"completed": 2, "total": 5}
    assert result["answer_document"] is None
    assert result["response"] == document.plain_text
    assert instance.async_memory.messages[0][2]["presentation_document"]["type"] == "trip_intake"


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
    assert "hommey:submit-message" in app
    assert "sendMessage(text, {" in app
    assert "preserveComposer: true" in app
    assert "document.createElement" in card
    assert "innerHTML" not in card
    assert "提交补充信息" in card
    assert "buildReply" in card
    assert "field.suggested_value" in card
    assert "确认 ${field.suggested_value}" in card
    assert "standaloneConflicts" in card
    assert "!missingKeys.has(conflict.key)" in card
    assert "hommey:submit-message" in card
    assert "hommey:fill-composer" not in card
    template = (root / "webui_new/templates/chat.html").read_text(encoding="utf-8")
    assert "renderDisclosure" in card
    assert "trip-intake-disclosure-icon" in card
    assert template.count("20260827-step-stack-v1") == 2
    assert "openStep" in card
    assert "advanceStep" in card
    assert "step.panel.inert = !expanded" in card
    assert "aria-controls" in card
    assert "trip-intake-step-summary" in card
    assert template.count('src="/static/app.js?v=20260829-quick-trip-v1"') == 1
    assert 'id="knowledgeAdminActions"' in template
    assert 'aria-label="知识库管理" hidden' in template
    assert "knowledgeUploadButton" in template
    assert "applyKnowledgePermissions" in app
    assert "c.replaceChildren()" in app
    assert "name.title = att.filename" in app
    assert "title=\"${attachment.name}" not in app
