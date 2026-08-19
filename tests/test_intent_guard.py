import asyncio
import json

import pytest

from agents.intention_agent import IntentionAgent
from agentscope.message import Msg
from core.intent_guard import (
    can_call_information_query,
    guard_user_input,
    has_travel_policy_context,
)
from core.intent_router import FastIntentRouter


def test_short_input_does_not_call_information_query():
    result = guard_user_input("你?")

    assert result is not None
    assert result.intent == "unclear"
    assert result.should_call_skill is False


def test_short_field_value_with_trip_context_is_not_blocked():
    # trip intake 收集流程中，短输入是"补字段值"（出差目的"培训"、出发地"北京"），
    # 不应被通用短输入规则拦截为 unclear，应放行给 LLM 结合上下文识别。
    result = guard_user_input(
        "培训",
        "行程框架已保存\n已确认：目的地：南京\n需要补充：\n- 出发地\n- 出差目的",
    )

    assert result is None


def test_short_input_without_context_is_still_blocked():
    # 首轮无上下文时，短输入仍按 unclear 拦截（安全网保持）。
    result = guard_user_input("培训")

    assert result is not None
    assert result.intent == "unclear"
    assert result.should_call_skill is False


def test_chitchat_routes_to_skill():
    route = FastIntentRouter.route("在吗")

    assert route is not None
    data = route.to_intention_data("在吗")
    assert data["routing"]["intent"] == "chitchat"
    assert data["routing"]["should_call_skill"] is True
    assert route.intent_types == ("chitchat",)


def test_greeting_prefix_business_query_is_not_chitchat():
    # P1-1：寒暄词作子串时不得短路业务请求。
    route = FastIntentRouter.route("你好，帮我查一下杭州的差旅报销标准")

    assert route is not None
    assert route.intent_types == ("rag_knowledge",)
    assert route.intent_type != "chitchat"


def test_thanks_prefix_business_query_is_not_chitchat():
    route = FastIntentRouter.route("谢谢，餐补多少")

    assert route is not None
    assert route.intent_types == ("rag_knowledge",)
    assert route.intent_type != "chitchat"


def test_capability_question_still_short_circuits_as_chitchat():
    for query in ("你能做什么", "介绍一下你自己"):
        route = FastIntentRouter.route(query)
        assert route is not None
        assert route.intent_types == ("chitchat",)
        assert route.safe_to_short_circuit is True


def test_social_single_words_short_circuit_as_chitchat():
    for query in ("哈哈", "没事", "明天见"):
        route = FastIntentRouter.route(query)
        assert route is not None
        assert route.intent_types == ("chitchat",)


def test_weather_query_without_trip_context_now_routes_to_information_query():
    route = FastIntentRouter.route("帮我查一下明天东京天气")

    assert route is not None
    assert route.intent_type == "information_query"
    assert route.should_call_skill is True
    assert route.intent_types == ("information_query",)

    result = can_call_information_query("帮我查一下明天东京天气", 0.9)
    assert result.intent == "information_query"
    assert result.should_call_skill is True


def test_ticket_query_routes_to_train_query_without_trip_context():
    route = FastIntentRouter.route("查一下去南京的车票")

    assert route is not None
    assert route.intent_type == "train_query"
    assert route.should_call_skill is True
    assert route.intent_types == ("train_query",)


def test_weather_query_with_place_routes_to_information_query():
    route = FastIntentRouter.route("查南京的天气")

    assert route is not None
    assert route.intent_type == "information_query"
    assert route.should_call_skill is True


def test_booking_request_is_denied_as_unsupported():
    result = guard_user_input("帮我订去南京的火车票")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.should_call_skill is False


def test_generic_play_search_does_not_leak_to_information_query():
    # 「查一下X」但没有天气/交通/差旅上下文 → 不放行为通用搜索（information_query）。
    result = can_call_information_query("查一下北京有什么好玩的", 0.82)

    assert result.intent == "unclear"
    assert result.should_call_skill is False


def test_weather_query_with_trip_context_routes_to_information_query():
    route = FastIntentRouter.route("我明天去东京出差，帮我查一下天气")

    assert route is not None
    data = route.to_intention_data("我明天去东京出差，帮我查一下天气")
    assert data["routing"]["intent"] == "information_query"
    assert data["routing"]["should_call_skill"] is True
    assert route.intent_types == ("information_query",)


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


def test_date_only_followup_inherits_immediately_previous_train_query():
    async def model_must_not_run(_messages):
        raise AssertionError("明确的车票日期补充不应再交给 LLM 猜测")

    agent = IntentionAgent(name="IntentionAgent", model=model_must_not_run)
    result = asyncio.run(
        agent.reply(
            [
                Msg(name="user", content="帮我查一下定南到重庆的车票", role="user"),
                Msg(name="assistant", content="请告诉我出行日期", role="assistant"),
                Msg(name="user", content="明天的", role="user"),
            ]
        )
    )
    data = json.loads(result.content)

    assert data["routing"]["intent"] == "train_query"
    assert data["routing"]["should_call_skill"] is True
    assert data["rewritten_query"] == "帮我查一下定南到重庆的车票，明天的"


def test_date_only_reply_does_not_inherit_non_train_query():
    model_calls = []

    async def contextual_model(_messages):
        model_calls.append(True)
        return json.dumps(
            {
                "reasoning": "补充出差日期",
                "routing": {
                    "intent": "event_collection",
                    "confidence": 0.9,
                    "reason": "补充日期",
                    "should_call_skill": True,
                },
                "intents": [
                    {
                        "type": "event_collection",
                        "confidence": 0.9,
                        "description": "",
                        "reason": "补充日期",
                        "should_call_skill": True,
                    }
                ],
                "key_entities": {"date": "2026-08-18"},
                "rewritten_query": "出差日期为2026-08-18",
            },
            ensure_ascii=False,
        )

    agent = IntentionAgent(name="IntentionAgent", model=contextual_model)
    result = asyncio.run(
        agent.reply(
            [
                Msg(name="user", content="我要去重庆出差", role="user"),
                Msg(name="assistant", content="请补充日期", role="assistant"),
                Msg(name="user", content="明天的", role="user"),
            ]
        )
    )
    data = json.loads(result.content)

    assert model_calls == [True]
    assert data["routing"]["intent"] == "event_collection"


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


def test_booking_payment_request_is_rejected_deterministically():
    # 无 ticket skill 时，订票/付款等交易语言确定性拒绝（产品边界「仅建议、不交易」）；
    # 目录出现 ticket_purchase/payment skill 后 transaction_supported() 变 True 自动放行。
    result = guard_user_input("帮我订票付款")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.should_call_skill is False


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
            },
            ensure_ascii=False,
        )

    agent = IntentionAgent(name="IntentionAgent", model=product_boundary_model)
    result = asyncio.run(agent.reply(Msg(name="user", content="帮我订票付款", role="user")))
    data = json.loads(result.content)

    assert data["routing"]["intent"] == "unsupported"
    assert data["routing"]["should_call_skill"] is False


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
    assert route.intent_types == ("itinerary_planning",)


def test_policy_query_routes_to_rag_knowledge():
    route = FastIntentRouter.route("餐补标准是多少")

    assert route is not None
    data = route.to_intention_data("餐补标准是多少")
    assert data["routing"]["intent"] == "rag_knowledge"
    assert route.intent_types == ("rag_knowledge",)


def test_generic_company_standard_does_not_enter_travel_rag_fast_path():
    route = FastIntentRouter.route("公司的年假标准是什么")

    assert route is None


@pytest.mark.parametrize(
    "query",
    ("采购审批流程是什么", "医疗费报销流程是什么", "政府补贴标准是多少"),
)
def test_non_travel_policy_language_does_not_enter_travel_rag(query):
    assert FastIntentRouter.route(query) is None


def test_explicit_non_travel_policy_does_not_inherit_previous_trip_context():
    assert has_travel_policy_context(
        "另外医疗费报销流程是什么",
        "用户: 我下周去南京出差",
    ) is False


@pytest.mark.parametrize(
    ("query", "expected"),
    (("我喜欢靠窗座位", "preference"), ("我以前去过北京吗", "memory_query")),
)
def test_user_scoped_preference_and_memory_do_not_require_repeated_trip_keyword(query, expected):
    route = FastIntentRouter.route(query)

    assert route is not None
    assert route.intent_types == (expected,)


def test_compliance_request_routes_through_trip_context_rag_and_compliance():
    route = FastIntentRouter.route("检查一下南京出差行程是否合规")

    assert route is not None
    data = route.to_intention_data("检查一下南京出差行程是否合规")
    assert data["routing"]["intent"] == "trip_compliance"
    assert route.intent_types == ("trip_compliance",)


def test_policy_structure_query_is_not_mislabeled_as_trip():
    # P1-3：裸"从X到Y出差"结构句是政策问题，不得产生 itinerary_planning 候选，
    # 否则会以单个可短路意图触发 5 步行程采集，政策问题永不回答。
    route = FastIntentRouter.route("从北京出差到上海的规定")

    assert route is None
    candidates = FastIntentRouter.detect("从北京出差到上海的规定")
    assert all(candidate.type != "itinerary_planning" for candidate in candidates)


def test_route_structure_with_plan_verb_still_routes_to_trip():
    # 带显式规划动词的结构句仍是真正的行程请求，快路由行为保持。
    route = FastIntentRouter.route("从北京到上海出差，帮我安排行程")

    assert route is not None
    assert route.intent_types == ("itinerary_planning",)
    assert route.safe_to_short_circuit is True


def test_mixed_plan_and_policy_query_stays_composite_not_short_circuited():
    # 混合句的 trip 候选必须保留，且因有两个可调用意图而不短路（LLM 复合处理）。
    route = FastIntentRouter.route("帮我规划去上海的行程，报销标准是多少")

    assert route is not None
    assert set(route.intent_types) == {"itinerary_planning", "rag_knowledge"}
    assert route.safe_to_short_circuit is False


def test_vague_browse_input_is_unclear_without_skill():
    result = guard_user_input("随便看看")

    assert result is not None
    assert result.intent == "unclear"
    assert result.should_call_skill is False


def test_information_query_requires_clear_target():
    result = can_call_information_query("查一下", 0.9)

    assert result.intent == "unclear"
    assert result.should_call_skill is False


def test_train_query_sentence_is_never_authorized_to_information_query():
    result = can_call_information_query("帮我查一下这周四上海到北京的高铁车次", 0.9)

    assert result.intent != "information_query"
    assert result.should_call_skill is False
    assert "train_query" in result.reason


def test_train_query_sentence_passes_guard_and_routes_to_train_query():
    assert guard_user_input("帮我查一下这周四上海到北京的高铁车次") is None

    route = FastIntentRouter.route("帮我查一下这周四上海到北京的高铁车次")
    assert route is not None
    assert route.intent_types == ("train_query",)
    assert route.intent_type == "train_query"
    assert route.safe_to_short_circuit is True


def test_intention_connection_error_falls_back_without_information_query():
    async def failing_model(_messages):
        raise RuntimeError("Connection error")

    agent = IntentionAgent(name="IntentionAgent", model=failing_model)
    result = asyncio.run(agent.reply(Msg(name="user", content="帮我处理一下这个事情", role="user")))
    data = json.loads(result.content)

    assert data["routing"]["intent"] == "fallback"
    assert data["routing"]["should_call_skill"] is False


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
            },
            ensure_ascii=False,
        )

    agent = IntentionAgent(name="IntentionAgent", model=low_confidence_model)
    result = asyncio.run(agent.reply(Msg(name="user", content="帮我处理这个内容", role="user")))
    data = json.loads(result.content)

    assert data["routing"]["should_call_skill"] is False
    assert data["intents"][0]["should_call_skill"] is False
