"""Real Chinese query matrix for pause/resume/new-goal routing."""
import asyncio

import pytest

from agents.intention_agent import IntentionAgent
from agentscope.message import Msg
from core.orchestration.models import IntentTask
from core.orchestration.fallback_composer import FallbackComposer
from core.orchestration.pipeline import MultiIntentPipeline
from core.orchestration.state import WorkflowRunState
from core.orchestration.state_store import OrchestrationStateStore
from core.orchestration.turn_resolver import TurnResolver


def _state(status: str, intent: str = "itinerary_planning") -> WorkflowRunState:
    node_status = "WAITING_USER" if status == "WAITING_USER" else "INTERRUPTED"
    goal_status = node_status
    return WorkflowRunState(
        run_id="run_query_matrix", user_id="u1", session_id="session-a",
        status=status, original_query="规划上海出差行程",
        goals={intent: {
            "goal_id": intent, "intent": intent, "status": goal_status,
            "query": "规划上海出差行程",
        }},
        nodes={f"{intent}-node": {
            "node_id": f"{intent}-node", "goal_id": intent,
            "status": node_status, "operation_id": f"run_query_matrix:{intent}-node",
        }},
        waits=([{
            "goal_id": intent, "expected_fields": ["origin"],
            "pause_agent": "event_collection",
        }] if status == "WAITING_USER" else []),
    )


@pytest.mark.parametrize(("query", "expected"), [
    ("北京", "resume"),
    ("我从北京出发", "resume"),
    ("后天出发，客户拜访", "resume"),
    ("出差1天，出差目的：客户拜访", "resume"),
    ("继续", "resume"),
    ("继续上次任务", "resume"),
    ("查一下北京天气", "new_goal"),
    ("我上次去了哪里", "new_goal"),
    ("顺便查一下公司的住宿标准", "new_goal"),
    ("另外帮我规划一趟广州出差", "new_goal"),
    ("谢谢", "new_goal"),
])
def test_waiting_query_matrix(query, expected):
    assert TurnResolver.resolve(query, _state("WAITING_USER")).kind == expected


@pytest.mark.parametrize(("intent", "query", "expected"), [
    ("itinerary_planning", "继续", "resume"),
    ("itinerary_planning", "改成后天去广州", "resume"),
    ("itinerary_planning", "重新规划一个上海行程", "resume"),
    ("itinerary_planning", "查一下北京天气", "new_goal"),
    ("itinerary_planning", "我上次去了哪里", "new_goal"),
    ("itinerary_planning", "另外帮我规划一趟广州出差", "new_goal"),
    ("information_query", "重新查一下上海天气", "resume"),
    ("information_query", "查公司的报销政策", "new_goal"),
])
def test_interrupted_query_matrix(intent, query, expected):
    assert TurnResolver.resolve(query, _state("INTERRUPTED", intent)).kind == expected


def test_explicit_continue_preserves_waiting_goal_query(tmp_path):
    seen_queries = []

    async def runner(**kwargs):
        seen_queries.append(kwargs["context"]["active_task"]["query"])
        return {"status": "success", "data": {
            "destination": "上海", "missing_fields": ["origin"],
            "planning_ready": False,
        }}

    store = OrchestrationStateStore("u1", postgres_dsn="", storage_dir=str(tmp_path))
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    intention = {
        "intents": [{
            "type": "itinerary_planning", "confidence": .9,
            "should_call_skill": True,
        }],
        "key_entities": {"destination": "上海"},
        "rewritten_query": "规划上海出差行程",
    }
    asyncio.run(pipeline.run(
        "规划上海出差行程", intention, {},
        session_id="session-a", request_id="req-1",
    ))
    state = asyncio.run(store.get_active("session-a"))
    output = asyncio.run(pipeline.resume_run(
        state, "继续", {}, request_id="req-2",
    ))

    assert output.paused is True
    assert len(seen_queries) == 2
    assert "继续" not in seen_queries[-1]
    assert "上海" in seen_queries[-1]


def test_interrupted_same_intent_query_revises_node_input(tmp_path):
    seen_queries = []

    async def runner(**kwargs):
        seen_queries.append(kwargs["context"]["active_task"]["query"])
        return {"status": "success", "data": {
            "query_success": True, "results": {"summary": "上海晴"},
        }}

    store = OrchestrationStateStore("u1", postgres_dsn="", storage_dir=str(tmp_path))
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    semantic = IntentTask(
        task_id="information_query", intent="information_query",
        query="查北京天气", entities={}, display_order=0,
    )
    execution = pipeline.graph_builder.compile([semantic])
    state = asyncio.run(store.create_run(
        session_id="session-a", request_id="req-1", original_query="查北京天气",
        intention_data={
            "intents": [{
                "type": "information_query", "confidence": .9,
                "should_call_skill": True,
            }],
            "key_entities": {}, "rewritten_query": "查北京天气",
        },
        semantic_tasks=[semantic.model_dump(mode="json")],
        node_ids=[item.task_id for item in execution],
        graph_hash=pipeline._graph_hash(execution),
        skill_versions=pipeline._skill_versions([semantic]),
    ))

    def interrupt(old):
        old.status = "INTERRUPTED"
        old.goals["information_query"].status = "INTERRUPTED"
        old.nodes["information_query-information_query"].status = "INTERRUPTED"

    state = asyncio.run(store.mutate(state.run_id, interrupt))
    output = asyncio.run(pipeline.resume_run(
        state, "重新查一下上海天气", {}, request_id="req-2",
    ))

    assert output.answer_document is not None
    assert seen_queries == ["重新查一下上海天气"]
    final = asyncio.run(store.get(state.run_id))
    assert final.status == "COMPLETED"


def test_exact_nanjing_weather_policy_plan_survives_intake_resume(tmp_path):
    original = (
        "我下周一要去南京出差 帮我看看南京最近的天气 "
        "然后帮我看看相关的差旅标准 给我一个详细的计划"
    )

    async def incomplete_model(_messages):
        # Simulate a common LLM omission: it keeps only the workflow main intent.
        import json
        return {"content": json.dumps({
            "routing": {
                "intent": "itinerary_planning", "confidence": .9,
                "reason": "需要规划", "should_call_skill": True,
            },
            "intents": [{
                "type": "itinerary_planning", "confidence": .9,
                "description": "", "reason": "需要规划", "should_call_skill": True,
            }],
            "key_entities": {"destination": "南京"},
            "rewritten_query": original,
        }, ensure_ascii=False)}

    intent_agent = IntentionAgent(name="IntentionAgent", model=incomplete_model)
    reply = asyncio.run(intent_agent.reply(Msg(name="user", content=original, role="user")))
    import json
    intention = json.loads(reply.content)
    # plan-trip 本身声明天气、制度和车次子步骤，不需要把它们升级成并列意图。
    assert {"itinerary_planning"} == {
        item["type"] for item in intention["intents"] if item["should_call_skill"]
    }

    calls = []

    async def runner(**kwargs):
        agent = kwargs["agent_name"]
        query = kwargs["context"]["active_task"]["query"]
        calls.append(agent)
        if agent == "event_collection":
            ready = "1天" in query
            return {"status": "success", "data": {
                "origin": "北京", "destination": "南京",
                "start_date": "2026-08-10", "duration_days": 1 if ready else None,
                "trip_purpose": "出差", "missing_fields": [] if ready else ["duration_days"],
                "planning_ready": ready,
            }}
        if agent == "rag_knowledge":
            return {"status": "success", "data": {
                "answer": "南京住宿和交通标准应按公司差旅制度执行。",
            }}
        if agent == "information_query":
            return {"status": "success", "data": {
                "query_success": True,
                "results": {"summary": "2026-08-10：多云，25～33°C，最高降水概率30%"},
            }}
        if agent == "train_query":
            return {"status": "success", "data": {
                "query_success": True,
                "results": {"summary": "G1次可作为候选，出发前通过12306核验", "trains": []},
            }}
        if agent == "itinerary_planning":
            return {"status": "success", "data": {"itinerary": {
                "title": "南京一日出差详细计划",
                "duration": "1天",
                "transport_recommendation": {
                    "preferred": "高铁往返",
                    "reason": "优先保证工作时间并预留进站缓冲",
                },
                "lodging_advice": "当天返回，无需住宿；若临时过夜需按制度核验标准。",
                "daily_plans": [{
                    "day": 1,
                    "activities": [
                        {"time": "上午", "activity": "北京前往南京", "description": "预留进站时间"},
                        {"time": "下午", "activity": "完成工作安排", "description": "按会议时间倒排交通"},
                        {"time": "晚间", "activity": "返回北京", "description": "保留延误备选"},
                    ],
                }],
                "notes": [
                    "南京住宿和交通标准应按公司差旅制度执行。",
                    "2026-08-10：多云，25～33°C，最高降水概率30%",
                ],
            }}}
        if agent == "trip_compliance":
            return {"status": "success", "data": {
                "summary": "交通和报销凭证需按检索到的制度核验。",
            }}
        if agent == "memory_query":
            return {"status": "success", "data": {"answer": "上次去了深圳"}}
        raise AssertionError(agent)

    store = OrchestrationStateStore("u1", postgres_dsn="", storage_dir=str(tmp_path))
    pipeline = MultiIntentPipeline(
        model=None, composer_model=None, agent_runner=runner, state_store=store,
    )
    composed_queries = []

    class CapturingComposer:
        async def compose(self, query, tasks, results):
            composed_queries.append(query)
            return FallbackComposer().compose(tasks, results)

    pipeline.composer = CapturingComposer()
    first = asyncio.run(pipeline.run(
        original, intention, {}, session_id="session-a", request_id="req-1",
    ))
    assert first.paused is True
    state = asyncio.run(store.get_active("session-a"))
    assert set(state.goals) == {"itinerary_planning"}
    assert state.goals["itinerary_planning"].status == "WAITING_USER"
    assert calls == ["event_collection"]

    # An independent side question can be answered while intake is waiting.
    # Its delivered Goal must not leak into the later resumed plan answer.
    side_intention = {
        "intents": [{
            "type": "memory_query", "confidence": .9,
            "should_call_skill": True,
        }],
        "key_entities": {}, "rewritten_query": "我上次去了哪里",
    }
    side = asyncio.run(pipeline.run(
        "我上次去了哪里", side_intention, {}, session_id="session-a",
        request_id="req-side", existing_state=state,
    ))
    assert [section.kind for section in side.answer_document.sections] == ["memory"]
    state = asyncio.run(store.get(state.run_id))
    memory_goal_id = next(
        goal.goal_id for goal in state.goals.values()
        if goal.intent == "memory_query"
    )
    assert state.goals[memory_goal_id].answer_delivered is True

    final = asyncio.run(pipeline.resume_run(
        state, "1天", {}, request_id="req-2",
    ))
    kinds = [section.kind for section in final.answer_document.sections]
    text = final.answer_document.plain_text

    assert kinds and set(kinds) == {"trip"}
    assert "南京住宿和交通标准" in text
    assert "2026-08-10" in text and "多云" in text
    assert "南京一日出差详细计划" in text
    assert "上午" in text and "下午" in text and "晚间" in text
    assert "memory" not in kinds and "上次去了深圳" not in text
    # Weather and policy execute once after intake resumes, then fuse into the trip card.
    assert calls.count("rag_knowledge") == 1
    assert calls.count("information_query") == 1
    assert composed_queries[-1].startswith(original)
    delivered = asyncio.run(store.get(state.run_id))
    assert all(goal.answer_delivered for goal in delivered.goals.values())
