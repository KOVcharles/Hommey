import asyncio
import json

from agents.lazy_agent_registry import LazyAgentRegistry
from agentscope.message import Msg

from core.orchestration.capabilities import apply_capability_selection
from core.orchestration.executor import TaskExecutor
from core.orchestration.graph_builder import TaskGraphBuilder
from core.orchestration.models import ExecutionTask, IntentTask
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.state_store import OrchestrationStateStore


def _plan(query: str) -> IntentTask:
    return IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query=query,
        entities={
            "origin": "北京",
            "destination": "南京",
            "start_date": "2026-09-01",
            "duration": "2天",
            "purpose": "客户拜访",
        },
    )


def test_weather_opt_out_keeps_transport_and_mandatory_steps():
    query = "给我详细的南京出差计划，不需要天气信息"
    task = apply_capability_selection([_plan(query)], query)[0]

    nodes = TaskGraphBuilder().compile([task])
    by_agent = {node.agent_name: node for node in nodes}

    assert task.capability_selection.exclude == ["weather"]
    assert by_agent["information_query"].capabilities == ["local_transport"]
    assert "市内交通" in by_agent["information_query"].query
    assert "天气" not in by_agent["information_query"].query
    assert {"rag_knowledge", "itinerary_planning", "trip_compliance"} <= set(by_agent)


def test_all_optional_facets_can_be_removed_without_disabling_policy_or_compliance():
    query = "详细规划南京出差，不要天气和市内交通，也不用查高铁、政策和合规"
    task = apply_capability_selection([_plan(query)], query)[0]

    nodes = TaskGraphBuilder().compile([task])
    agents = {node.agent_name for node in nodes}

    assert set(task.capability_selection.exclude) == {
        "weather", "local_transport", "train",
    }
    assert "information_query" not in agents
    assert "train_query" not in agents
    assert {"event_collection", "rag_knowledge", "itinerary_planning", "trip_compliance"} <= agents


def test_different_destination_standalone_weather_is_not_a_plan_dependency():
    plan = _plan("规划南京出差")
    plan.group_id = "same-turn"
    weather = IntentTask(
        task_id="information_query",
        group_id="same-turn",
        intent="information_query",
        query="查询上海天气",
        entities={"destination": "上海"},
    )

    nodes = TaskGraphBuilder().compile([plan, weather])
    by_id = {node.task_id: node for node in nodes}
    planning = by_id["itinerary_planning-itinerary_planning"]

    assert "information_query-information_query" not in planning.depends_on
    assert "itinerary_planning-information_query" in planning.depends_on


def test_weather_opt_out_is_persisted_while_waiting_for_trip_details(tmp_path):
    query = "规划南京出差，不需要天气"

    async def runner(**_kwargs):
        return {"status": "success", "data": {
            "destination": "南京",
            "planning_ready": False,
            "missing_fields": ["origin"],
        }}

    store = OrchestrationStateStore("u1", postgres_dsn="", storage_dir=str(tmp_path))
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    intention = {
        "intents": [{
            "type": "itinerary_planning",
            "confidence": 0.9,
            "should_call_skill": True,
        }],
        "key_entities": {"destination": "南京"},
        "rewritten_query": query,
    }

    output = asyncio.run(pipeline.run(
        query, intention, {}, session_id="session-a", request_id="req-1",
    ))
    state = asyncio.run(store.get_active("session-a"))
    persisted = IntentTask.model_validate(state.semantic_tasks[0])
    nodes = TaskGraphBuilder().compile([persisted])
    info = next(node for node in nodes if node.agent_name == "information_query")

    assert output.paused is True
    assert persisted.capability_selection.exclude == ["weather"]
    assert info.capabilities == ["local_transport"]


def test_executor_passes_only_declared_dependency_results():
    seen_previous = {}

    async def runner(**kwargs):
        seen_previous[kwargs["agent_name"]] = [
            item["agent_name"] for item in kwargs["previous_results"]
        ]
        return {"status": "success", "data": {"ok": True}}

    tasks = [
        ExecutionTask(
            task_id="weather-unrelated", goal_id="weather", intent="information_query",
            query="查询上海天气", agent_name="unrelated_weather", priority=1,
        ),
        ExecutionTask(
            task_id="plan-intake", goal_id="plan", intent="itinerary_planning",
            query="收集南京行程", agent_name="event_collection", priority=1,
        ),
        ExecutionTask(
            task_id="plan-compose", goal_id="plan", intent="itinerary_planning",
            query="规划南京行程", agent_name="itinerary_planning", priority=2,
            depends_on=["plan-intake"],
        ),
    ]

    asyncio.run(TaskExecutor(runner).execute(tasks, {}))

    assert seen_previous["itinerary_planning"] == ["event_collection"]


def test_query_info_skips_weather_call_but_keeps_local_transport():
    agent = LazyAgentRegistry(model=None, cache={})["information_query"]
    calls = []

    async def weather(*_args, **_kwargs):
        calls.append("weather")
        return {"query_success": True, "results": {"summary": "晴"}}

    async def transport(*_args, **_kwargs):
        calls.append("local_transport")
        return {"query_success": True, "results": {"summary": "建议地铁接驳"}}

    agent._weather_query = weather
    agent._web_search = transport
    payload = {
        "context": {
            "active_task": {
                "query": "查询北京到南京的市内交通",
                "entities": {"destination": "南京"},
                "capabilities": ["local_transport"],
            },
        },
        "previous_results": [{
            "agent_name": "event_collection",
            "result": {"data": {
                "planning_ready": True,
                "origin": "北京",
                "destination": "南京",
                "start_date": "2026-09-01",
                "duration_days": 2,
            }},
        }],
    }

    reply = asyncio.run(agent.reply(Msg(
        name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user",
    )))
    result = json.loads(reply.content)

    assert calls == ["local_transport"]
    assert "weather" not in result["results"]
    assert result["results"]["transport"]["summary"] == "建议地铁接驳"


def test_itinerary_planner_keeps_repeated_agent_facets_for_fusion():
    prompts = []

    async def model(messages):
        prompts.append(messages[-1]["content"])
        return {"content": json.dumps({
            "itinerary": {
                "title": "南京出差",
                "duration": "2天",
                "transport_recommendation": {},
                "lodging_advice": "",
                "daily_plans": [],
                "notes": [],
                "reimbursement_checklist": [],
                "missing_info": [],
            },
            "planning_complete": True,
        }, ensure_ascii=False)}

    agent = LazyAgentRegistry(model=model, cache={})["itinerary_planning"]
    payload = {
        "context": {"active_task": {"query": "规划南京出差"}},
        "previous_results": [
            {
                "task_id": "weather-info",
                "goal_id": "weather",
                "agent_name": "information_query",
                "result": {"data": {"results": {
                    "weather": {"summary": "南京有雨"},
                    "summary": "天气：南京有雨",
                }}},
            },
            {
                "task_id": "plan-local-transport",
                "goal_id": "plan",
                "agent_name": "information_query",
                "result": {"data": {"results": {
                    "transport": {"summary": "建议地铁接驳"},
                    "summary": "交通：建议地铁接驳",
                }}},
            },
        ],
    }

    asyncio.run(agent.reply(Msg(
        name="Orchestrator", content=json.dumps(payload, ensure_ascii=False), role="user",
    )))

    assert "南京有雨" in prompts[0]
    assert "建议地铁接驳" in prompts[0]
    assert "weather-info" in prompts[0]
    assert "plan-local-transport" in prompts[0]
