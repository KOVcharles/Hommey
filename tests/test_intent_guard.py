import asyncio
import json

from agents.intention_agent import IntentionAgent
from agents.orchestration_agent import OrchestrationAgent
from agentscope.message import Msg
from core.intent_guard import can_call_information_query, guard_user_input
from core.intent_router import FastIntentRouter


def _schedule_agents(route):
    data = route.to_intention_data("")
    return [item.get("agent_name") for item in data.get("agent_schedule", [])]


def test_short_input_does_not_call_information_query():
    result = guard_user_input("你?")

    assert result is not None
    assert result.intent == "unclear"
    assert result.should_call_skill is False
    assert result.agent_schedule == []


def test_chitchat_routes_to_skill():
    route = FastIntentRouter.route("在吗")

    assert route is not None
    data = route.to_intention_data("在吗")
    assert data["routing"]["intent"] == "chitchat"
    assert data["routing"]["should_call_skill"] is True
    assert _schedule_agents(route) == ["chitchat"]


def test_weather_query_without_trip_context_does_not_fast_route():
    route = FastIntentRouter.route("帮我查一下明天东京天气")

    assert route is None

    result = can_call_information_query("帮我查一下明天东京天气", 0.9)
    assert result.intent == "unclear"
    assert result.should_call_skill is False
    assert "出差" in result.clarification


def test_weather_query_with_trip_context_routes_to_information_query():
    route = FastIntentRouter.route("我明天去东京出差，帮我查一下天气")

    assert route is not None
    data = route.to_intention_data("我明天去东京出差，帮我查一下天气")
    assert data["routing"]["intent"] == "information_query"
    assert data["routing"]["should_call_skill"] is True
    assert _schedule_agents(route) == ["information_query"]


def test_weather_followup_can_use_existing_business_trip_context():
    result = can_call_information_query(
        "那南京明天天气怎么样",
        0.9,
        "用户: 我下周要去南京出差",
    )

    assert result.intent == "information_query"
    assert result.should_call_skill is True


def test_intention_followup_uses_dialogue_context_instead_of_context_free_route():
    model_calls = []

    async def contextual_model(messages):
        model_calls.append(messages)
        return json.dumps(
            {
                "reasoning": "南京天气与上一轮公司出差相关",
                "routing": {
                    "intent": "information_query",
                    "confidence": 0.92,
                    "reason": "差旅目的地天气查询",
                    "should_call_skill": True,
                },
                "intents": [
                    {
                        "type": "information_query",
                        "confidence": 0.92,
                        "description": "",
                        "reason": "差旅目的地天气查询",
                        "should_call_skill": True,
                    }
                ],
                "key_entities": {"destination": "南京"},
                "rewritten_query": "查询南京出差期间的天气",
                "agent_schedule": [],
            },
            ensure_ascii=False,
        )

    agent = IntentionAgent(name="IntentionAgent", model=contextual_model)
    result = asyncio.run(
        agent.reply(
            [
                Msg(name="user", content="我下周要去南京出差", role="user"),
                Msg(name="assistant", content="请告诉我具体日期", role="assistant"),
                Msg(name="user", content="那边天气怎么样", role="user"),
            ]
        )
    )
    data = json.loads(result.content)

    assert len(model_calls) == 1
    assert data["routing"]["intent"] == "information_query"
    assert data["routing"]["should_call_skill"] is True
    assert data["agent_schedule"][0]["agent_name"] == "information_query"


def test_programming_request_is_rejected_as_out_of_scope():
    result = guard_user_input("帮我写一个 Python 程序")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.should_call_skill is False
    assert "公司差旅" in result.clarification


def test_private_tourism_request_is_rejected():
    result = guard_user_input("帮我规划三亚蜜月旅游")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.should_call_skill is False


def test_booking_payment_request_passes_guard_to_llm_decision():
    # 无 ticket skill 时 guard 不再硬拒订票请求：LLM 主导，由产品边界 prompt 判定。
    result = guard_user_input("帮我订票付款")

    assert result is None


def test_booking_request_without_ticket_skill_resolves_to_unsupported():
    async def product_boundary_model(_messages):
        return json.dumps(
            {
                "reasoning": "订票不在产品边界内",
                "routing": {
                    "intent": "unsupported",
                    "confidence": 0.95,
                    "reason": "不支持预订",
                    "should_call_skill": False,
                },
                "intents": [
                    {
                        "type": "unsupported",
                        "confidence": 0.95,
                        "description": "",
                        "reason": "不支持预订",
                        "should_call_skill": False,
                    }
                ],
                "key_entities": {},
                "rewritten_query": "帮我订票付款",
                "agent_schedule": [],
            },
            ensure_ascii=False,
        )

    agent = IntentionAgent(name="IntentionAgent", model=product_boundary_model)
    result = asyncio.run(agent.reply(Msg(name="user", content="帮我订票付款", role="user")))
    data = json.loads(result.content)

    assert data["routing"]["intent"] == "unsupported"
    assert data["routing"]["should_call_skill"] is False
    assert data["agent_schedule"] == []


def test_payment_receipt_policy_question_is_not_mistaken_for_payment_action():
    result = guard_user_input("报销需要提供支付明细吗")

    assert result is None


def test_private_tourism_is_rejected_even_after_business_trip_context():
    result = guard_user_input(
        "接下来帮我规划三亚蜜月旅游",
        "用户: 我下周要去南京出差",
    )

    assert result is not None
    assert result.intent == "unsupported"


def test_trip_request_routes_to_trip_planning():
    route = FastIntentRouter.route("我下周去上海出差，帮我安排两天行程")

    assert route is not None
    data = route.to_intention_data("我下周去上海出差，帮我安排两天行程")
    assert data["routing"]["intent"] == "itinerary_planning"
    assert _schedule_agents(route) == [
        "event_collection", "rag_knowledge", "information_query", "itinerary_planning", "trip_compliance"
    ]


def test_policy_query_routes_to_rag_knowledge():
    route = FastIntentRouter.route("餐补标准是多少")

    assert route is not None
    data = route.to_intention_data("餐补标准是多少")
    assert data["routing"]["intent"] == "rag_knowledge"
    assert _schedule_agents(route) == ["rag_knowledge"]


def test_generic_company_standard_does_not_enter_travel_rag_fast_path():
    route = FastIntentRouter.route("公司的年假标准是什么")

    assert route is None


def test_compliance_request_routes_through_trip_context_rag_and_compliance():
    route = FastIntentRouter.route("检查一下南京出差行程是否合规")

    assert route is not None
    data = route.to_intention_data("检查一下南京出差行程是否合规")
    assert data["routing"]["intent"] == "trip_compliance"
    assert _schedule_agents(route) == [
        "event_collection",
        "rag_knowledge",
        "trip_compliance",
    ]


def test_vague_browse_input_is_unclear_without_skill():
    result = guard_user_input("随便看看")

    assert result is not None
    assert result.intent == "unclear"
    assert result.should_call_skill is False
    assert result.agent_schedule == []


def test_information_query_requires_clear_target():
    result = can_call_information_query("查一下", 0.9)

    assert result.intent == "unclear"
    assert result.should_call_skill is False
    assert result.agent_schedule == []


def test_intention_connection_error_falls_back_without_information_query():
    async def failing_model(_messages):
        raise RuntimeError("Connection error")

    agent = IntentionAgent(name="IntentionAgent", model=failing_model)
    result = asyncio.run(agent.reply(Msg(name="user", content="帮我处理一下这个事情", role="user")))
    data = json.loads(result.content)

    assert data["routing"]["intent"] == "fallback"
    assert data["routing"]["should_call_skill"] is False
    assert data["agent_schedule"] == []


def test_low_confidence_skill_call_is_blocked():
    async def low_confidence_model(_messages):
        return json.dumps(
            {
                "reasoning": "low confidence",
                "routing": {
                    "intent": "rag_knowledge",
                    "confidence": 0.4,
                    "reason": "not sure",
                    "should_call_skill": True,
                },
                "intents": [
                    {
                        "type": "rag_knowledge",
                        "confidence": 0.4,
                        "description": "",
                        "reason": "not sure",
                        "should_call_skill": True,
                    }
                ],
                "key_entities": {},
                "rewritten_query": "帮我处理这个内容",
                "agent_schedule": [
                    {
                        "agent_name": "rag_knowledge",
                        "priority": 1,
                        "reason": "not sure",
                        "expected_output": "",
                    }
                ],
            },
            ensure_ascii=False,
        )

    agent = IntentionAgent(name="IntentionAgent", model=low_confidence_model)
    result = asyncio.run(agent.reply(Msg(name="user", content="帮我处理这个内容", role="user")))
    data = json.loads(result.content)

    assert data["routing"]["should_call_skill"] is False
    assert data["intents"][0]["should_call_skill"] is False
    assert data["agent_schedule"] == []


def test_orchestrator_respects_should_not_call_skill():
    orchestrator = OrchestrationAgent(agent_registry={"information_query": object()}, memory_manager=None)
    payload = {
        "routing": {
            "intent": "unclear",
            "confidence": 0.9,
            "reason": "输入过短",
            "should_call_skill": False,
        },
        "agent_schedule": [{"agent_name": "information_query", "priority": 1}],
    }

    result = asyncio.run(
        orchestrator.reply(
            Msg(name="intention", content=json.dumps(payload, ensure_ascii=False), role="assistant")
        )
    )
    data = json.loads(result.content)

    assert data["status"] == "no_agents"
    assert data["results"] == []
