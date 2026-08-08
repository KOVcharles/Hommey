import asyncio
import json

from agents.intention_agent import IntentionAgent
from agentscope.message import Msg


async def _unused_model(_messages):
    raise AssertionError("fast routing should handle this query")


def _reply(query: str) -> dict:
    agent = IntentionAgent(name="IntentionAgent", model=_unused_model)
    result = asyncio.run(agent.reply(Msg(name="user", content=query, role="user")))
    return json.loads(result.content)


def _intent_types(data: dict) -> set[str]:
    return {item["type"] for item in data.get("intents", [])}


def _schedule(data: dict) -> list[tuple[str, int]]:
    return [
        (item["agent_name"], item["priority"])
        for item in data.get("agent_schedule", [])
    ]


def test_trip_and_policy_multi_intent_routes_to_both_schedule_paths():
    data = _reply("帮我规划一下去南京的路线，顺便告诉我餐补是多少")

    assert {"itinerary_planning", "rag_knowledge"} <= _intent_types(data)
    assert ("event_collection", 1) in _schedule(data)
    assert ("rag_knowledge", 2) in _schedule(data)
    assert ("information_query", 2) in _schedule(data)
    assert ("itinerary_planning", 3) in _schedule(data)
    assert data["routing"]["mode"] == "multi"
    assert data["routing"]["should_call_skill"] is True


def test_trip_and_weather_multi_intent_routes_to_information_and_trip():
    data = _reply("帮我安排明天去上海的行程，顺便查下天气")

    assert {"itinerary_planning", "information_query"} <= _intent_types(data)
    assert ("event_collection", 1) in _schedule(data)
    assert ("information_query", 2) in _schedule(data)
    assert ("itinerary_planning", 3) in _schedule(data)


def test_preference_and_trip_multi_intent_routes_to_preference_and_trip():
    data = _reply("我喜欢住汉庭，帮我规划下周去南京出差")

    assert {"preference", "itinerary_planning"} <= _intent_types(data)
    assert ("preference", 1) in _schedule(data)
    assert ("event_collection", 1) in _schedule(data)
    assert ("itinerary_planning", 3) in _schedule(data)


def test_policy_query_with_business_trip_context_does_not_trigger_trip_schedule():
    data = _reply("南京出差餐补是多少")

    assert _intent_types(data) == {"rag_knowledge"}
    assert _schedule(data) == [("rag_knowledge", 1)]


def test_trip_only_routes_to_event_collection_then_itinerary_planning():
    data = _reply("帮我规划去南京的路线")

    assert _intent_types(data) == {"itinerary_planning"}
    assert _schedule(data) == [
        ("event_collection", 1),
        ("rag_knowledge", 2),
        ("information_query", 2),
        ("itinerary_planning", 3),
        ("trip_compliance", 4),
    ]


def test_low_confidence_intent_is_filtered_per_intent():
    agent = IntentionAgent(name="IntentionAgent", model=_unused_model)
    data = agent._apply_routing_guard(
        {
            "reasoning": "mixed confidence",
            "routing": {
                "intent": "itinerary_planning",
                "confidence": 0.9,
                "reason": "trip is clear",
                "should_call_skill": True,
            },
            "intents": [
                {
                    "type": "itinerary_planning",
                    "confidence": 0.9,
                    "description": "",
                    "reason": "trip is clear",
                    "should_call_skill": True,
                },
                {
                    "type": "rag_knowledge",
                    "confidence": 0.3,
                    "description": "",
                    "reason": "policy is weak",
                    "should_call_skill": True,
                },
            ],
            "key_entities": {},
            "rewritten_query": "帮我规划去南京的路线，餐补可能也相关",
            "agent_schedule": [
                {"agent_name": "rag_knowledge", "priority": 1, "reason": "", "expected_output": ""}
            ],
        },
        "帮我规划去南京的路线，餐补可能也相关",
    )

    assert data["intents"][0]["should_call_skill"] is True
    assert data["intents"][1]["should_call_skill"] is False
    # The explicit low-confidence RAG intent is filtered, while the validated
    # plan-trip workflow still declares policy retrieval as a required step.
    assert ("rag_knowledge", 2) in _schedule(data)
    assert _schedule(data) == [
        ("event_collection", 1),
        ("rag_knowledge", 2),
        ("information_query", 2),
        ("itinerary_planning", 3),
        ("trip_compliance", 4),
    ]


def test_primary_intent_is_catalog_ranked_with_workflow_preference():
    agent = IntentionAgent(name="IntentionAgent", model=_unused_model)

    def make(intent_type, confidence=0.9):
        return {"type": intent_type, "confidence": confidence}

    # 多步 workflow（itinerary_planning）是组合请求的主语。
    primary = agent._select_primary_intent([
        make("rag_knowledge"), make("itinerary_planning"), make("information_query"),
    ])
    assert primary["type"] == "itinerary_planning"

    # 单步意图之间按 catalog_order 排序（rag < memory < preference）。
    assert agent._select_primary_intent([
        make("preference"), make("rag_knowledge"),
    ])["type"] == "rag_knowledge"
    assert agent._select_primary_intent([
        make("preference"), make("memory_query"),
    ])["type"] == "memory_query"

    # 未知意图按最大 catalog_order 垫底，且置信度做同优先级 tiebreaker。
    assert agent._select_primary_intent([
        make("chitchat"), make("no_such_intent"),
    ])["type"] == "chitchat"
