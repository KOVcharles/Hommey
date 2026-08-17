"""Architecture invariants independent of any one sample query."""
import asyncio

from core.intent_router import FastIntentRouter
from core.orchestration.fallback_composer import FallbackComposer
from core.orchestration.models import IntentTask, TaskResult
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.state_store import OrchestrationStateStore


def _store(tmp_path):
    return OrchestrationStateStore("u1", postgres_dsn="", storage_dir=str(tmp_path))


def test_fast_path_requires_provably_complete_single_intent():
    single = FastIntentRouter.route("南京出差餐补标准是多少")
    mixed = FastIntentRouter.route("南京出差餐补标准是多少，然后再帮我做个安排")

    assert single is not None and single.safe_to_short_circuit is True
    assert mixed is not None and mixed.safe_to_short_circuit is False


def test_workflow_uses_explicit_edges_without_owning_independent_goals():
    pipeline = MultiIntentPipeline(model=None, composer_model=None, agent_runner=None)
    tasks = [
        IntentTask(task_id="plan", group_id="req", intent="itinerary_planning", query="规划南京出差", display_order=0),
        IntentTask(task_id="policy", group_id="req", intent="rag_knowledge", query="查询南京差旅标准", display_order=1),
        IntentTask(task_id="weather", group_id="req", intent="information_query", query="查询南京天气", display_order=2),
    ]
    nodes = pipeline.graph_builder.compile(tasks)
    by_agent = {node.agent_name: node for node in nodes}

    assert by_agent["rag_knowledge"].goal_id == "policy"
    assert by_agent["information_query"].goal_id == "weather"
    assert by_agent["rag_knowledge"].depends_on == []
    assert by_agent["information_query"].depends_on == []
    assert set(by_agent["itinerary_planning"].depends_on) >= {
        by_agent["event_collection"].task_id,
        by_agent["rag_knowledge"].task_id,
        by_agent["information_query"].task_id,
        by_agent["train_query"].task_id,
    }
    assert by_agent["train_query"].depends_on == [by_agent["event_collection"].task_id]


def test_same_intent_answer_sections_remain_goal_scoped():
    tasks = [
        IntentTask(task_id="history_nanjing", intent="memory_query", query="南京历史", display_order=0),
        IntentTask(task_id="history_shanghai", intent="memory_query", query="上海历史", display_order=1),
    ]
    results = [
        TaskResult(
            task_id="history_nanjing-memory_query", goal_id="history_nanjing",
            intent="memory_query", agent_name="memory_query", status="success",
            data={"answer": "南京记录"},
        ),
        TaskResult(
            task_id="history_shanghai-memory_query", goal_id="history_shanghai",
            intent="memory_query", agent_name="memory_query", status="success",
            data={"answer": "上海记录"},
        ),
    ]
    document = FallbackComposer().compose(tasks, results)
    assert [section.goal_id for section in document.sections] == [
        "history_nanjing", "history_shanghai",
    ]
    assert "南京记录" in document.sections[0].body
    assert "上海记录" in document.sections[1].body


def test_same_intent_goals_have_distinct_identity_in_one_run(tmp_path):
    calls = []

    async def runner(**kwargs):
        calls.append(kwargs["context"]["active_task"]["query"])
        return {"status": "success", "data": {"answer": "找到一条历史记录"}}

    intention = {
        "intents": [{"type": "memory_query", "confidence": .9, "should_call_skill": True}],
        "key_entities": {}, "rewritten_query": "查询上次南京出差",
    }
    # Build an interrupted active container, then append another Goal with the
    # same intent but a different query.
    semantic = [{
        "task_id": "memory_query", "intent": "memory_query",
        "query": "查询上次南京出差", "entities": {}, "depends_on": [],
        "side_effect": False, "failure_policy": "continue", "display_order": 0,
    }]
    active_store = OrchestrationStateStore("u2", postgres_dsn="", storage_dir=str(tmp_path))
    active_pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=active_store,
    )
    execution = active_pipeline.graph_builder.compile([
        IntentTask.model_validate(semantic[0])
    ])
    active = asyncio.run(active_store.create_run(
        session_id="session-a", request_id="old", original_query="查询上次南京出差",
        intention_data=intention, semantic_tasks=semantic,
        node_ids=[node.task_id for node in execution],
        node_goals={node.task_id: node.goal_id for node in execution},
        graph_hash=active_pipeline._graph_hash(execution), skill_versions={},
    ))

    def interrupt(old):
        old.status = "INTERRUPTED"
        old.goals["memory_query"].status = "INTERRUPTED"
        old.nodes[execution[0].task_id].status = "INTERRUPTED"

    active = asyncio.run(active_store.mutate(active.run_id, interrupt))
    asyncio.run(active_pipeline.run(
        "查询上次上海出差", intention, {}, session_id="session-a",
        request_id="new", existing_state=active,
    ))
    final = asyncio.run(active_store.get(active.run_id))
    assert len(final.goals) == 2
    assert len({goal.goal_id for goal in final.goals.values()}) == 2
    assert {goal.intent for goal in final.goals.values()} == {"memory_query"}


def test_optional_goal_failure_completes_run_with_error_section(tmp_path):
    async def runner(**_kwargs):
        return {"status": "error", "data": {}, "error_message": "天气服务不可用"}

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    intention = {
        "intents": [{"type": "information_query", "confidence": .9, "should_call_skill": True}],
        "key_entities": {"destination": "南京"}, "rewritten_query": "查询南京出差天气",
    }
    output = asyncio.run(pipeline.run(
        "查询南京出差天气", intention, {},
        session_id="session-a", request_id="req-optional",
    ))
    files = store._file_all()
    assert output.answer_document.sections[0].status == "error"
    assert len(files) == 1 and files[0].status == "COMPLETED"
    assert next(iter(files[0].goals.values())).status == "FAILED"
    assert next(iter(files[0].goals.values())).answer_delivered is True


def test_abort_failure_persists_downstream_skips_and_failed_run(tmp_path):
    async def runner(**kwargs):
        if kwargs["agent_name"] == "event_collection":
            return {"status": "success", "data": {"planning_ready": True}}
        if kwargs["agent_name"] == "rag_knowledge":
            return {"status": "error", "data": {}, "error_message": "制度查询失败"}
        if kwargs["agent_name"] == "information_query":
            return {"status": "success", "data": {
                "query_success": True, "results": {"summary": "天气正常"},
            }}
        if kwargs["agent_name"] == "train_query":
            return {"status": "success", "data": {
                "query_success": True, "results": {"trains": []},
            }}
        raise AssertionError(f"downstream must not run: {kwargs['agent_name']}")

    store = _store(tmp_path)
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    intention = {
        "intents": [{"type": "itinerary_planning", "confidence": .9, "should_call_skill": True}],
        "key_entities": {"destination": "南京", "duration": "1天"},
        "rewritten_query": "规划南京一日出差行程",
    }
    asyncio.run(pipeline.run(
        "规划南京一日出差行程", intention, {},
        session_id="session-a", request_id="req-abort",
    ))
    state = store._file_all()[0]
    assert state.status == "FAILED"
    assert state.nodes["itinerary_planning-itinerary_planning"].status == "SKIPPED"
    assert state.nodes["itinerary_planning-trip_compliance"].status == "SKIPPED"
    assert state.goals["itinerary_planning"].status == "FAILED"
