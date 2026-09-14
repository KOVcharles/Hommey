import asyncio
import json

import pytest

from core.intent_guard import (
    guard_user_input,
)


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


def test_booking_request_is_denied_as_unsupported():
    result = guard_user_input("帮我订去南京的火车票")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.should_call_skill is False


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
    result = guard_user_input("帮我订票付款")

    assert result is not None
    assert result.intent == "unsupported"
    assert result.should_call_skill is False


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


def test_vague_browse_input_is_unclear_without_skill():
    result = guard_user_input("随便看看")

    assert result is not None
    assert result.intent == "unclear"
    assert result.should_call_skill is False
