"""Durable Run/Turn/Goal/Node pause, resume and multi-intent contracts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.orchestration.lifecycle import ExecutionLifecycle
from core.orchestration.models import PauseInfo
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.state import WaitState
from core.orchestration.state_store import OrchestrationStateStore, StateConflictError
from core.orchestration.turn_resolver import TurnResolver


PLAN_QUERY = "我明天去上海出差3天，帮我规划一下行程"
PLAN_INTENTION = {
    "intents": [{"type": "itinerary_planning", "confidence": 0.9, "should_call_skill": True}],
    "key_entities": {"destination": "上海", "date": "明天", "duration": "3天"},
    "rewritten_query": PLAN_QUERY,
}


def _store(tmp_path):
    return OrchestrationStateStore("u1", postgres_dsn="", storage_dir=str(tmp_path))


def test_pause_persists_canonical_run_state(tmp_path):
    async def runner(**kwargs):
        assert kwargs["agent_name"] == "event_collection"
        return {"status": "success", "data": {
            "destination": "上海", "start_date": "2026-08-08",
            "duration_days": 3, "trip_purpose": "客户拜访",
            "missing_fields": ["origin"], "planning_ready": False,
        }}

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    output = asyncio.run(pipeline.run(
        PLAN_QUERY, PLAN_INTENTION, {}, session_id="session-a", request_id="req-1",
    ))

    state = asyncio.run(store.get_active("session-a"))
    assert output.paused is True
    assert state.status == "WAITING_USER"
    assert state.current_request_id == "req-1"
    assert state.goals["itinerary_planning"].status == "WAITING_USER"
    node = state.nodes["itinerary_planning-event_collection"]
    assert node.status == "WAITING_USER"
    assert node.operation_id == f"{state.run_id}:itinerary_planning-event_collection"
    assert state.waits[0].expected_fields == ["origin"]


def test_resume_injects_new_input_and_reuses_operation_id(tmp_path):
    seen = []

    async def runner(**kwargs):
        agent = kwargs["agent_name"]
        seen.append((
            agent,
            kwargs["context"]["active_task"]["query"],
            kwargs["task_params"]["operation_id"],
        ))
        if agent == "event_collection":
            ready = "北京" in kwargs["context"]["active_task"]["query"]
            return {"status": "success", "data": {
                "origin": "北京" if ready else "", "destination": "上海",
                "start_date": "2026-08-08", "duration_days": 3,
                "trip_purpose": "客户拜访", "missing_fields": [] if ready else ["origin"],
                "planning_ready": ready,
            }}
        if agent == "rag_knowledge":
            return {"status": "success", "data": {"answer": "住宿标准400元/晚。"}}
        if agent == "information_query":
            return {"status": "success", "data": {"results": {"summary": "晴"}}}
        if agent == "itinerary_planning":
            return {"status": "success", "data": {"itinerary": {"title": "上海出差方案"}}}
        if agent == "trip_compliance":
            return {"status": "success", "data": {"compliant": True}}
        raise AssertionError(agent)

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    first = asyncio.run(pipeline.run(
        PLAN_QUERY, PLAN_INTENTION, {}, session_id="session-a", request_id="req-1",
    ))
    state = asyncio.run(store.get_active("session-a"))
    operation_id = state.nodes["itinerary_planning-event_collection"].operation_id
    output = asyncio.run(pipeline.resume_run(
        state, "我从北京出发", {}, request_id="req-2",
    ))

    final_state = asyncio.run(store.get(state.run_id))
    assert first.paused is True
    assert output.answer_document is not None
    assert final_state.status == "COMPLETED"
    event_calls = [item for item in seen if item[0] == "event_collection"]
    assert event_calls[-1][1] == "我从北京出发"
    assert event_calls[0][2] == event_calls[-1][2] == operation_id


def test_waiting_goal_does_not_truncate_independent_goal(tmp_path):
    executed = []

    async def runner(**kwargs):
        agent = kwargs["agent_name"]
        executed.append(agent)
        if agent == "event_collection":
            return {"status": "success", "data": {
                "destination": "上海", "missing_fields": ["origin"],
                "planning_ready": False,
            }}
        if agent == "information_query":
            return {"status": "success", "data": {"results": {"summary": "晴"}}}
        raise AssertionError(f"waiting planning goal should not run {agent}")

    intention = {
        "intents": [
            {"type": "itinerary_planning", "confidence": .9, "should_call_skill": True},
            {"type": "information_query", "confidence": .9, "should_call_skill": True},
        ],
        "key_entities": {"destination": "上海"},
        "rewritten_query": "规划上海行程并查天气",
    }
    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    output = asyncio.run(pipeline.run(
        "规划上海行程并查天气", intention, {},
        session_id="session-a", request_id="req-multi",
    ))

    state = asyncio.run(store.get_active("session-a"))
    assert output.paused is True
    # Both priority-1 nodes are intentionally concurrent; completion order is
    # not part of the contract.
    assert set(executed) == {"event_collection", "information_query"}
    assert state.nodes["information_query-information_query"].status == "SUCCEEDED"
    assert state.nodes["itinerary_planning-rag_knowledge"].status == "READY"


def test_new_goal_is_appended_to_same_waiting_run(tmp_path):
    async def runner(**kwargs):
        if kwargs["agent_name"] == "event_collection":
            return {"status": "success", "data": {
                "destination": "上海", "missing_fields": ["origin"],
                "planning_ready": False,
            }}
        if kwargs["agent_name"] == "memory_query":
            return {"status": "success", "data": {"answer": "上次去了深圳"}}
        raise AssertionError(kwargs["agent_name"])

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    asyncio.run(pipeline.run(
        PLAN_QUERY, PLAN_INTENTION, {}, session_id="session-a", request_id="req-1",
    ))
    waiting = asyncio.run(store.get_active("session-a"))
    memory_intention = {
        "intents": [{"type": "memory_query", "confidence": .9, "should_call_skill": True}],
        "key_entities": {}, "rewritten_query": "我上次去了哪里",
    }
    side_answer = asyncio.run(pipeline.run(
        "我上次去了哪里", memory_intention, {}, session_id="session-a",
        request_id="req-2", existing_state=waiting,
    ))

    state = asyncio.run(store.get(waiting.run_id))
    assert side_answer.answer_document is not None
    assert state.run_id == waiting.run_id
    assert state.status == "WAITING_USER"
    assert state.goals["itinerary_planning"].status == "WAITING_USER"
    assert state.goals["memory_query"].status == "SUCCEEDED"


def test_turn_request_id_is_idempotent_and_stale_interrupt_is_rejected(tmp_path):
    store = _store(tmp_path)
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-1", original_query="查天气",
        intention_data={}, semantic_tasks=[{
            "task_id": "information_query", "intent": "information_query",
            "query": "查天气", "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 0,
        }], node_ids=["information_query-information_query"],
        graph_hash="hash", skill_versions={"query-info": "1.0.0"},
    ))
    first_turn = state.current_turn_id
    same = asyncio.run(store.start_turn(state.run_id, "req-1", "查天气"))
    assert same.current_turn_id == first_turn

    second = asyncio.run(store.start_turn(state.run_id, "req-2", "继续"))
    with pytest.raises(StateConflictError):
        asyncio.run(store.request_interrupt(
            state.run_id, second.current_turn_id, request_id="req-1",
        ))
    interrupted = asyncio.run(store.request_interrupt(
        state.run_id, second.current_turn_id, request_id="req-2",
    ))
    assert interrupted.status == "INTERRUPTING"
    assert asyncio.run(store.should_interrupt(state.run_id, second.current_turn_id)) is True


def test_concurrent_duplicate_turn_creates_one_durable_turn(tmp_path):
    store = _store(tmp_path)
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-1", original_query="查天气",
        intention_data={}, semantic_tasks=[{
            "task_id": "information_query", "intent": "information_query",
            "query": "查天气", "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 0,
        }], node_ids=["information_query-information_query"],
        graph_hash="hash", skill_versions={},
    ))

    def start_duplicate():
        return asyncio.run(store.start_turn(state.run_id, "req-2", "继续"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _index: start_duplicate(), range(8)))

    assert len({row.current_turn_id for row in rows}) == 1
    turn_files = list((store._dir / "turns").glob("turn_*.json"))
    assert len(turn_files) == 2  # initial Turn + exactly one resumed Turn


def test_start_turn_resumes_only_selected_waiting_goal(tmp_path):
    store = _store(tmp_path)
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-1", original_query="规划两趟行程",
        intention_data={},
        semantic_tasks=[
            {
                "task_id": goal_id, "intent": "itinerary_planning",
                "query": query, "entities": {}, "depends_on": [],
                "side_effect": False, "failure_policy": "continue", "display_order": index,
            }
            for index, (goal_id, query) in enumerate((
                ("trip_nanjing", "规划南京行程"),
                ("trip_shanghai", "规划上海行程"),
            ))
        ],
        node_ids=["trip_nanjing-event_collection", "trip_shanghai-event_collection"],
        node_goals={
            "trip_nanjing-event_collection": "trip_nanjing",
            "trip_shanghai-event_collection": "trip_shanghai",
        },
        graph_hash="hash", skill_versions={},
    ))

    def wait_for_both(old):
        old.status = "WAITING_USER"
        old.focused_goal_id = "trip_nanjing"
        for goal in old.goals.values():
            goal.status = "WAITING_USER"
        for node in old.nodes.values():
            node.status = "WAITING_USER"
        old.waits = [
            WaitState(goal_id="trip_nanjing", expected_fields=["duration"]),
            WaitState(goal_id="trip_shanghai", expected_fields=["origin"]),
        ]

    asyncio.run(store.mutate(state.run_id, wait_for_both))
    resumed = asyncio.run(store.start_turn(
        state.run_id, "req-2", "1天", goal_ids=["trip_nanjing"],
    ))

    assert resumed.nodes["trip_nanjing-event_collection"].status == "READY"
    assert resumed.goals["trip_nanjing"].status == "RUNNING"
    assert resumed.nodes["trip_shanghai-event_collection"].status == "WAITING_USER"
    assert resumed.goals["trip_shanghai"].status == "WAITING_USER"
    assert [wait.goal_id for wait in resumed.waits] == ["trip_shanghai"]


def test_mark_waiting_preserves_sibling_wait_across_turns(tmp_path):
    # P1-5：同一 run 跨轮连续暂停多个 goal 时，mark_waiting 必须合并而非覆盖
    # 上一轮的 WaitState（不变量：waits 含每个 WAITING_USER goal 的 WaitState）。
    store = _store(tmp_path)
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-1", original_query="规划两趟行程",
        intention_data={},
        semantic_tasks=[
            {
                "task_id": goal_id, "intent": "itinerary_planning",
                "query": query, "entities": {}, "depends_on": [],
                "side_effect": False, "failure_policy": "continue", "display_order": index,
            }
            for index, (goal_id, query) in enumerate((
                ("trip_nanjing", "规划南京行程"),
                ("trip_shanghai", "规划上海行程"),
            ))
        ],
        node_ids=["trip_nanjing-event_collection", "trip_shanghai-event_collection"],
        node_goals={
            "trip_nanjing-event_collection": "trip_nanjing",
            "trip_shanghai-event_collection": "trip_shanghai",
        },
        graph_hash="hash", skill_versions={},
    ))
    lifecycle = ExecutionLifecycle(store, state)

    def _pause(goal_id, node_id):
        return PauseInfo(
            intent="itinerary_planning", goal_id=goal_id, node_id=node_id,
            skill="plan-trip", pause_agent="event_collection",
            collected_facts={"missing_fields": ["origin"]},
        )

    # 第一轮：G1 暂停。
    asyncio.run(lifecycle.mark_waiting([_pause("trip_nanjing", "trip_nanjing-event_collection")]))
    # 第二轮：G2 追加后暂停 —— 不得抹掉 G1 的 WaitState。
    asyncio.run(lifecycle.mark_waiting([_pause("trip_shanghai", "trip_shanghai-event_collection")]))

    final = asyncio.run(store.get(state.run_id))
    assert {wait.goal_id for wait in final.waits} == {"trip_nanjing", "trip_shanghai"}
    assert final.focused_goal_id == "trip_shanghai"
    assert final.status == "WAITING_USER"
    assert final.goals["trip_nanjing"].status == "WAITING_USER"
    assert final.goals["trip_shanghai"].status == "WAITING_USER"
    assert final.nodes["trip_nanjing-event_collection"].status == "WAITING_USER"
    assert final.nodes["trip_shanghai-event_collection"].status == "WAITING_USER"


def test_independent_goal_does_not_consume_interrupted_goal(tmp_path):
    async def runner(**kwargs):
        assert kwargs["agent_name"] == "memory_query"
        return {"status": "success", "data": {"answer": "上次去了深圳"}}

    store = _store(tmp_path)
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-1", original_query=PLAN_QUERY,
        intention_data=PLAN_INTENTION,
        semantic_tasks=[{
            "task_id": "itinerary_planning", "intent": "itinerary_planning",
            "query": PLAN_QUERY, "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 0,
        }], node_ids=["itinerary_planning-event_collection"],
        graph_hash="unused", skill_versions={"plan-trip": "1.0.0"},
    ))
    def interrupted(old):
        old.status = "INTERRUPTED"
        old.goals["itinerary_planning"].status = "INTERRUPTED"
        old.nodes["itinerary_planning-event_collection"].status = "INTERRUPTED"
    state = asyncio.run(store.mutate(state.run_id, interrupted))

    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    intention = {
        "intents": [{"type": "memory_query", "confidence": .9, "should_call_skill": True}],
        "key_entities": {}, "rewritten_query": "我上次去了哪里",
    }
    asyncio.run(pipeline.run(
        "我上次去了哪里", intention, {}, session_id="session-a",
        request_id="req-2", existing_state=state,
    ))

    final = asyncio.run(store.get(state.run_id))
    assert final.status == "INTERRUPTED"
    assert final.goals["itinerary_planning"].status == "INTERRUPTED"
    assert final.goals["memory_query"].status == "SUCCEEDED"


def test_orphaned_active_run_becomes_resumable_after_process_loss(tmp_path):
    store = _store(tmp_path)
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="dead-request", original_query=PLAN_QUERY,
        intention_data=PLAN_INTENTION,
        semantic_tasks=[{
            "task_id": "itinerary_planning", "intent": "itinerary_planning",
            "query": PLAN_QUERY, "entities": {}, "depends_on": [],
            "side_effect": False, "failure_policy": "continue", "display_order": 0,
        }], node_ids=["itinerary_planning-event_collection"],
        node_goals={"itinerary_planning-event_collection": "itinerary_planning"},
        graph_hash="hash", skill_versions={},
    ))

    def started(old):
        old.nodes["itinerary_planning-event_collection"].status = "RUNNING"

    asyncio.run(store.mutate(state.run_id, started))
    recovered = asyncio.run(store.recover_orphaned_active_run(
        state.run_id, incoming_request_id="new-request",
    ))

    assert recovered.status == "INTERRUPTED"
    assert recovered.goals["itinerary_planning"].status == "INTERRUPTED"
    assert recovered.nodes["itinerary_planning-event_collection"].status == "INTERRUPTED"
    assert TurnResolver.resolve("继续", recovered).kind == "resume"


def test_same_request_crash_replay_reuses_goal_and_operation_id(tmp_path):
    operations = []

    async def runner(**kwargs):
        operations.append(kwargs["task_params"]["operation_id"])
        return {"status": "success", "data": {"answer": "上次去了深圳"}}

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    intention = {
        "intents": [{
            "type": "memory_query", "confidence": .9,
            "should_call_skill": True,
        }],
        "key_entities": {}, "rewritten_query": "我上次去了哪里",
    }
    semantic = [{
        "task_id": "memory_query", "group_id": "req-crashed",
        "intent": "memory_query", "query": "我上次去了哪里",
        "entities": {}, "depends_on": [], "side_effect": False,
        "failure_policy": "continue", "display_order": 0,
    }]
    execution = pipeline.graph_builder.compile([
        pipeline.validator.validate(semantic, intention)[0]
    ])
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-crashed",
        original_query="我上次去了哪里", intention_data=intention,
        semantic_tasks=semantic, node_ids=[execution[0].task_id],
        node_goals={execution[0].task_id: execution[0].goal_id},
        graph_hash=pipeline._graph_hash(execution), skill_versions={},
    ))

    def crashed_while_running(old):
        old.nodes[execution[0].task_id].status = "RUNNING"

    state = asyncio.run(store.mutate(state.run_id, crashed_while_running))
    output = asyncio.run(pipeline.run(
        "我上次去了哪里", intention, {}, session_id="session-a",
        request_id="req-crashed", existing_state=state,
    ))

    final = asyncio.run(store.get(state.run_id))
    assert output.answer_document is not None
    assert list(final.goals) == ["memory_query"]
    assert operations == [f"{state.run_id}:{execution[0].task_id}"]
