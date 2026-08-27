import asyncio
import json

from agents.intention_agent import IntentionAgent
from agentscope.message import Msg
from core.intent_result import coerce_intent_analysis
from core.orchestration.decomposer import TaskDecomposer
from core.orchestration.policy import OrchestrationPolicy
from core.orchestration.validator import TaskValidator


def _group(group_id, intent, query, confidence=0.95, entities=None):
    return {
        "group_id": group_id,
        "intent": intent,
        "query": query,
        "confidence": confidence,
        "entities": entities or {},
        "source_refs": ["current_query"],
    }


def test_intention_agent_returns_semantic_groups_without_authorization_fields():
    async def model(_messages):
        return json.dumps({
            "schema_version": 1,
            "groups": [
                _group("weather", "information_query", "查询上海明天天气"),
            ],
            "relations": [],
        }, ensure_ascii=False)

    agent = IntentionAgent(name="IntentionAgent", model=model)
    response = asyncio.run(agent.reply(
        Msg(name="user", content="帮我查上海明天天气", role="user")
    ))
    payload = json.loads(response.content)

    assert payload["groups"][0]["intent"] == "information_query"
    assert "routing" not in payload
    assert "intents" not in payload
    assert "should_call_skill" not in payload["groups"][0]


def test_policy_recomputes_legacy_authorization_instead_of_trusting_it():
    legacy = {
        "routing": {"intent": "rag_knowledge", "confidence": 0.3, "should_call_skill": True},
        "intents": [{
            "type": "rag_knowledge", "confidence": 0.3, "should_call_skill": True,
        }],
        "rewritten_query": "查询差旅餐补标准",
        "key_entities": {},
    }

    analysis = coerce_intent_analysis(legacy, "查询差旅餐补标准")
    evaluation = OrchestrationPolicy().evaluate(
        analysis, original_query="查询差旅餐补标准",
    )

    assert evaluation.decisions[0].authorized is False
    assert evaluation.to_compatibility_dict("查询差旅餐补标准")["routing"]["should_call_skill"] is False


def test_same_intent_groups_remain_distinct_goals_with_isolated_entities():
    analysis = {
        "schema_version": 1,
        "groups": [
            _group("beijing_weather", "information_query", "查询北京明天天气", entities={"destination": "北京"}),
            _group("shanghai_weather", "information_query", "查询上海明天天气", entities={"destination": "上海"}),
        ],
        "relations": [],
    }
    evaluation = OrchestrationPolicy().evaluate(
        analysis, original_query="分别查询北京和上海明天天气",
    )
    payload = evaluation.to_compatibility_dict("分别查询北京和上海明天天气")
    raw_tasks = TaskDecomposer.from_analysis(payload["original_query"], payload)
    tasks = TaskValidator().validate(raw_tasks, payload)

    assert [task.task_id for task in tasks] == ["beijing_weather", "shanghai_weather"]
    assert [task.entities["destination"] for task in tasks] == ["北京", "上海"]
    assert "destination" not in payload["key_entities"]


def test_required_context_relation_becomes_goal_dependency():
    analysis = {
        "schema_version": 1,
        "groups": [
            _group("weather", "information_query", "查询上海明天天气"),
            _group("plan", "itinerary_planning", "根据天气规划上海公司出差行程"),
        ],
        "relations": [{
            "from": ["weather"], "to": "plan", "type": "required_context",
        }],
    }
    evaluation = OrchestrationPolicy().evaluate(
        analysis, original_query="查上海明天天气，再根据天气规划公司出差行程",
    )
    payload = evaluation.to_compatibility_dict(
        "查上海明天天气，再根据天气规划公司出差行程"
    )
    raw_tasks = TaskDecomposer.from_analysis(payload["original_query"], payload)

    assert next(task for task in raw_tasks if task["task_id"] == "plan")["depends_on"] == ["weather"]


def test_required_context_target_is_denied_when_prerequisite_is_not_authorized():
    analysis = {
        "schema_version": 1,
        "groups": [
            _group(
                "weather",
                "information_query",
                "上海天气",
                confidence=0.2,
            ),
            _group(
                "plan",
                "itinerary_planning",
                "根据天气规划上海公司出差行程",
            ),
        ],
        "relations": [{
            "from": ["weather"], "to": "plan", "type": "required_context",
        }],
    }

    evaluation = OrchestrationPolicy().evaluate(
        analysis, original_query="查上海天气，再根据天气规划公司出差行程",
    )
    decisions = {item.group_id: item for item in evaluation.decisions}
    payload = evaluation.to_compatibility_dict(
        "查上海天气，再根据天气规划公司出差行程"
    )

    assert decisions["weather"].authorized is False
    assert decisions["plan"].authorized is False
    assert decisions["plan"].reason_code == "REQUIRED_CONTEXT_NOT_AUTHORIZED"
    assert TaskDecomposer.from_analysis(payload["original_query"], payload) == []
