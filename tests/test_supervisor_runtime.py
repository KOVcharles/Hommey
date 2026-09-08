"""Small offline smoke tests for the new runtime's real control flow."""
from copy import deepcopy
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_runtime.contracts import Report, Scope, SpecialistResult, ToolRejected, RuntimeStopped
from agent_runtime.engine import Supervisor
from agent_runtime.model_client import call_model
from agent_runtime.profiles import PROFILES
from agent_runtime.services import SourceScope
from agent_runtime.store import fingerprint, trip_version
from agent_runtime.validation import validated_changes, validate_report


CONFIG = {"main_rounds": 14, "child_rounds": 6, "max_children": 12, "parallel_children": 3,
    "child_timeout_sec": 2, "tool_timeout_sec": 1}
SCOPE = Scope(user_id="employee-a", session_id="session-a", request_id="request-a")
TEXT = "我从北京到上海出差，2026-09-15出发，2天，拜访客户。请查制度、记忆、天气，规划并检查合规。"


def reply(*calls):
    return {"content": [{"type": "tool_use", "id": f"c{i}", "name": name, "input": args} for i, (name, args) in enumerate(calls)]}


def outputs(messages):
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


class FakeServices:
    def __init__(self):
        self.trip = {}
        self.prefs = {}
        self.active = self.peak = 0
        self.calls = []
        self.memory = SimpleNamespace(long_term=SimpleNamespace(get_preference=lambda: dict(self.prefs)))

    async def context(self, scope):
        return {"trip": dict(self.trip), "recent": [{"role": "user", "content": "PARENT_HISTORY_SECRET"}], "session_summaries": []}

    async def execute(self, scope, name, args):
        self.calls.append((scope, name))
        self.active += 1
        self.peak = max(self.active, self.peak)
        await asyncio.sleep(0.01)
        self.active -= 1
        if name == "search_policy":
            return "policy", [{"content": "住宿需符合企业预算并保留发票", "metadata": {"title": "差旅制度"}}]
        if name == "search_memory":
            return "memory", [{"kind": "preferences", "data": {"seat_preference": "靠窗"}}]
        if name == "get_weather":
            return "weather", {"city": "上海", "forecasts": [{"date": "2026-09-15", "day_condition": "晴", "low_c": 22, "high_c": 29}]}
        raise AssertionError(name)


class FakeStore:
    """Test-only durable-store double; production never falls back to this."""
    def __init__(self, services):
        self.services, self.rows = services, {}
        self.writes = 0

    async def call(self, method, scope, *args):
        key = (scope.user_id, scope.request_id)
        row = self.rows.get(key)
        if method == "begin":
            if row and (row["hash"] != args[0] or row["session"] != scope.session_id):
                raise ToolRejected("request collision")
            if row is None:
                row = self.rows[key] = {"hash": args[0], "session": scope.session_id, "checkpoint": {}, "response": None, "receipts": {}}
            row.update(owner="owner", status="running", cancel=False)
            return deepcopy(row)
        if method == "previous":
            return None
        if method == "cancel":
            if row and row["status"] == "running" and row["session"] == scope.session_id:
                row["cancel"] = True
                return True
            return False
        if method == "stop":
            row["status"] = args[1]
            return
        if not row or row["cancel"] or row["owner"] != args[0]:
            raise RuntimeStopped("stopped")
        if method == "check":
            return
        if method == "save":
            row.update(checkpoint=deepcopy(args[1]), response=deepcopy(args[2]), status=args[3])
            return
        if method == "apply":
            _, operation, version, trip, prefs, action = args
            if operation in row["receipts"]:
                return deepcopy(row["receipts"][operation])
            if version != trip_version(self.services.trip):
                raise ToolRejected("stale trip")
            self.writes += 1
            self.services.trip.update(trip)
            if trip:
                self.services.trip["status"] = "active"
            self.services.prefs.update(prefs)
            receipt = {"applied": True, "trip": dict(self.services.trip), "version": trip_version(self.services.trip), "preferences_updated": bool(prefs)}
            row["receipts"][operation] = receipt
            return deepcopy(receipt)
        raise AssertionError(method)


class JourneyModel:
    def __init__(self):
        self.main_round = 0
        self.roles = set()
        self.fail_once = False

    async def __call__(self, messages, tools, tool_choice):
        assert tool_choice == "required"
        names = {t["function"]["name"] for t in tools}
        out = outputs(messages)
        if "delegate" in names:
            self.main_round += 1
            if self.fail_once and self.main_round == 2:
                raise OSError("simulated model disconnect")
            completed = [v for v in out if "result_id" in v]
            by_role = {v["role"]: v["result_id"] for v in completed}
            if "trip_context" not in by_role:
                return reply(("delegate", {"role": "trip_context", "task": "整理本次出差"}))
            if not any(v.get("applied") for v in out):
                return reply(("apply_changes", {"result_id": by_role["trip_context"]}))
            if "policy_rag" not in by_role:
                return reply(*[("delegate", {"role": role, "task": "查询本次企业差旅所需资料"}) for role in ("policy_rag", "memory", "travel_info")])
            if "trip_planner" not in by_role:
                return reply(("delegate", {"role": "trip_planner", "task": "安排差旅行程", "result_ids": list(by_role.values())}))
            if "compliance" not in by_role:
                return reply(("delegate", {"role": "compliance", "task": "检查方案与制度", "result_ids": [by_role["trip_planner"], by_role["policy_rag"]]}))
            return reply(("finish", {"result_ids": [by_role["trip_planner"], by_role["compliance"], by_role["travel_info"]]}))
        assert "PARENT_HISTORY_SECRET" not in json.dumps(messages)
        assert not names & {"delegate", "apply_changes", "discard_result", "finish", "execute", "mcp"}
        system = messages[0]["content"]
        role = next(role for role, profile in PROFILES.items() if profile.instructions in system)
        self.roles.add(role)
        assert names == set(PROFILES[role].tools) | {"read_skill", "report"}
        if role == "trip_context":
            trip = {"origin": "北京", "destination": "上海", "start_date": "2026-09-15", "duration_days": 2, "trip_purpose": "拜访客户"}
            return reply(("report", {"summary": "北京至上海出差两天，拜访客户。", "data": {"trip": trip, "field_sources": {k: TEXT for k in trip}}}))
        if role in {"policy_rag", "memory", "travel_info"}:
            if not out:
                query = {"policy_rag": ("search_policy", {"query": "差旅住宿制度"}), "memory": ("search_memory", {"kind": "preferences"}), "travel_info": ("get_weather", {"city": "上海"})}[role]
                return reply(query)
            if len(out) == 1:
                return reply(("read_source", {"source_id": out[0]["sources"][0]["source_id"]}))
            return reply(("report", {"summary": "已查询资料，具体适用条件仍需核对。", "evidence_refs": [out[1]["id"]]}))
        context = json.loads(messages[1]["content"])
        if role == "trip_planner":
            assert {r["role"] for r in context["dependencies"]} == {"trip_context", "policy_rag", "memory", "travel_info"}
            return reply(("report", {"summary": "计划拜访客户；住宿金额尚待确认。", "data": {"itinerary": {"days": [{"date": "2026-09-15", "activities": ["拜访客户"]}]}}}))
        refs = [ref for r in context["dependencies"] for ref in r["evidence_refs"]]
        return reply(("report", {"status": "partial", "summary": "缺少住宿金额，尚不能确定整体合规。", "evidence_refs": refs, "data": {"verdict": "unknown"}}))


@pytest.mark.asyncio
async def test_six_role_journey_parallel_isolation_cards_and_idempotency():
    services, model = FakeServices(), JourneyModel()
    store = FakeStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    result = await runtime.run(SCOPE, TEXT)
    assert model.roles == set(PROFILES)
    assert services.peak == 3
    assert store.writes == 1
    assert services.trip["status"] == "active"  # planning never means travelled/completed
    assert any(s["kind"] == "weather" and len(s["days"]) == 1 for s in result["answer_document"]["sections"])
    assert "拜访客户" in result["response"]
    before = model.main_round
    replay = await runtime.run(SCOPE, TEXT)
    assert replay["idempotent_replay"] and replay["response"] == result["response"]
    assert before == model.main_round and store.writes == 1
    with pytest.raises(ToolRejected):
        await runtime.run(SCOPE, TEXT + "变更内容")


@pytest.mark.asyncio
async def test_checkpoint_resumes_after_model_disconnect():
    services, model = FakeServices(), JourneyModel()
    model.fail_once = True
    store = FakeStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    with pytest.raises(OSError):
        await runtime.run(SCOPE, TEXT)
    assert len(store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["results"]) == 1
    output = await runtime.run(SCOPE, TEXT)
    assert output["engine"] == "supervisor" and store.writes == 1


@pytest.mark.asyncio
async def test_cancel_during_model_wait_and_wrong_session_rejected():
    entered = asyncio.Event()
    async def slow_model(*args, **kwargs):
        entered.set()
        await asyncio.sleep(10)
    services = FakeServices()
    store = FakeStore(services)
    runtime = Supervisor(slow_model, services, store, CONFIG)
    task = asyncio.create_task(runtime.run(SCOPE, TEXT))
    await entered.wait()
    assert not await runtime.cancel(SCOPE.model_copy(update={"session_id": "other"}))
    assert await runtime.cancel(SCOPE)
    output = await asyncio.wait_for(task, 2)
    assert output["interrupted"] and store.writes == 0


@pytest.mark.asyncio
async def test_out_of_domain_never_calls_model_or_business_tool():
    async def forbidden(*args, **kwargs):
        raise AssertionError("model should not run")
    services = FakeServices()
    result = await Supervisor(forbidden, services, FakeStore(services), CONFIG).run(SCOPE, "帮我写一段Python代码")
    assert "只处理企业差旅" in result["response"] and services.calls == []


def test_sources_are_private_and_policy_cannot_report_unread_evidence():
    a, b = SourceScope(), SourceScope()
    ref = a.add("policy", {"content": "制度"})["source_id"]
    with pytest.raises(ToolRejected):
        b.read(ref)
    report = Report(summary="制度结论", evidence_refs=[ref])
    with pytest.raises(ToolRejected):
        validate_report("policy_rag", report, a, [])
    a.read(ref)
    validate_report("policy_rag", report, a, [])
    with pytest.raises(ToolRejected):
        validate_report("compliance", Report(summary="合规", data={"verdict": "compliant"}), a, [])


def test_write_gate_rejects_invented_dates_fields_and_preferences():
    def proposed(data, role="trip_context"):
        return SpecialistResult(result_id="r", role=role, task="task", summary="summary", data=data)
    with pytest.raises(ToolRejected):
        validated_changes(proposed({"trip": {"start_date": "2026-09-15"}, "field_sources": {"start_date": "到上海出差"}}), "到上海出差", {}, {})
    with pytest.raises(ToolRejected):
        validated_changes(proposed({"trip": {"status": "completed"}}), TEXT, {}, {})
    with pytest.raises(ToolRejected):
        validated_changes(proposed({"preferences": {"seat_preference": "靠窗"}, "preference_sources": {"seat_preference": "到上海出差"}}, "memory"), "到上海出差", {}, {})
    trip, prefs, action = validated_changes(proposed({"preferences": {"seat_preference": "靠窗"}, "preference_sources": {"seat_preference": "我喜欢靠窗"}}, "memory"), "我喜欢靠窗", {}, {})
    assert prefs == {"seat_preference": "靠窗"} and trip == {}


@pytest.mark.asyncio
async def test_native_stream_uses_final_complete_call_and_rejects_repaired_json():
    async def model(*args, **kwargs):
        async def stream():
            yield reply(("finish", {"kind": "ask"}))
            yield reply(("finish", {"kind": "ask", "question": "何时出发？"}))
        return stream()
    parsed = await call_model(model, [], [])
    assert parsed.calls[0]["arguments"]["question"] == "何时出发？"
    async def invalid(*args, **kwargs):
        return {"content": [{"type": "tool_use", "id": "x", "name": "apply_changes", "input": {"result_id": "r"}, "raw_input": '{"result_id":"r"'}]}
    with pytest.raises(ToolRejected):
        await call_model(invalid, [], [])


@pytest.mark.asyncio
async def test_web_entry_pins_old_runs_and_new_entry_persists_documents():
    from unittest.mock import AsyncMock
    from webui_new.manager import HommeyWebInstance
    instance = HommeyWebInstance("employee-a")
    instance.initialized = True
    instance.session_id = "session-a"
    saved = []
    instance.memory_manager = SimpleNamespace(current_request_id=None,
        add_message=lambda role, content, metadata: saved.append((role, metadata)) or "message-id")
    response = {"response": "差旅结果", "answer_document": None, "presentation_document": None, "agents": [], "preferences_updated": False}
    instance.supervisor = SimpleNamespace(run=AsyncMock(return_value=response))
    instance.state_store = SimpleNamespace(get_active=AsyncMock(return_value=SimpleNamespace(status="WAITING_USER")))
    instance._process_legacy_message_impl = AsyncMock(return_value={"engine": "legacy"})
    assert (await instance._process_message_impl("继续", request_id="request-a"))["engine"] == "legacy"
    instance.supervisor.run.assert_not_awaited()
    instance.state_store.get_active.return_value = None
    result = await instance._process_message_impl("查询出差天气", request_id="request-a")
    assert result["response"] == "差旅结果"
    assert [role for role, _ in saved] == ["user", "assistant"]
    assert instance.session_id == "session-a"
    scope = instance.supervisor.run.call_args.args[0]
    assert scope.user_id == "employee-a" and scope.session_id == "session-a"


def test_checkpoint_redaction_keeps_json_replayable():
    from agent_runtime.store import safe_checkpoint
    payload = {"messages": [{"content": json.dumps({"note": "密码是secret-password", "city": "上海"}, ensure_ascii=False)}]}
    safe = safe_checkpoint(payload)
    value = json.loads(safe["messages"][0]["content"])
    assert "secret-password" not in value["note"] and value["city"] == "上海"
