"""Reject clear out-of-scope requests before the supervisor; ambiguous input stays with the model."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from core.guard_rules import (BOOKING_KEYWORDS, FORBIDDEN_ACTIONS, GIBBERISH_RE,
    OUT_OF_SCOPE_KEYWORDS, PERSONAL_TRAVEL_KEYWORDS, UNCLEAR_EXACT)
from core.intent_catalog import CHITCHAT_EXACT


@dataclass(frozen=True)
class GuardResult:
    intent: str
    confidence: float
    reason: str
    should_call_skill: bool
    clarification: Optional[str] = None


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip())


def meaningful_length(query: str) -> int:
    return len(re.findall(r"[\w\u4e00-\u9fff]", query or ""))


def guard_user_input(user_query: str, conversation_context: str = "") -> Optional[GuardResult]:
    q = normalize_query(user_query)
    q_lower = q.lower()
    length = meaningful_length(q)

    if not q:
        return _unclear("输入为空，缺少可识别的用户意图")

    if q_lower in CHITCHAT_EXACT or q in CHITCHAT_EXACT:
        return _chitchat()

    if q_lower in UNCLEAR_EXACT or q in UNCLEAR_EXACT:
        return _unclear("输入过短或缺少明确任务")

    if GIBBERISH_RE.match(q):
        return _unclear("输入疑似乱码或只有标点")

    if any(keyword in q for keyword in FORBIDDEN_ACTIONS):
        return _unsupported("用户请求包含系统不应执行的高风险操作")

    # 交易操作始终在产品权限之外。
    if has_booking_intent(q):
        return _unsupported("预订、购票或付款等交易请求不属于本助手范围，本助手仅提供查询与规划建议。")

    if any(keyword in q_lower for keyword in OUT_OF_SCOPE_KEYWORDS):
        return _unsupported("用户请求与公司差旅规划或报销无关")

    if (
        any(keyword in q for keyword in PERSONAL_TRAVEL_KEYWORDS)
        and not has_explicit_business_context(q)
    ):
        return _unsupported("用户请求是私人旅游需求，不属于公司差旅范围")

    if length <= 2 and not conversation_context:
        return _unclear("输入太短，无法判断具体意图")

    return None


def has_booking_intent(query: str) -> bool:
    """预订/购票/付款等交易语言 → 产品边界「仅建议、不交易」，拒绝为 unsupported。"""
    return any(keyword in normalize_query(query) for keyword in BOOKING_KEYWORDS)


def has_explicit_business_context(query: str, conversation_context: str = "") -> bool:
    """Stricter check used when a request explicitly looks like private travel."""
    combined = f"{normalize_query(query)}\n{conversation_context or ''}"
    explicit_terms = (
        "出差", "差旅", "商旅", "商务行程", "公务出行", "拜访客户", "客户拜访",
        "会议地点", "会场", "差旅任务", "出差任务", "报销", "差旅政策", "差旅制度",
    )
    return any(keyword in combined for keyword in explicit_terms)


def _unclear(reason: str) -> GuardResult:
    return GuardResult(
        intent="unclear",
        confidence=0.9,
        reason=reason,
        should_call_skill=False,
        clarification="我还不太确定你的意思。你是想查询差旅政策、规划行程，还是查某个旅行信息？",
    )


def _chitchat() -> GuardResult:
    """A fixed greeting; no specialist execution is needed."""
    return GuardResult(
        intent="chitchat",
        confidence=0.99,
        reason="明确的寒暄或社交对话",
        should_call_skill=False,
    )


def _unsupported(reason: str) -> GuardResult:
    return GuardResult(
        intent="unsupported",
        confidence=0.95,
        reason=reason,
        should_call_skill=False,
        clarification=(
            "这个问题不属于公司差旅规划或报销范围，我暂时无法处理。"
            "我可以帮你查询差旅政策、规划出差路线，或准备报销材料。"
        ),
    )
