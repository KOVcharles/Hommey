"""Context completeness, historical evidence access, and current-turn isolation."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from agent_runtime.context_window import conversation_window, trip_facts
from agent_runtime.contracts import Finish, SpecialistResult, ToolRejected
from agent_runtime.engine import Supervisor, Turn
from agent_runtime.render import render
from agent_runtime.store import trip_version
from tests.test_dialogue_routing import ConversationServices, ConversationStore, exchange, scope
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, reply, outputs, model_payload
from tests.test_supervisor_control import role_of


def result(key="history", version=0, **kwargs):
    return SpecialistResult(result_id=key, input_version=version, role="travel_info",
                            task="上海天气", summary="历史天气", **kwargs)


def make_turn(reports):
    services = FakeServices()
    turn = Turn(Supervisor(None, services, FakeStore(services), CONFIG), SCOPE, "追问", "追问", None)
    turn.state = {"trip": {}, "version": trip_version({}),
                  "results": {r.result_id: r.model_dump() for r in reports},
                  "applied_results": [], "discarded_results": [], "work_items": {}}
    return turn


def test_long_multi_result_delivery_keeps_question_and_last_exchange_intact():
    reports = [SpecialistResult(result_id=role, role=role, task="完整出差", summary="已核实的内容。" * 250)
               for role in ("travel_info", "policy_rag", "trip_planner", "compliance")]
    question = "请问您从哪个城市出发前往南京？"
    delivered = render(Finish(kind="ask", question=question), reports)["response"]
    assert len(delivered) > 1400 and delivered.endswith(question)
    rows = [{"role": "user", "content": "很早之前" * 3000},
            {"role": "assistant", "content": "更早回复"},
            {"role": "user", "content": "请规划南京出差"},
            {"role": "assistant", "content": delivered},
            {"role": "user", "content": "北京"}]
    window = conversation_window(rows, "北京", budget=1000)
    assert window == rows[-3:]
    assert question in window[-2]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("discarded", [False, True])
async def test_expired_stale_result_and_sources_readable_but_not_deliverable(discarded):
    source = {"id": "src_old", "kind": "weather", "data": {"city": "上海"},
              "retrieved_at": (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()}
    turn = make_turn([result(sources=[source], evidence_refs=[source["id"]])])
    if discarded:
        turn.state["discarded_results"] = ["history"]
    read = await turn.invoke_main({}, {"name": "read_result", "arguments": {"result_id": "history"}})
    assert read["stale"] and read["expired"] and read["discarded"] == discarded
    assert read["status"] == turn.work_index()[0]["status"] == "success"
    source_read = await turn.invoke_main({}, {"name": "read_source", "arguments": {"source_id": source["id"]}})
    assert source_read["stale"] and source_read["expired"] and source_read["data"]["city"] == "上海"
    with pytest.raises(ToolRejected):
        await turn.invoke_main({}, {"name": "finish", "arguments": {"result_ids": ["history"]}})
    with pytest.raises(ToolRejected):
        await turn.invoke_main({}, {"name": "apply_changes", "arguments": {"result_id": "history"}})
    with pytest.raises(ToolRejected):
        await turn.invoke_main({}, {"name": "read_source", "arguments": {"source_id": "another_users_source"}})
    # Expiry alone is sufficient, even when the version matches.
    turn.state["results"]["history"]["input_version"] = turn.state["version"]
    turn.state["discarded_results"] = []
    with pytest.raises(ToolRejected, match="过期"):
        turn.results(["history"])


@pytest.mark.asyncio
async def test_ordinary_followup_reads_previous_work_without_replaying_children_or_quotas():
    services = ConversationServices()
    store = ConversationStore(services)
    seen = []
    async def model(messages, **kwargs):
        assert role_of(messages) is None
        payload = json.loads(messages[1]["content"])
        seen.append(deepcopy(payload))
        if not outputs(messages):
            assert not payload["work"]
            assert payload["reference_results"][0]["result_id"] == "history" and payload["reference_results"][0]["stale"]
            return reply(("read_result", {"result_id": "history"}))
        assert outputs(messages)[0]["summary"] == "历史天气"
        return reply(("finish", {"kind": "ask", "question": "历史资料已过期，需要查询哪天？"}))
    historical = result()
    store.rows[(SCOPE.user_id, "previous")] = {"session": SCOPE.session_id, "status": "interrupted",
        "checkpoint": {"results": {historical.result_id: historical.model_dump()}, "calls": 999,
                       "work_items": {"old": {"status": "failed"}},
                       "children": {"1:c0": {"result_id": "history", "messages": ["OLD_TRANSCRIPT"]}}}}
    output = await Supervisor(model, services, store, CONFIG).run(scope(1), "刚才的结果有什么限制？")
    assert len(seen) == 2 and output["outcome"] == "waiting_input"
    checkpoint = store.rows[(SCOPE.user_id, "request-1")]["checkpoint"]
    assert checkpoint["children"] == {} and checkpoint["work_items"] == {} and checkpoint["calls"] == 0
    assert "history" in checkpoint["results"] and not services.calls


@pytest.mark.asyncio
async def test_form_payload_full_fields_and_repeated_form_are_idempotent_across_turns():
    services, payloads = ConversationServices(), []
    store = ConversationStore(services)
    fields = {"origin": "北京", "destination": "南京"}
    text = "用户填写的行程表单：" + json.dumps(fields, ensure_ascii=False)
    async def model(messages, **kwargs):
        payloads.append(model_payload(messages))
        return reply(("request_trip_details", {}))
    runtime = Supervisor(model, services, store, CONFIG)
    for number in (1, 2):
        services.history.append({"role": "user", "content": "提交表单"})
        output = await runtime.run(scope(number), text, trip_input=fields)
        services.history.append({"role": "assistant", "content": output["response"]})
    assert store.writes == 1 and len(payloads) == 2
    assert all(set(p) == {"context_kind", "request_id", "dialogue_message_count", "facts", "work", "reference_results", "conversation", "today"} for p in payloads)
    assert all(p["conversation"][-1]["content"] == text for p in payloads)
    assert payloads[-1]["facts"] == fields
    assert len(payloads[-1]["work"]) == 1 and len(payloads[-1]["reference_results"]) == 1
    assert "status" not in payloads[-1]["facts"]


@pytest.mark.asyncio
async def test_attachment_text_is_available_to_parent_and_extractor():
    services, seen = FakeServices(), []
    async def model(messages, **kwargs):
        payload = model_payload(messages)
        seen.append(payload)
        assert "附件中的会议安排" in payload["conversation"][-1]["content"]
        if role_of(messages) == "trip_context":
            return reply(("report", {"status": "needs_input", "summary": "请确认附件中的行程信息", "data": {"trip": {}, "field_sources": {}}}))
        if not outputs(messages):
            return reply(("delegate", {"role": "trip_context", "task": "核对附件行程"}))
        return reply(("finish", {"kind": "ask", "question": "请确认附件安排是否用于这次出差"}))
    output = await Supervisor(model, services, FakeStore(services), CONFIG).run(
        SCOPE, "请看附件\n附件中的会议安排", user_text="请看附件")
    assert len(seen) == 3 and output["outcome"] == "waiting_input"


@pytest.mark.asyncio
async def test_apply_updates_visible_facts_and_staleness_before_next_model_call():
    services = ConversationServices()
    store = ConversationStore(services)
    old = result(version=trip_version({}))
    store.rows[(SCOPE.user_id, "old")] = {"session": SCOPE.session_id, "status": "completed",
        "checkpoint": {"results": {old.result_id: old.model_dump()}}}
    observed = []
    async def model(messages, **kwargs):
        if role_of(messages) == "trip_context":
            return reply(("report", {"summary": "目的地改为南京", "data": {
                "trip": {"destination": "南京"}, "field_sources": {"destination": "南京"}}}))
        payload = json.loads(messages[1]["content"])
        observed.append(deepcopy(payload))
        if not outputs(messages):
            return reply(("delegate", {"role": "trip_context", "task": "修改目的地"}))
        return reply(("finish", {"kind": "ask", "question": "何时出发？"}))
    await Supervisor(model, services, store, CONFIG).run(scope(1), "目的地改为南京")
    assert len(observed) == 2
    assert observed[0]["reference_results"][0]["stale"] is False
    assert observed[1]["reference_results"][0]["stale"] is True and observed[1]["facts"]["destination"] == "南京"


def test_facts_exclude_internal_provider_and_storage_data():
    assert trip_facts({"destination": "南京", "_trip_id": "private", "status": "active",
                       "work_location_verified": {"raw": "provider record"}}) == {"destination": "南京"}


def test_old_committed_trip_cannot_label_new_empty_form_as_saved():
    old = SpecialistResult(result_id="old", role="trip_context", task="历史行程", summary="已保存",
                           input_version=0, data={"trip": {"destination": "南京"}})
    turn = make_turn([old])
    turn.state["applied_results"] = ["old"]
    assert "已保存" not in turn.intake()["response"]


@pytest.mark.asyncio
async def test_incomplete_trip_can_be_delegated_for_partial_planning():
    services, called = FakeServices(), []
    services.trip = {"destination": "南京"}
    async def model(messages, **kwargs):
        if role_of(messages) == "trip_planner":
            called.append(True)
            return reply(("report", {"status": "needs_input", "summary": "日期未知，无法给出逐日计划",
                "missing_info": ["日期"], "data": {"itinerary": {"days": []}}}))
        if not outputs(messages):
            return reply(("delegate", {"role": "trip_planner", "task": "评估现有信息能安排的工作"}))
        return reply(("finish", {"kind": "ask", "question": "请补充出发日期"}))
    output = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "先评估目前能安排哪些工作")
    assert called == [True] and output["outcome"] == "waiting_input"


@pytest.mark.asyncio
async def test_legacy_checkpoint_resume_keeps_operation_identity_and_pending_question():
    services, seen = FakeServices(), []
    store = FakeStore(services)
    text = "请帮我选择"
    from agent_runtime.store import fingerprint
    version = trip_version({})
    stored = SpecialistResult(result_id="result_intake_submission", role="trip_context",
        task="保存行程", summary="已保存", input_version=version)
    checkpoint = {"trip": {}, "version": version, "results": {stored.result_id: stored.model_dump()},
        "children": {}, "calls": 0, "applied_results": [stored.result_id], "discarded_results": [],
        "preferences_updated": False, "work_items": {"intake_submission": {
            "role": "trip_context", "result_id": stored.result_id, "status": "completed", "input_version": version}},
        "pending_input": None, "resolved_input": None, "work_context": {}, "reply_to": None,
        "choice_output": None, "checkpoint_version": 2, "intake_submission_applied": True,
        "main": {"round": 1, "outputs": {}, "pending": [{"id": "old_call", "name": "finish",
            "arguments": {"kind": "ask", "question": "您想选哪项？", "pending_input": {"choices": ["制度", "规划"]}}}],
            "messages": [{"role": "system", "content": "旧提示"},
                         {"role": "user", "content": json.dumps({"context": {"recent": []}, "current_request": text})}]}}
    store.rows[(SCOPE.user_id, SCOPE.request_id)] = {"hash": fingerprint({"text": text, "user_text": text}),
        "session": SCOPE.session_id, "checkpoint": checkpoint, "response": None, "receipts": {}}
    async def forbidden(*args, **kwargs):
        seen.append(True)
        raise AssertionError("Saved pending call should finish without a model")
    output = await Supervisor(forbidden, services, store, CONFIG).run(SCOPE, text)
    assert not seen and "1. 制度" in output["response"] and store.writes == 0
    saved = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    assert saved["applied_results"] == [stored.result_id]
    assert "pending_input" not in saved and "work_context" not in saved
    assert set(json.loads(saved["main"]["messages"][1]["content"])) == {"facts", "work", "conversation", "today"}


@pytest.mark.asyncio
async def test_repeated_verified_form_and_literal_entry_preserve_provider_identity():
    from tests.test_trip_choices import trip
    from webui_new.quick_trip import build_quick_trip_message
    services = ConversationServices()
    store = ConversationStore(services)
    async def model(messages, **kwargs):
        return reply(("request_trip_details", {}))
    runtime = Supervisor(model, services, store, CONFIG)
    fields = trip()
    fields["capability_selection"] = {"include": ["weather"], "exclude": ["train"]}
    text = build_quick_trip_message(fields)
    await runtime.run(scope(1), text, trip_input=fields)
    original = deepcopy(services.trip)
    original_version = trip_version(original)
    await runtime.run(scope(2), text, trip_input=fields)
    await runtime.run(scope(3), "我要去重庆出差")
    assert services.trip == original and trip_version(services.trip) == original_version and store.writes == 1


@pytest.mark.asyncio
async def test_historical_failed_proposal_does_not_block_unrelated_new_query():
    services = ConversationServices()
    store = ConversationStore(services)
    failed = SpecialistResult(result_id="old_proposal", role="trip_context", task="旧的修改",
        summary="未保存", status="error", input_version=trip_version({}), data={"trip": {"destination": "上海"}})
    store.rows[(SCOPE.user_id, "old")] = {"session": SCOPE.session_id, "status": "failed",
        "checkpoint": {"results": {failed.result_id: failed.model_dump()}}}
    seen = []
    async def model(messages, **kwargs):
        if role_of(messages) == "memory":
            seen.append(True)
            return reply(("report", {"status": "needs_input", "summary": "需要明确查询年份", "evidence_refs": [], "data": {}}))
        if not outputs(messages):
            return reply(("delegate", {"role": "memory", "task": "查询历史出差记录"}))
        return reply(("finish", {"kind": "ask", "question": "需要查询哪一年？"}))
    output = await Supervisor(model, services, store, CONFIG).run(scope(1), "帮我找历史出差记录")
    assert seen == [True] and output["outcome"] == "waiting_input" and store.writes == 0


def test_empty_successful_plan_remains_invalid():
    from pydantic import ValidationError
    from agent_runtime.contracts import PlanReport
    with pytest.raises(ValidationError):
        PlanReport(status="success", summary="已完成", data={"itinerary": {"days": []}})


@pytest.mark.asyncio
async def test_business_context_reads_whole_messages_without_summary_pipeline():
    from contextlib import contextmanager
    from types import SimpleNamespace
    from agent_runtime.services import BusinessServices
    content = "已核实内容" * 400 + "从哪里出发？"
    statements = []
    class Database:
        @contextmanager
        def connection(self):
            yield self

        @contextmanager
        def cursor(self):
            yield self

        def execute(self, sql, params):
            statements.append((sql, params))

        def fetchall(self):
            return [{"role": "user", "content": "北京"}, {"role": "assistant", "content": content}]
    memory = SimpleNamespace(long_term=SimpleNamespace(pool=Database()), get_active_trip=lambda **kwargs: {})
    context = await BusinessServices(memory).context(SCOPE)
    assert context["recent"][0]["content"] == content and len(content) > 1400
    assert len(statements) == 1 and "session_summaries" not in statements[0][0]
    assert statements[0][1][0] == SCOPE.user_id
