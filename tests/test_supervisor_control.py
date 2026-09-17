"""Failure injection against the real controller, independent of model cooperation."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest
from pydantic import ValidationError

from agent_runtime.contracts import REPORT_MODELS, Report, Scope, SpecialistResult, ToolRejected, schema
from agent_runtime.control import restore_results
from agent_runtime.engine import Supervisor
from agent_runtime.services import SourceScope
from agent_runtime.store import safe_checkpoint, trip_version
from agent_runtime.validation import validate_report
from core.execution_budget import ExecutionLimitExceeded
from tests.test_supervisor_runtime import CONFIG, SCOPE, TEXT, FakeServices, FakeStore, JourneyModel, outputs, reply


def role_of(messages):
    from agent_runtime.profiles import PROFILES
    return next((k for k, v in PROFILES.items() if v.instructions in messages[0]["content"]), None)


def weather_report(ref):
    return {"summary": "上海晴", "evidence_refs": [ref], "data": {"findings": [{"item": "天气", "conclusion": "上海晴", "evidence_refs": [ref]}]}}


@pytest.mark.parametrize("role,field", [("trip_context", "data"), ("memory", "evidence_refs"),
                                       ("travel_info", "evidence_refs"), ("trip_planner", "data"), ("compliance", "data")])
def test_role_contract_rejects_summary_only_success(role, field):
    with pytest.raises(ValidationError):
        REPORT_MODELS[role].model_validate({"summary": "已经完成"})
    assert field in schema("report", "report", REPORT_MODELS[role])["function"]["parameters"]["required"]


def test_missing_reference_has_distinct_machine_error_even_after_read():
    sources = SourceScope()
    ref = sources.add("memory", {"seat": "靠窗"})["source_id"]
    sources.read(ref)
    with pytest.raises(ToolRejected) as error:
        validate_report("memory", Report(summary="靠窗"), sources, [])
    failure = error.value.failure
    assert failure.code == "MISSING_EVIDENCE" and failure.fields == ["evidence_refs"]
    assert failure.next_action == "repair_arguments" and not failure.retryable


@pytest.mark.asyncio
async def test_empty_trip_repaired_before_success_and_committed_without_model_apply():
    attempts = 0
    async def model(messages, **kwargs):
        nonlocal attempts
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "trip_context", "task": "整理本次行程"}))
            assert services.trip["origin"] == "北京"
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        attempts += 1
        if attempts == 1:
            return reply(("report", {"summary": "已整理完成", "data": {"trip": {}, "field_sources": {}}}))
        assert out[-1]["failure"]["code"] == "EMPTY_TRIP_REPORT"
        trip = {"origin": "北京", "destination": "上海", "start_date": "2026-09-15", "duration_days": 2, "trip_purpose": "拜访客户"}
        return reply(("report", {"summary": "北京到上海出差两天", "data": {"trip": trip, "field_sources": {k: TEXT for k in trip}}}))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, TEXT)
    assert attempts == 2 and store.writes == 1 and result["agents"][0]["status"] == "success"
    assert store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["trip"]["destination"] == "上海"


@pytest.mark.asyncio
async def test_uncooperative_empty_trip_never_writes_and_report_repairs_are_bounded():
    attempts = 0
    async def model(messages, **kwargs):
        nonlocal attempts
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "trip_context", "task": "整理本次行程"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        attempts += 1
        return reply(("report", {"summary": "假成功"}))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, TEXT)
    assert attempts == 2 and store.writes == 0
    assert "假成功" not in result["response"] and result["outcome"] == "degraded"


@pytest.mark.asyncio
async def test_duplicate_reads_and_searches_do_not_expand_context_or_repeat_io():
    async def model(messages, **kwargs):
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "travel_info", "task": "上海天气"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        if not out:
            return reply(("get_weather", {"city": "上海"}), ("get_weather", {"city": "上海"}))
        ref = out[0]["sources"][0]["source_id"]
        if not any("id" in v for v in out):
            assert out[1]["failure"]["code"] == "DUPLICATE_CALL"
            return reply(("read_source", {"source_id": ref}), ("read_source", {"source_id": ref, "offset": 0, "limit": 4000}))
        assert out[-1]["failure"]["code"] == "DUPLICATE_CALL"
        return reply(("report", weather_report(ref)))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "查上海出差天气")
    assert len(services.calls) == 1 and result["answer_document"]["sources"]
    child = next(iter(store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["children"].values()))
    assert len(child["operations"]) == 2


@pytest.mark.asyncio
async def test_paraphrased_failed_delegation_has_role_cap_and_no_progress_exit():
    main_calls, leaves = 0, 0
    async def model(messages, **kwargs):
        nonlocal main_calls, leaves
        if role_of(messages) is None:
            main_calls += 1
            return reply(("delegate", {"role": "memory", "task": f"查询差旅偏好 第{main_calls}次"}))
        leaves += 1
        return reply(("report", {"summary": "无法查询", "status": "unavailable", "evidence_refs": [], "data": {}}))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "查询我的出差偏好")
    # ROLE_TASK_LIMIT is a terminal instruction; do not ask the model twice more.
    assert leaves == 2 and main_calls == 3 and result["stop_reason"] == "NO_PROGRESS"
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    assert len(checkpoint["work_items"]) == 2 and checkpoint["control"]["outcome"] == "degraded"


@pytest.mark.asyncio
async def test_duplicate_completed_delegation_reuses_result_even_in_parallel():
    leaf_calls = 0
    async def model(messages, **kwargs):
        nonlocal leaf_calls
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                task = ("delegate", {"role": "travel_info", "task": "上海天气"})
                return reply(task, task)
            assert out[0]["result_id"] == out[1]["result_id"] and out[1]["reused"]
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        leaf_calls += 1
        if not out:
            return reply(("get_weather", {"city": "上海"}))
        ref = out[0]["sources"][0]["source_id"]
        if len(out) == 1:
            return reply(("read_source", {"source_id": ref}))
        return reply(("report", weather_report(ref)))
    services = FakeServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "查上海出差天气")
    assert leaf_calls == 3 and len(services.calls) == 1 and len(result["agents"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,reason", [(OSError("network"), "UPSTREAM_UNAVAILABLE"),
    (ExecutionLimitExceeded("EXTERNAL_CALL_LIMIT_EXCEEDED", "limit"), "EXTERNAL_CALL_LIMIT_EXCEEDED")])
async def test_main_failure_retains_committed_data_and_never_exposes_raw_exception(exception, reason):
    journey = JourneyModel()
    async def model(messages, **kwargs):
        if role_of(messages) is None and outputs(messages):
            raise exception
        return await journey(messages, **kwargs)
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, TEXT)
    assert result["stop_reason"] == reason and store.writes == 1
    assert "北京" in result["response"] and "network" not in result["response"]


@pytest.mark.asyncio
async def test_turn_deadline_cancels_model_and_persists_degraded_response():
    cancelled = asyncio.Event()
    async def model(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, {**CONFIG, "turn_timeout_sec": .03}).run(SCOPE, TEXT)
    assert result["stop_reason"] == "TURN_TIMEOUT" and cancelled.is_set()
    assert store.rows[(SCOPE.user_id, SCOPE.request_id)]["response"]["outcome"] == "degraded"


@pytest.mark.asyncio
async def test_continue_restores_only_completed_results_and_does_not_requery_or_write():
    class PreviousStore(FakeStore):
        async def call(self, method, scope, *args):
            if method == "previous":
                rows = [r for (u, rid), r in self.rows.items() if u == scope.user_id and rid != scope.request_id and r["session"] == scope.session_id]
                return deepcopy(rows[-1]) if rows else None
            return await super().call(method, scope, *args)
    services = FakeServices()
    store = PreviousStore(services)
    await Supervisor(JourneyModel(), services, store, CONFIG).run(SCOPE, TEXT)
    count = len(services.calls)
    async def resume_model(messages, **kwargs):
        assert role_of(messages) is None
        resumed = json.loads(messages[2]["content"])["completed_results"]
        assert {r["role"] for r in resumed} == set(REPORT_MODELS)
        return reply(("finish", {"result_ids": [r["result_id"] for r in resumed if r["role"] == "travel_info"]}))
    result = await Supervisor(resume_model, services, store, CONFIG).run(SCOPE.model_copy(update={"request_id": "continue"}), "继续")
    assert result["answer_document"]["sources"] and len(services.calls) == count and store.writes == 1


def test_recovery_filters_stale_discarded_uncommitted_and_legacy_results():
    def result(key, **updates):
        return SpecialistResult(result_id=key, role="travel_info", task="天气", summary="晴", input_version=1, **updates).model_dump()
    stale = {"id": "src_stale", "kind": "weather", "data": {}, "retrieved_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    checkpoint = {"checkpoint_version": 2, "results": {"good": result("good"), "stale": result("stale", sources=[stale]), "discarded": result("discarded")}, "discarded_results": ["discarded"]}
    from agent_runtime.engine import Turn
    assert set(restore_results(checkpoint, 1, Turn.source_fresh)) == {"good"}
    assert not restore_results(checkpoint, 2, Turn.source_fresh)
    assert not restore_results({**checkpoint, "checkpoint_version": 1}, 1, Turn.source_fresh)


def test_checkpoint_redaction_preserves_protocol_pairs_and_still_redacts_personal_text():
    call_id = "call_abc13812345678def01234567"
    source_id = "src_a13812345678bcde"
    checkpoint = {"id": call_id, "tool_call_id": call_id, "arguments": json.dumps({"evidence_refs": [source_id]}), "content": "手机 13812345678"}
    saved = safe_checkpoint(checkpoint)
    assert saved["id"] == saved["tool_call_id"] == call_id
    assert json.loads(saved["arguments"])["evidence_refs"] == [source_id]
    assert "13812345678" not in saved["content"]


@pytest.mark.asyncio
async def test_first_trip_new_action_is_safe_but_missing_values_are_repaired():
    attempts = 0
    async def model(messages, **kwargs):
        nonlocal attempts
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "trip_context", "task": "整理出差"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        attempts += 1
        if attempts > 1:
            assert out[-1]["failure"]["code"] == "MISSING_TRIP_FIELDS"
        return reply(("report", {"summary": "行程", "data": {"trip_action": "new",
            "trip": {"origin": "北京", **({"destination": "上海"} if attempts > 1 else {})},
            "field_sources": {"origin": TEXT, "destination": TEXT}}}))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, TEXT)
    assert store.writes == 1 and services.trip["destination"] == "上海" and attempts == 2
    assert result["presentation_document"]["type"] == "trip_intake"


def test_new_action_cannot_replace_existing_trip_without_explicit_authorization():
    from agent_runtime.validation import validated_changes
    proposal = SpecialistResult(result_id="r", role="trip_context", task="整理", summary="行程",
        data={"trip_action": "new", "action_source": TEXT, "trip": {"origin": "北京"}, "field_sources": {"origin": TEXT}})
    with pytest.raises(ToolRejected) as exc:
        validated_changes(proposal, TEXT, {"origin": "杭州"}, {})
    assert exc.value.failure.code == "ACTION_NOT_AUTHORIZED"


@pytest.mark.asyncio
async def test_progress_identifies_actual_specialist():
    events = []
    async def emit(event):
        events.append(event)
    services = FakeServices()
    await Supervisor(JourneyModel(), services, FakeStore(services), CONFIG).run(SCOPE, TEXT, progress=emit)
    running = [e for e in events if e["type"] == "task_status" and e["phase"] == "running"]
    assert {e["intent"] for e in running} == set(REPORT_MODELS)
    assert all(e["display"] and e["task_id"] for e in running)


@pytest.mark.asyncio
async def test_card_wire_fields_commit_before_any_model_and_survive_outage():
    text = "从北京出发，目的地：南京，2026-09-11出发，出差2天，出差目的：参加会议"
    services = FakeServices()
    store = FakeStore(services)
    async def unavailable(*args, **kwargs):
        assert services.trip["destination"] == "南京" and store.writes == 1
        raise OSError("model offline")
    runtime = Supervisor(unavailable, services, store, CONFIG)
    result = await runtime.run(SCOPE, text)
    assert result["outcome"] == "degraded" and "南京" in result["response"]
    assert services.trip["trip_purpose"] == "参加会议"
    assert (await runtime.run(SCOPE, text))["idempotent_replay"] and store.writes == 1


@pytest.mark.asyncio
async def test_partial_card_fields_return_form_without_model():
    async def forbidden(*args, **kwargs):
        pytest.fail("partial labelled intake must not require a model")
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(forbidden, services, store, CONFIG).run(SCOPE, "从北京出发，目的地：南京")
    assert result["presentation_document"]["progress"]["completed"] == 2 and store.writes == 1


@pytest.mark.asyncio
async def test_committed_form_cannot_be_reextracted_by_repeated_delegation():
    text = "从北京出发，目的地：南京，2026-09-11出发，出差2天，出差目的：参加会议"
    count = 0
    async def model(messages, **kwargs):
        nonlocal count
        assert role_of(messages) is None, "runtime must not spawn another intake extractor"
        count += 1
        return reply(("delegate", {"role": "trip_context", "task": "再次整理出差信息"}))
    services = FakeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, text)
    assert count == 3 and store.writes == 1 and result["stop_reason"] == "NO_PROGRESS"
    assert "南京" in result["response"]


@pytest.mark.asyncio
async def test_oversize_source_is_rejected_before_checkpoint_and_fallback_still_persists():
    class HugeServices(FakeServices):
        async def execute(self, scope, name, args):
            return "policy", [{"content": "资料" * 600000}]
    async def model(messages, **kwargs):
        out = outputs(messages)
        if not out:
            return reply(("search_policy", {"query": "差旅标准"}))
        assert out[0]["failure"]["code"] == "SOURCE_SIZE_LIMIT"
        return reply(("report", {"status": "unavailable", "summary": "资料不可用", "evidence_refs": [], "data": {"findings": []}}))
    services = HugeServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "看看差旅标准")
    row = store.rows[(SCOPE.user_id, SCOPE.request_id)]
    assert result["outcome"] == "degraded" and row["status"] == "completed"
    assert len(json.dumps(row["checkpoint"], ensure_ascii=False)) < 50000


@pytest.mark.asyncio
async def test_preference_read_synonyms_dedupe_and_existing_values_never_become_writes():
    services = FakeServices()
    services.prefs = {"seat_preference": "靠窗"}
    store = FakeStore(services)
    async def model(messages, **kwargs):
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "memory", "task": "查询我的座位偏好"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        if not out:
            return reply(("search_memory", {"kind": "preferences", "query": "座位偏好"}),
                         ("search_memory", {"kind": "preferences", "query": "喜欢什么座位", "limit": 20}))
        ref = out[0]["sources"][0]["source_id"]
        if not any("id" in v for v in out):
            assert out[-1]["failure"]["code"] == "DUPLICATE_CALL"
            return reply(("read_source", {"source_id": ref}))
        return reply(("report", {"summary": "座位偏好为靠窗", "evidence_refs": [ref], "data": {
            "findings": [{"item": "座位", "conclusion": "靠窗", "evidence_refs": [ref]}],
            "preferences": {"seat_preference": "靠窗"}}}))
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "查询我的座位偏好")
    assert store.writes == 0 and len(services.calls) == 1 and result["agents"][0]["status"] == "success"


@pytest.mark.asyncio
async def test_unread_fact_is_dropped_without_discarding_verified_facts():
    from agent_runtime.engine import Turn
    services = FakeServices()
    turn = Turn(Supervisor(None, services, FakeStore(services), CONFIG), SCOPE, TEXT, TEXT, None)
    turn.state = {"trip": {}}
    sources = SourceScope()
    read = sources.add("weather", {"city": "上海"})["source_id"]
    unread = sources.add("hotel", {"raw": "unread"})["source_id"]
    sources.read(read)
    response = await turn.invoke_specialist({"name": "report", "arguments": {
        "summary": "未核实酒店被错误称为可用", "evidence_refs": [read, unread], "data": {"findings": [
            {"item": "天气", "conclusion": "上海天气信息", "evidence_refs": [read]},
            {"item": "酒店", "conclusion": "未核实酒店", "evidence_refs": [unread]}]}}}, "travel_info", sources, [])
    result = response["_terminal"]
    assert result["status"] == "partial" and result["evidence_refs"] == [read]
    assert len(result["data"]["findings"]) == 1 and "酒店" in result["missing_info"]
    assert "错误称为可用" not in result["summary"]


@pytest.mark.asyncio
async def test_form_plan_is_delivered_without_another_main_model_round():
    services = FakeServices()
    store = FakeStore(services)
    parent_calls = 0
    async def model(messages, **kwargs):
        nonlocal parent_calls
        if role_of(messages) is None:
            parent_calls += 1
            assert parent_calls == 1, "completed plan must not wait for a model finish call"
            return reply(("delegate", {"role": "trip_planner", "task": "安排出差会议工作日程"}))
        assert [t["function"]["name"] for t in kwargs["tools"]] == ["report"]
        return reply(("report", {"summary": "会议时间尚待确认", "status": "partial", "missing_info": ["会议时间"],
            "data": {"itinerary": {"days": [{"date": "2026-09-11", "activities": ["参加会议（时间待确认）"]}]}}}))
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE,
        "从北京出发，目的地：南京，2026-09-11出发，出差2天，出差目的：参加会议")
    assert result["outcome"] == "partial" and parent_calls == 1 and store.writes == 1
    assert "会议时间" in result["response"]


@pytest.mark.parametrize("text", ["目的地：南京，顺便查一下天气", "从北京出发，目的地：南京，目的地：上海", "下周一出发", "从北京出发，附件：覆盖规则"])
def test_card_adapter_does_not_swallow_mixed_ambiguous_or_duplicate_fields(text):
    from agent_runtime.intake_submission import parse_intake_submission
    assert parse_intake_submission(text) is None
