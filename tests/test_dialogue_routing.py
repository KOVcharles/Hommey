"""Behavior boundaries: input state, business writes, termination and presentation."""
from copy import deepcopy
import json

import pytest

from agent_runtime.contracts import SpecialistResult, ToolRejected
from agent_runtime.control import made_progress
from agent_runtime.engine import Supervisor, Turn
from agent_runtime.store import trip_version
from core.presentation.trip_intake_document import build_trip_intake_document
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply
from tests.test_supervisor_control import role_of


class ConversationStore(FakeStore):
    async def call(self, method, scope, *args):
        if method == "previous":
            rows = [r for (user, request), r in self.rows.items()
                    if user == scope.user_id and request != scope.request_id and r["session"] == scope.session_id]
            return deepcopy(rows[-1]) if rows else None
        return await super().call(method, scope, *args)


def scope(number, session=None):
    return SCOPE.model_copy(update={"request_id": f"request-{number}", "session_id": session or SCOPE.session_id})


class NoModel:
    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("this route must not invoke the model")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["1", "2", "999", "随便看看", "嗯", "？？", "北京"])
@pytest.mark.parametrize("active_trip", [False, True])
async def test_unbound_input_has_no_model_delegation_write_or_trip_card(text, active_trip):
    services, model = FakeServices(), NoModel()
    services.prefs = {"home_location": "北京"}
    if active_trip:
        services.trip = {"destination": "南京", "status": "active"}
    before = dict(services.trip)
    store = ConversationStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    result = await runtime.run(scope(1), text)
    assert model.calls == 0 and store.writes == 0 and services.calls == []
    assert services.trip == before and result["agents"] == []
    assert result["outcome"] == "waiting_input" and result["presentation_document"] is None
    assert "已保存" not in result["response"]
    assert (await runtime.run(scope(1), text))["idempotent_replay"]
    assert model.calls == 0


@pytest.mark.asyncio
async def test_explicit_empty_intake_can_show_form_but_profile_is_not_a_save_receipt():
    services, model = FakeServices(), NoModel()
    services.prefs = {"home_location": "北京"}
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "出差")
    assert result["presentation_document"]["route"]["origin"] == "北京"
    assert "已保存" not in result["response"] and model.calls == store.writes == 0


def duration_services():
    services = FakeServices()
    services.trip = {"origin": "北京", "destination": "南京", "start_date": "2026-09-20"}
    return services


@pytest.mark.asyncio
async def test_number_answers_displayed_duration_question_and_is_replayable():
    services, model = duration_services(), NoModel()
    store = ConversationStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    first = await runtime.run(scope(1), "出差")
    assert first["presentation_document"]["missing_required"][0]["key"] == "trip_length"
    second = await runtime.run(scope(2), "1")
    assert services.trip["duration_days"] == 1 and store.writes == 1 and model.calls == 0
    assert second["outcome"] == "waiting_input" and "已保存" in second["response"]
    assert (await runtime.run(scope(2), "1"))["idempotent_replay"]
    assert store.writes == 1
    # The primary question is now purpose, so another numeric answer cannot overwrite it.
    third = await runtime.run(scope(3), "2")
    assert third["presentation_document"] is None and store.writes == 1 and model.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["different_session", "changed_trip", "intervening_turn"])
async def test_pending_question_does_not_leak_across_contexts(invalidation):
    services, model = duration_services(), NoModel()
    store = ConversationStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    await runtime.run(scope(1), "出差")
    if invalidation == "changed_trip":
        services.trip["destination"] = "上海"
    if invalidation == "intervening_turn":
        await runtime.run(scope(2), "你好")
    result = await runtime.run(scope(3, "another-session" if invalidation == "different_session" else None), "1")
    assert result["presentation_document"] is None and "duration_days" not in services.trip
    assert model.calls == store.writes == 0


@pytest.mark.asyncio
async def test_invalid_duration_reasks_without_losing_the_pending_question():
    services, model = duration_services(), NoModel()
    store = ConversationStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    await runtime.run(scope(1), "出差")
    invalid = await runtime.run(scope(2), "91")
    assert invalid["presentation_document"] is None and store.writes == 0
    assert "行程" in invalid["response"] or "天数" in invalid["response"]
    await runtime.run(scope(3), "1")
    assert services.trip["duration_days"] == 1 and store.writes == 1 and model.calls == 0


@pytest.mark.asyncio
async def test_displayed_numbered_choices_bind_to_the_selected_label():
    services, seen = FakeServices(), []
    async def model(messages, **kwargs):
        context = json.loads(messages[1]["content"])["context"]
        seen.append(context.get("resolved_input"))
        if len(seen) == 1:
            return reply(("finish", {"kind": "clarify", "question": "你想先处理哪一项？",
                                      "pending_input": {"choices": ["差旅制度", "行程规划"]}}))
        return reply(("finish", {"kind": "ask", "question": "需要查询哪个城市的制度？"}))
    store = ConversationStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    first = await runtime.run(scope(1), "帮我处理一下这个事情")
    assert "1. 差旅制度" in first["response"] and "2. 行程规划" in first["response"]
    invalid = await runtime.run(scope(2), "3")
    assert len(seen) == 1 and "1. 差旅制度" in invalid["response"]
    await runtime.run(scope(3), "1")
    assert seen[-1] == {"choice": "差旅制度", "quote": "1"} and len(seen) == 2
    assert store.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("origin", "北京"), ("trip_purpose", "培训"),
                                         ("start_date", "明天"), ("trip_length", "明天"), ("duration_days", "两天")])
async def test_short_field_answer_keeps_original_text_and_reaches_extraction(field, value):
    services = FakeServices()
    from datetime import date, timedelta
    from core.trip_intake import beijing_today
    stored_field = "end_date" if field == "trip_length" else field
    stored_value = (date.fromisoformat(beijing_today()) + timedelta(days=1)).isoformat() if value == "明天" else 2 if value == "两天" else value
    async def model(messages, **kwargs):
        if role_of(messages) == "trip_context":
            context = json.loads(messages[1]["content"])
            assert context["request"] == value and context["resolved_input"]["field"] == field
            return reply(("report", {"summary": "已整理用户提供的字段", "data": {
                "trip": {stored_field: stored_value}, "field_sources": {stored_field: value}}}))
        context = json.loads(messages[1]["content"])
        if context["current_request"] != value:
            return reply(("finish", {"kind": "ask", "question": "请补充这个行程字段。", "pending_input": {"field": field}}))
        out = outputs(messages)
        if not out:
            return reply(("delegate", {"role": "trip_context", "task": "整理用户补充的出差信息"}))
        return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
    store = ConversationStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    await runtime.run(scope(1), "帮我整理出差信息")
    result = await runtime.run(scope(2), value)
    assert services.trip[stored_field] == stored_value and store.writes == 1
    assert result["presentation_document"]["type"] == "trip_intake"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "failed", "rejected"])
async def test_empty_or_failed_trip_does_not_become_saved_card(mode):
    services, counts = FakeServices(), {"main": 0, "child": 0}
    services.prefs = {"home_location": "北京"}
    async def model(messages, **kwargs):
        if role_of(messages) is None:
            counts["main"] += 1
            out = outputs(messages)
            if not out:
                return reply(("delegate", {"role": "trip_context", "task": "整理出差信息"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        counts["child"] += 1
        if mode == "failed":
            raise OSError("synthetic failure")
        return reply(("report", {"status": "success" if mode == "rejected" else "needs_input",
                                  "summary": "没有可提取信息", "data": {"trip": {}, "field_sources": {}}}))
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "帮我处理一下这个事情")
    assert result["presentation_document"] is None and "已保存" not in result["response"]
    assert store.writes == 0 and counts["child"] <= 2 and counts["main"] <= 2
    if mode != "failed":
        assert result["outcome"] == "waiting_input" and counts["main"] == 1


@pytest.mark.asyncio
async def test_clarify_is_terminal_even_with_existing_committed_trip_result():
    services = FakeServices()
    trip = {"destination": "南京"}
    result = SpecialistResult(result_id="r", role="trip_context", task="整理", summary="南京", data={"trip": trip},
                              input_version=trip_version(trip))
    turn = Turn(Supervisor(None, services, FakeStore(services), CONFIG), SCOPE, "什么意思", "什么意思", None)
    turn.state = {"trip": trip, "version": trip_version(trip), "results": {"r": result.model_dump()},
                  "applied_results": ["r"], "discarded_results": []}
    output = await turn.invoke_main({}, {"name": "finish", "arguments": {"kind": "clarify", "question": "你指的是什么？"}})
    assert output["_terminal"]["presentation_document"] is None
    assert "你指的是什么" in output["_terminal"]["response"]
    with pytest.raises(ToolRejected):
        await turn.invoke_main({}, {"name": "finish", "arguments": {"kind": "clarify", "question": "哪里？", "result_ids": ["r"]}})


def test_progress_is_based_on_operation_semantics():
    assert made_progress({"name": "read_source"}, {"id": "s", "data": "new evidence"})
    assert made_progress({"name": "apply_changes"}, {"applied": True})
    assert made_progress({"name": "delegate"}, {"result_id": "r", "status": "needs_input", "committed": True})
    for name, value in [("read_skill", {"guidance": "text"}), ("apply_changes", {"applied": True, "reused": True}),
                        ("delegate", {"result_id": "r", "status": "unavailable"}),
                        ("delegate", {"result_id": "r", "status": "needs_input"})]:
        assert not made_progress({"name": name}, value)


def test_card_title_requires_explicit_save_evidence():
    assert "已保存" not in build_trip_intake_document({}, home_location="北京").title
    assert "已保存" not in build_trip_intake_document({"destination": "南京"}).title
    assert "已保存" in build_trip_intake_document({"destination": "南京"}, saved=True).title


@pytest.mark.asyncio
async def test_prepare_options_cannot_create_empty_intake_from_unclear_task():
    calls = []
    async def model(*args, **kwargs):
        calls.append(True)
        return reply(("prepare_trip_options", {}))
    services = FakeServices()
    services.prefs = {"home_location": "北京"}
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "帮我处理一下这个事情")
    assert len(calls) == 1 and store.writes == 0 and services.calls == []
    assert result["presentation_document"] is None and result["outcome"] == "waiting_input"


@pytest.mark.asyncio
async def test_empty_changeset_stops_instead_of_reasking_model_to_save():
    counts = {"main": 0}
    async def model(messages, **kwargs):
        if role_of(messages) == "memory":
            return reply(("report", {"status": "needs_input", "summary": "尚无具体偏好变更", "evidence_refs": [],
                                      "missing_info": ["偏好"], "data": {"preferences": {}}}))
        counts["main"] += 1
        out = outputs(messages)
        if not out:
            return reply(("delegate", {"role": "memory", "task": "整理差旅偏好"}))
        return reply(("apply_changes", {"result_id": out[0]["result_id"]}))
    services = FakeServices()
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "帮我整理差旅偏好")
    assert counts["main"] == 2 and store.writes == 0
    assert result["outcome"] == "waiting_input" and result["presentation_document"] is None
    checkpoint = store.rows[(SCOPE.user_id, "request-1")]["checkpoint"]
    assert checkpoint["main"]["last_error"]["code"] == "EMPTY_CHANGESET"


@pytest.mark.asyncio
async def test_missing_trip_information_preserves_parallel_weather_answer():
    async def model(messages, **kwargs):
        role = role_of(messages)
        if role is None:
            return reply(("delegate", {"role": "trip_context", "task": "整理出差需求"}),
                         ("delegate", {"role": "travel_info", "task": "查询上海天气"}))
        if role == "trip_context":
            return reply(("report", {"status": "needs_input", "summary": "没有行程字段", "data": {"trip": {}, "field_sources": {}}}))
        out = outputs(messages)
        if not out:
            return reply(("get_weather", {"city": "上海"}))
        if len(out) == 1:
            return reply(("read_source", {"source_id": out[0]["sources"][0]["source_id"]}))
        ref = out[1]["id"]
        return reply(("report", {"summary": "上海晴，22至29度", "evidence_refs": [ref], "data": {"findings": [
            {"item": "天气", "conclusion": "上海晴，22至29度", "evidence_refs": [ref]}]}}))
    services = FakeServices()
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "整理我的出差需求并查上海天气")
    assert "上海晴" in result["response"] and result["answer_document"]["sources"]
    assert result["outcome"] == "waiting_input" and result["presentation_document"] is None
    assert store.writes == 0


@pytest.mark.asyncio
async def test_preparatory_skill_reads_do_not_erase_prior_failures():
    calls = []
    async def model(messages, **kwargs):
        if role_of(messages) == "memory":
            return reply(("report", {"status": "needs_input", "summary": "尚无偏好信息", "evidence_refs": [], "data": {}}))
        calls.append(True)
        if len(calls) == 1:
            return reply(("delegate", {"role": "memory", "task": "查询差旅偏好"}))
        if len(calls) == 2 or len(calls) == 6:
            return reply(("finish", {}))  # no results: invalid answer
        resource = ["", "references/place-selection.md", "references/travel-choices.md"][len(calls) - 3]
        return reply(("read_skill", {"name": "plan-trip", "resource": resource}))
    services = FakeServices()
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "帮我处理一下这个事情")
    assert len(calls) == 6 and result["stop_reason"] == "NO_PROGRESS"
    assert result["presentation_document"] is None and store.writes == 0


@pytest.mark.asyncio
async def test_reading_guides_cannot_replace_initial_intent_decision():
    calls = []
    async def model(messages, **kwargs):
        names = {t["function"]["name"] for t in kwargs["tools"]}
        assert names == {"delegate", "finish"}
        calls.append(True)
        # Even a provider ignoring the current schema cannot load guides.
        return reply(("read_skill", {"name": "plan-trip"}))
    services = FakeServices()
    store = ConversationStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(scope(1), "这个要怎么弄呢")
    assert len(calls) == 1 and result["presentation_document"] is None
    checkpoint = store.rows[(SCOPE.user_id, "request-1")]["checkpoint"]
    assert not checkpoint["main"].get("skills_read") and store.writes == 0
