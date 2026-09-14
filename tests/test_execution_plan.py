"""Execution invariants and compound routing, including cancellation/failure injection."""
import asyncio
from copy import deepcopy
import json
from contextlib import contextmanager

import pytest

from agent_runtime import execution_plan as plan
from agent_runtime.contracts import ToolRejected, WorkItem
from agent_runtime.engine import Supervisor, is_direct_policy_query
from agent_runtime.fast_routes import weather_policy_tasks
from agent_runtime.store import RunStore
from context.memory_repository import stable_uuid
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply
from tests.test_supervisor_control import role_of

TEXT = "给我查一下南京天气以及相关的差旅标准"


def state_with_step(role="travel_info", dependencies=None):
    state = {"work_items": {}, "results": {}, "applied_results": []}
    plan.register(state, "key", WorkItem(role=role, task="task", result_id="result_test",
        input_version=1, dependency_ids=dependencies or []).model_dump())
    return state


def test_success_requires_running_valid_result_and_trip_receipt():
    state = state_with_step("trip_context")
    with pytest.raises(ToolRejected, match="转换"):
        plan.transition(state, "result_test", "completed")
    plan.transition(state, "result_test", "running")
    with pytest.raises(ToolRejected, match="结果"):
        plan.transition(state, "result_test", "completed")
    state["results"]["result_test"] = {"role": "trip_context", "status": "success", "data": {"trip": {"origin": "北京"}}}
    with pytest.raises(ToolRejected, match="尚未保存"):
        plan.complete(state, "result_test")
    state["applied_results"].append("result_test")
    plan.complete(state, "result_test")
    with pytest.raises(ToolRejected):
        plan.transition(state, "result_test", "running")
    assert state["work_items"]["key"]["finished_at"]


def test_missing_or_failed_dependency_cannot_start():
    state = state_with_step(dependencies=["dependency"])
    with pytest.raises(ToolRejected):
        plan.transition(state, "result_test", "running")
    state["results"]["dependency"] = {"status": "unavailable"}
    with pytest.raises(ToolRejected):
        plan.transition(state, "result_test", "running")
    state["results"]["dependency"]["status"] = "partial"
    plan.transition(state, "result_test", "running")


def test_finish_does_not_hide_unexecuted_required_step():
    state = state_with_step()
    assert plan.settle(state, "completed") == "degraded"
    assert state["work_items"]["key"]["status"] == "failed"


def test_recovery_keeps_step_identity_and_is_bounded():
    state = state_with_step()
    state["children"] = {"child": {"result_id": "result_test"}}
    plan.settle(state, "cancelled")
    plan.resume(state)
    assert state["work_items"]["key"]["status"] == "pending"
    assert state["work_items"]["key"]["attempts"] == 2
    plan.settle(state, "cancelled")
    plan.resume(state)
    assert state["work_items"]["key"]["status"] == "failed"
    assert len(state["work_items"]) == 1


@pytest.mark.parametrize("text", [TEXT, "请帮我查询南京的天气和差旅制度。"])
def test_full_compound_route(text):
    assert not is_direct_policy_query(text)
    tasks = weather_policy_tasks(text)
    assert {t["role"] for t in tasks} == {"policy_rag", "travel_info"}
    assert all("南京" in t["task"] for t in tasks)


@pytest.mark.parametrize("text", [TEXT + "，再帮我规划行程", "不要查南京天气，只查差旅标准",
    "如果南京下雨再查差旅标准", "查询南京天气和上海差旅标准", "查询明天南京天气和差旅标准",
    "给我查一下南京天气以及相关的差旅标准并保存偏好", "南京天气", "查询南京和上海天气以及差旅标准"])
def test_compound_route_does_not_drop_extra_meaning(text):
    assert weather_policy_tasks(text) is None
    assert not is_direct_policy_query(text)


class Store(FakeStore):
    async def call(self, method, scope, *args):
        result = await super().call(method, scope, *args)
        if method == "stop" and len(args) > 2:
            self.rows[(scope.user_id, scope.request_id)]["checkpoint"] = deepcopy(args[2])
        return result


class Services(FakeServices):
    async def execute(self, scope, name, args):
        if name == "get_weather":
            assert args.city == "南京"
        kind, data = await super().execute(scope, name, args)
        if kind == "weather":
            data["city"] = "南京"
        return kind, data


class Model:
    def __init__(self, failed_role=None):
        self.roles = []
        self.failed_role = failed_role

    async def __call__(self, messages, **kwargs):
        role = role_of(messages)
        assert role in {"policy_rag", "travel_info"}, "Compound route must not call the main model"
        self.roles.append(role)
        scoped = json.loads(messages[1]["content"])["request"]
        assert "南京" in scoped
        if role == "travel_info":
            assert "差旅标准" not in scoped
        else:
            assert "天气" not in scoped
        out = outputs(messages)
        if not out:
            return reply(("get_weather", {"city": "南京"}) if role == "travel_info" else ("search_policy", {"query": "南京差旅标准"}))
        if role == self.failed_role:
            return reply(("report", {"status": "unavailable", "summary": "暂无可核实资料", "data": {"findings": []}, "evidence_refs": []}))
        ref = out[0]["sources"][0]["source_id"]
        if len(out) == 1:
            return reply(("read_source", {"source_id": ref}))
        fact = "南京晴，22至29度" if role == "travel_info" else "住宿需符合企业预算并保留发票"
        return reply(("report", {"summary": fact, "data": {"findings": [{"item": "天气" if role == "travel_info" else "住宿",
            "conclusion": fact, "evidence_refs": [ref]}]}, "evidence_refs": [ref]}))


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_role", [None, "policy_rag", "travel_info"])
async def test_both_intents_registered_before_io_and_independent_failure(failed_role):
    services, model = Services(), Model(failed_role)
    store = Store(services)
    events = []
    async def progress(event):
        if event["type"] == "execution_plan":
            saved = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["public_plan"]
            assert saved["revision"] >= event["revision"]  # persistence precedes publication
            if event["steps"] and all(s["status"] == "pending" for s in event["steps"]):
                assert len(event["steps"]) == 2 and not services.calls
            events.append(event)
    runtime = Supervisor(model, services, store, CONFIG)
    result = await runtime.run(SCOPE, TEXT, progress=progress)
    assert len(services.calls) == 2 and services.peak == 2 and store.writes == 0
    assert {r for r in model.roles} == {"policy_rag", "travel_info"}
    final = result["public_plan"]
    assert len(final["steps"]) == 2
    assert all(s["status"] not in {"pending", "running"} for s in final["steps"])
    assert len({e["revision"] for e in events}) == len(events)
    assert [e["revision"] for e in events] == sorted(e["revision"] for e in events)
    if failed_role:
        assert result["outcome"] == "degraded"
        assert {s["status"] for s in final["steps"]} == {"failed", "succeeded"}
        assert ("南京晴" if failed_role == "policy_rag" else "住宿需符合") in result["response"]
    else:
        assert result["outcome"] == "completed"
        assert "南京晴" in result["response"] and "住宿需符合" in result["response"]
    count = len(model.roles)
    replay = await runtime.run(SCOPE, TEXT)
    assert replay["public_plan"] == final and len(model.roles) == count


@pytest.mark.asyncio
async def test_timeout_cancels_both_children_and_persists_terminal_plan():
    services = Services()
    store = Store(services)
    active = 0
    async def blocked_model(messages, **kwargs):
        nonlocal active
        active += 1
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1
    result = await Supervisor(blocked_model, services, store, {**CONFIG, "turn_timeout_sec": 0.06}).run(SCOPE, TEXT)
    assert active == 0
    assert result["stop_reason"] == "TURN_TIMEOUT"
    assert all(s["status"] == "failed" for s in result["public_plan"]["steps"])


@pytest.mark.asyncio
async def test_disconnect_persists_cancelled_steps():
    services = Services()
    store = Store(services)
    started = asyncio.Event()
    async def blocked_model(messages, **kwargs):
        started.set()
        await asyncio.sleep(10)
    runtime = Supervisor(blocked_model, services, store, CONFIG)
    task = asyncio.create_task(runtime.run(SCOPE, TEXT))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = store.rows[(SCOPE.user_id, SCOPE.request_id)]
    assert row["status"] == "interrupted"
    assert row["checkpoint"]["public_plan"]["status"] == "cancelled"
    assert all(s["status"] == "cancelled" for s in row["checkpoint"]["public_plan"]["steps"])


@pytest.mark.asyncio
async def test_extra_intent_preserves_complete_input_in_main_path():
    services = Services()
    store = Store(services)
    text = TEXT + "，再帮我规划行程"
    async def model(messages, **kwargs):
        assert role_of(messages) is None
        assert json.loads(messages[1]["content"])["current_request"] == text
        return reply(("finish", {"kind": "ask", "question": "请补充出发日期和出差目的"}))
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, text)
    assert result["outcome"] == "waiting_input" and not services.calls


@pytest.mark.asyncio
async def test_form_progress_and_receipt_precede_first_model_call():
    services = Services()
    store = Store(services)
    events = []
    async def progress(event):
        if event["type"] == "execution_plan":
            events.append(event)
            for step in event["steps"]:
                if step["status"] == "succeeded":
                    assert store.writes == 1
    async def model(messages, **kwargs):
        assert store.writes == 1
        assert events[-1]["steps"][0]["status"] == "succeeded"
        return reply(("finish", {"kind": "ask", "question": "请补充会议时间"}))
    await Supervisor(model, services, store, CONFIG).run(SCOPE,
        "从北京出发，目的地：南京，2026-09-11出发，出差2天，出差目的：参加会议", progress=progress)
    assert any(e["steps"] and e["steps"][0]["status"] == "pending" for e in events)
    assert any(e["steps"] and e["steps"][0]["status"] == "running" for e in events)


def test_recovery_query_is_scoped_and_fenced_projection_does_not_block_resume():
    state = state_with_step()
    plan.transition(state, "result_test", "running")
    saved = plan.snapshot(state, "request-a")
    class Pool:
        @contextmanager
        def connection(self):
            yield self
        @contextmanager
        def cursor(self):
            yield self
        def execute(self, sql, params):
            self.sql, self.params = sql, params
        def fetchall(self):
            return [{"plan": deepcopy(saved), "status": "interrupted"}]
    pool = Pool()
    plans = RunStore(pool)._public_plans("employee-a", "session-a")
    assert pool.params == ("employee-a", stable_uuid("session-a", namespace="session"))
    assert "WHERE user_id=%s AND session_id=%s" in pool.sql
    assert "checkpoint->'public_plan'" in pool.sql and "retention_until>NOW()" in pool.sql
    assert plans[0]["status"] == "cancelled" and plans[0]["steps"][0]["status"] == "cancelled"
    assert saved["status"] == "running"
    resumed = plan.snapshot(state, "request-a")
    assert saved["revision"] < plans[0]["revision"] < resumed["revision"]
