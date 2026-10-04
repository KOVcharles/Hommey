"""Generic clarification cards: bounded contract, native tool, durable lifecycle."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent_runtime.engine import Supervisor
from context.long_term_memory import FileLongTermMemory
from core.presentation.information_request import RequestInformation
from tests.test_intake_lifecycle import instance_with
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply
from webui_new.core.errors import BusinessError


QUESTION = {"title": "确认适用的差旅条件", "description": "先确认**人员类别**，才能查找适用条款。",
    "fields": [{"key": "personnel_type", "label": "人员类别", "input_type": "single_select",
                "options": ["教职工", "学生", "暂不确定"]}]}


@pytest.mark.parametrize("change", [
    {"fields": []}, {"fields": QUESTION["fields"] * 6},
    {"fields": QUESTION["fields"] * 2}, {"title": " "}, {"title": "长" * 61},
    {"fields": [{**QUESTION["fields"][0], "options": ["教职工", "教职工"]}]},
    {"fields": [{**QUESTION["fields"][0], "options": ["教职工"]}]},
    {"fields": [{**QUESTION["fields"][0], "options": list("一二三四五六七八九")}]},
    {"fields": [{**QUESTION["fields"][0], "input_type": "html"}]},
    {"fields": [{**QUESTION["fields"][0], "key": "personnel type"}]},
    {"fields": [{**QUESTION["fields"][0], "default": "学生"}]},
    {"fields": [{**QUESTION["fields"][0], "input_type": "date"}]},
])
def test_rejects_invalid_or_preselected_questions(change):
    with pytest.raises(ValidationError):
        RequestInformation.model_validate({**deepcopy(QUESTION), **change})


def test_plain_text_retains_questions_options_and_help():
    data = deepcopy(QUESTION)
    data["fields"].append({"key": "date", "label": "出发日期", "input_type": "date", "required": False,
        "help_text": "日期尚未确定可以不填。"})
    result = RequestInformation.model_validate(data).output()
    assert result["outcome"] == "waiting_input"
    assert result["response"] == result["presentation_document"]["plain_text"]
    assert "教职工 / 学生 / 暂不确定 / 自行填写" in result["response"]
    assert "出发日期（选填）" in result["response"]
    assert "日期尚未确定可以不填。" in result["response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_outer_save", [False, True])
async def test_native_tool_waits_without_business_writes_and_replays_once(lost_outer_save):
    services = FakeServices()
    store = FakeStore(services)
    calls = 0

    async def model(messages, tools, **kwargs):
        nonlocal calls
        calls += 1
        assert "request_information" in {tool["function"]["name"] for tool in tools}
        return reply(("request_information", QUESTION))

    runtime = Supervisor(model, services, store, CONFIG)
    result = await runtime.run(SCOPE, "确认适用人员类别")
    assert result["presentation_document"]["type"] == "information_request"
    assert result["outcome"] == "waiting_input"
    assert not services.calls and store.writes == 0
    if lost_outer_save:
        store.rows[(SCOPE.user_id, SCOPE.request_id)]["response"] = None
    again = await runtime.run(SCOPE, "确认适用人员类别")
    assert again["response"] == result["response"] and calls == 1


@pytest.mark.asyncio
async def test_terminal_question_cannot_be_batched_with_another_tool():
    services = FakeServices()
    store = FakeStore(services)

    async def model(messages, **kwargs):
        if not outputs(messages):
            return reply(("request_information", QUESTION), ("request_trip_details", {}))
        assert all(value.get("failure", {}).get("code") == "EXCLUSIVE_TOOL" for value in outputs(messages))
        return reply(("request_information", QUESTION))

    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "确认我的类别")
    assert result["outcome"] == "waiting_input"


def source():
    return {"role": "assistant", "request_id": "source", "content": "补充信息",
        "presentation_document": {**RequestInformation.model_validate(QUESTION).output()["presentation_document"],
                                  "interaction_id": "source"}}


def test_session_owned_form_allows_only_current_card_or_identical_retry():
    row = source()
    assert instance_with([row])._validate_intake_submission("source", "submit", "人员类别：学生", "session") == "information_request"
    submitted = {"role": "user", "request_id": "submit", "content_type": "form_submission", "content": "人员类别：学生"}
    instance = instance_with([row, submitted])
    instance._validate_intake_submission("source", "submit", "人员类别：学生", "session")
    for card_id, request_id, text in [("foreign", "submit", "人员类别：学生"),
            ("source", "changed", "人员类别：学生"), ("source", "submit", "人员类别：教职工")]:
        with pytest.raises(BusinessError):
            instance._validate_intake_submission(card_id, request_id, text, "session")
    with pytest.raises(BusinessError):
        instance_with([row, {"role": "user", "content": "先查天气"}])._validate_intake_submission("source", "new", "人员类别：学生", "session")


def test_history_restores_card_submission_and_read_only_state(tmp_path):
    memory = FileLongTermMemory("employee", storage_path=str(tmp_path))
    memory.add_chat_message("assistant", "补充信息", "session", {"request_id": "source", "presentation_document": source()["presentation_document"]})
    memory.add_chat_message("user", "人员类别：学生", "session", {"request_id": "submit", "content_type": "form_submission"})
    rows = FileLongTermMemory("employee", storage_path=str(tmp_path)).get_chat_history(session_id="session")
    restored = instance_with(rows).get_chat_session("session")["messages"][0]["presentation_document"]
    assert restored["archived"] and restored["submitted_text"] == "人员类别：学生"
    assert restored["interaction_id"] == "source"
    legacy = source(); legacy.pop("request_id"); legacy["presentation_document"].pop("interaction_id")
    assert instance_with([legacy]).get_chat_session("session")["messages"][0]["presentation_document"]["archived"]
    with pytest.raises(BusinessError):
        instance_with([legacy])._validate_intake_submission("None", "submit", "人员类别：学生", "session")


@pytest.mark.asyncio
async def test_submission_and_following_card_are_saved_with_correct_metadata():
    from types import SimpleNamespace
    instance = instance_with([source()])
    saved = []
    instance.memory_manager.add_message = lambda role, text, metadata: saved.append((role, text, metadata)) or "id"

    async def run(scope, text, **kwargs):
        assert text == "人员类别：学生"
        return RequestInformation.model_validate(QUESTION).output()

    instance.supervisor = SimpleNamespace(run=run)
    result = await instance._process_message_impl("人员类别：学生", request_id="next", intake_request_id="source",
        session_id="session", request_memory=instance.memory_manager)
    assert saved[0][2]["content_type"] == "form_submission"
    assert saved[1][2]["presentation_document"]["interaction_id"] == "next"
    assert result["presentation_document"]["interaction_id"] == "next"


@pytest.mark.asyncio
async def test_stream_delivers_one_presentation_without_duplicate_text():
    instance = instance_with([])
    async def process(message, **kwargs):
        return RequestInformation.model_validate(QUESTION).output()
    instance.process_message = process
    events = [event async for event in instance.stream_message("确认人员类别", session_id="session")]
    assert sum(event["type"] == "presentation_document" for event in events) == 1
    assert not any(event["type"] == "chunk" for event in events)
    assert events[-1]["outcome"] == "waiting_input"
