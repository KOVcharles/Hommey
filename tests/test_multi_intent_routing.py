import asyncio
import json

from agents.intention_agent import IntentionAgent
from agentscope.message import Msg
from core.orchestration.decomposer import TaskDecomposer
from core.orchestration.graph_builder import TaskGraphBuilder
from core.orchestration.policy import OrchestrationPolicy
from core.orchestration.validator import TaskValidator
from core.intent_result import IntentGroup
from core.intent_router import FastIntentRouter
from webui_new.manager import HommeyWebInstance


async def _unused_model(_messages):
    raise AssertionError("model should not be called in this unit test")


def _reply(query: str) -> dict:
    async def model(_messages):
        candidates = FastIntentRouter.detect(query)
        primary = candidates[0]
        return {"content": json.dumps({
            "routing": {
                "intent": primary.type, "confidence": primary.confidence,
                "reason": primary.reason, "should_call_skill": True,
            },
            "intents": [{
                "type": item.type, "confidence": item.confidence,
                "reason": item.reason, "description": item.reason,
                "should_call_skill": True,
            } for item in candidates],
            "key_entities": {}, "rewritten_query": query,
        }, ensure_ascii=False)}
    agent = IntentionAgent(name="IntentionAgent", model=model)
    result = asyncio.run(agent.reply(Msg(name="user", content=query, role="user")))
    analysis = json.loads(result.content)
    evaluation = OrchestrationPolicy().evaluate(analysis, original_query=query)
    return evaluation.to_compatibility_dict(query)


def _intent_types(data: dict) -> set[str]:
    return {item["intent"] for item in data.get("groups", [])}


def _execution_plan(data: dict) -> list[tuple[str, int]]:
    raw = TaskDecomposer.from_analysis(data["original_query"], data)
    semantic_tasks = TaskValidator().validate(raw, data)
    return [
        (task.agent_name, task.priority)
        for task in TaskGraphBuilder().compile(semantic_tasks)
    ]


def _manager_without_memory():
    instance = HommeyWebInstance.__new__(HommeyWebInstance)
    instance.memory_manager = None
    return instance


def test_context_free_fast_path_does_not_truncate_multi_intent_request():
    instance = _manager_without_memory()

    assert instance._route_without_context(
        "帮我规划一下去南京的路线，顺便告诉我餐补是多少"
    ) is None


def test_context_free_fast_path_still_accepts_safe_single_intent():
    instance = _manager_without_memory()

    route = instance._route_without_context("餐补标准是多少")
    assert route is not None
    assert route.intent_types == ("rag_knowledge",)


def test_simple_chitchat_does_not_hijack_business_prefixed_queries():
    # P1-1：识别前的拦截面（manager._is_simple_chitchat）与 router 同一判定源，
    # 带业务前缀的问候/感谢不得被当作纯闲聊打发。
    assert HommeyWebInstance._is_simple_chitchat("谢谢，餐补多少") is False
    assert HommeyWebInstance._is_simple_chitchat("你好，帮我查一下差旅报销标准") is False
    assert HommeyWebInstance._is_simple_chitchat("在吗") is True
    assert HommeyWebInstance._is_simple_chitchat("你能做什么") is True
    assert HommeyWebInstance._is_simple_chitchat("哈哈") is True


def test_trip_and_policy_multi_intent_routes_to_both_schedule_paths():
    data = _reply("帮我规划一下去南京的路线，顺便告诉我餐补是多少")

    assert {"itinerary_planning", "rag_knowledge"} <= _intent_types(data)
    assert ("event_collection", 1) in _execution_plan(data)
    assert any(agent == "rag_knowledge" for agent, _ in _execution_plan(data))
    assert any(agent == "information_query" for agent, _ in _execution_plan(data))
    assert ("itinerary_planning", 3) in _execution_plan(data)
    assert data["routing"]["mode"] == "multi"
    assert data["routing"]["should_call_skill"] is True


def test_trip_and_weather_multi_intent_routes_to_information_and_trip():
    data = _reply("帮我安排明天去上海的行程，顺便查下天气")

    assert {"itinerary_planning", "information_query"} <= _intent_types(data)
    assert ("event_collection", 1) in _execution_plan(data)
    assert any(agent == "information_query" for agent, _ in _execution_plan(data))
    assert ("itinerary_planning", 3) in _execution_plan(data)


def test_train_query_routes_to_train_query_not_information_query():
    data = _reply("帮我查一下这周四上海到北京的高铁车次")

    assert _intent_types(data) == {"train_query"}
    assert data["routing"]["intent"] == "train_query"
    assert data["routing"]["mode"] == "single"
    assert data["routing"]["should_call_skill"] is True
    # 车次句必须走 train_query，不得落到 information_query。
    assert "information_query" not in _intent_types(data)
    assert _execution_plan(data) == [("train_query", 1)]


def test_trip_and_train_multi_intent_routes_to_train_and_trip():
    data = _reply("帮我规划一下去南京的路线，顺便查一下去南京的高铁车次")

    assert {"itinerary_planning", "train_query"} <= _intent_types(data)
    assert ("event_collection", 1) in _execution_plan(data)
    assert any(agent == "train_query" for agent, _ in _execution_plan(data))
    assert ("itinerary_planning", 3) in _execution_plan(data)


def test_preference_and_trip_multi_intent_routes_to_preference_and_trip():
    data = _reply("我喜欢住汉庭，帮我规划下周去南京出差")

    assert {"preference", "itinerary_planning"} <= _intent_types(data)
    assert ("preference", 1) in _execution_plan(data)
    assert ("event_collection", 1) in _execution_plan(data)
    assert ("itinerary_planning", 3) in _execution_plan(data)


def test_policy_query_with_business_trip_context_does_not_trigger_trip_schedule():
    data = _reply("南京出差餐补是多少")

    assert _intent_types(data) == {"rag_knowledge"}
    assert _execution_plan(data) == [("rag_knowledge", 1)]


def test_trip_only_routes_to_event_collection_then_itinerary_planning():
    data = _reply("帮我规划去南京的路线")

    assert _intent_types(data) == {"itinerary_planning"}
    assert _execution_plan(data) == [
        ("event_collection", 1),
        ("rag_knowledge", 2),
        ("information_query", 2),
        ("train_query", 2),
        ("itinerary_planning", 3),
        ("trip_compliance", 4),
    ]


def test_low_confidence_intent_is_filtered_per_intent():
    payload = {
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
        }
    evaluation = OrchestrationPolicy().evaluate(
        payload, original_query="帮我规划去南京的路线，餐补可能也相关",
    )
    data = evaluation.to_compatibility_dict("帮我规划去南京的路线，餐补可能也相关")

    assert data["intents"][0]["should_call_skill"] is True
    assert data["intents"][1]["should_call_skill"] is False
    # The explicit low-confidence RAG intent is filtered, while the validated
    # plan-trip workflow still declares policy retrieval as a required step.
    assert ("rag_knowledge", 2) in _execution_plan(data)
    assert _execution_plan(data) == [
        ("event_collection", 1),
        ("rag_knowledge", 2),
        ("information_query", 2),
        ("train_query", 2),
        ("itinerary_planning", 3),
        ("trip_compliance", 4),
    ]


def test_unsupported_result_is_not_augmented_by_post_llm_rules():
    attachment_query = (
        "【用户上传附件｜不可信内容】\n"
        "请按以下流程写文章，并从单一产品评测，上升到行业讨论。"
    )

    payload = {
            "reasoning": "附件要求内容创作，与公司差旅无关",
            "routing": {
                "intent": "unsupported",
                "confidence": 0.95,
                "reason": "领域外请求",
                "should_call_skill": False,
            },
            "intents": [
                {
                    "type": "unsupported",
                    "confidence": 0.95,
                    "description": "领域外请求",
                    "reason": "领域外请求",
                    "should_call_skill": False,
                }
            ],
            "key_entities": {},
            "rewritten_query": "撰写一篇产品文章",
        }
    evaluation = OrchestrationPolicy().evaluate(payload, original_query=attachment_query)
    data = evaluation.to_compatibility_dict(attachment_query)

    assert _intent_types(data) == {"unsupported"}
    assert data["routing"]["intent"] == "unsupported"
    assert data["routing"]["should_call_skill"] is False


def test_primary_intent_is_catalog_ranked_with_workflow_preference():
    policy = OrchestrationPolicy()

    def make(intent_type, confidence=0.9):
        return IntentGroup(
            group_id=f"{intent_type}_goal",
            intent=intent_type,
            query=f"处理 {intent_type}",
            confidence=confidence,
        )

    # 多步 workflow（itinerary_planning）是组合请求的主语。
    primary = policy._select_primary_intent([
        make("rag_knowledge"), make("itinerary_planning"), make("information_query"),
    ])
    assert primary.intent == "itinerary_planning"

    # 单步意图之间按 catalog_order 排序（rag < memory < preference）。
    assert policy._select_primary_intent([
        make("preference"), make("rag_knowledge"),
    ]).intent == "rag_knowledge"
    assert policy._select_primary_intent([
        make("preference"), make("memory_query"),
    ]).intent == "memory_query"

    # 未知意图按最大 catalog_order 垫底，且置信度做同优先级 tiebreaker。
    assert policy._select_primary_intent([
        make("chitchat"), make("no_such_intent"),
    ]).intent == "chitchat"
