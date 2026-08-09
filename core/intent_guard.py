"""Lightweight intent guard used before skill routing.

guard 只做"可证明无歧义"的安全网（LLM 主导识别）：垃圾/寒暄/明确领域外/
高风险操作在此短路；订票、付款等交易语言**放行**给 LLM 由产品边界判定。
关键词表已收敛到 core/guard_rules.py（声明式数据）；本模块只保留判定逻辑，
新增"购买车票"类 skill 时无需改任何 Python。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, Optional, Tuple

from core.guard_rules import (
    BUSINESS_TRAVEL_KEYWORDS,
    COMPLIANCE_KEYWORDS,
    FORBIDDEN_ACTIONS,
    GENERIC_POLICY_KEYWORDS,
    GIBBERISH_RE,
    MEMORY_KEYWORDS,
    OUT_OF_SCOPE_KEYWORDS,
    PERSONAL_TRAVEL_KEYWORDS,
    POLICY_KEYWORDS,
    PREFERENCE_KEYWORDS,
    SEARCH_KEYWORDS,
    TRAVEL_TRANSPORT_KEYWORDS,
    TRIP_KEYWORDS,
    UNCLEAR_EXACT,
    WEATHER_KEYWORDS,
)
from core.intent_catalog import (
    CHITCHAT_EXACT,
    CHITCHAT_KEYWORDS,
    confidence_threshold_for_intent,
)


DEFAULT_CONFIDENCE_THRESHOLD = 0.65
INFORMATION_QUERY_THRESHOLD = 0.75

# is_pure_chitchat 的残余文本业务标记：剥离寒暄词后若仍含这些词，
# 说明不是纯闲聊（带业务前缀的问候/感谢应进入意图识别而非被短路）。
_CHITCHAT_RESIDUAL_BUSINESS_MARKERS: Tuple[str, ...] = (
    BUSINESS_TRAVEL_KEYWORDS + POLICY_KEYWORDS + GENERIC_POLICY_KEYWORDS
    + COMPLIANCE_KEYWORDS + WEATHER_KEYWORDS + SEARCH_KEYWORDS
    + MEMORY_KEYWORDS + PREFERENCE_KEYWORDS + TRIP_KEYWORDS
    + TRAVEL_TRANSPORT_KEYWORDS + ("规定", "制度", "预订", "订票")
)


@dataclass(frozen=True)
class GuardResult:
    intent: str
    confidence: float
    reason: str
    should_call_skill: bool
    clarification: Optional[str] = None

    def to_intention_data(self, user_query: str) -> Dict[str, Any]:
        return {
            "routing": {
                "intent": self.intent,
                "confidence": self.confidence,
                "reason": self.reason,
                "should_call_skill": self.should_call_skill,
            },
            "reasoning": self.reason,
            "intents": [
                {
                    "type": self.intent,
                    "confidence": self.confidence,
                    "description": self.reason,
                    "reason": self.reason,
                    "should_call_skill": self.should_call_skill,
                }
            ],
            "key_entities": {},
            "rewritten_query": normalize_query(user_query),
            "clarification": self.clarification,
        }


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

    # 交易/订票语言不在此短路：放行给 LLM，由产品边界 prompt 判定 unsupported。
    # 目录中出现 ticket 类 skill 后，同一批关键词自然成为可识别意图（纯声明式）。

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


def passes_confidence_gate(intent: str, confidence: float) -> bool:
    """意图级置信度门槛；未声明时按意图默认值（information_query 更严）。"""
    threshold = confidence_threshold_for_intent(intent)
    if threshold is None:
        threshold = (
            INFORMATION_QUERY_THRESHOLD
            if intent == "information_query"
            else DEFAULT_CONFIDENCE_THRESHOLD
        )
    return confidence >= threshold


def can_call_information_query(
    user_query: str,
    confidence: float,
    conversation_context: str = "",
) -> GuardResult:
    q = normalize_query(user_query)
    length = meaningful_length(q)

    if confidence < INFORMATION_QUERY_THRESHOLD:
        return _unclear(
            f"information_query 置信度 {confidence:.2f} 低于阈值 {INFORMATION_QUERY_THRESHOLD:.2f}"
        )

    if length < 6:
        return _unclear("信息查询输入过短，缺少明确查询对象")

    if not has_clear_information_target(q):
        return _unclear("缺少明确的信息查询对象")

    if not has_travel_information_context(q, conversation_context):
        return GuardResult(
            intent="unclear",
            confidence=0.9,
            reason="信息查询缺少公司差旅行程上下文",
            should_call_skill=False,
            clarification="这项查询是用于公司出差吗？请补充出差目的地或当前行程，我再帮你查询相关信息。",
        )

    return GuardResult(
        intent="information_query",
        confidence=confidence,
        reason="明确的信息查询请求",
        should_call_skill=True,
    )


def has_clear_information_target(query: str) -> bool:
    if any(keyword in query for keyword in ("天气", "气温", "下雨", "预报")):
        return meaningful_length(query) >= 6

    if any(keyword in query for keyword in ("开放时间", "门票", "航班", "高铁", "路线", "价格", "地址")):
        return True

    search_words = ("查一下", "搜索", "查询", "了解一下")
    if any(word in query for word in search_words):
        remainder = query
        for word in search_words:
            remainder = remainder.replace(word, "")
        remainder = remainder.replace("帮我", "").replace("一下", "").strip(" ，。？?！!")
        return meaningful_length(remainder) >= 4

    return False


def has_business_travel_context(query: str, conversation_context: str = "") -> bool:
    """Return whether the request belongs to the corporate-travel workflow."""
    combined = f"{normalize_query(query)}\n{conversation_context or ''}"
    if any(keyword in combined for keyword in BUSINESS_TRAVEL_KEYWORDS):
        return True

    # In a company travel assistant, an explicit route between places or a
    # transport/hotel request is a valid travel task even if the user omits
    # the word "出差".
    if re.search(r"从.{1,20}(到|去|前往).{1,20}", combined):
        return True
    if any(keyword in combined for keyword in TRAVEL_TRANSPORT_KEYWORDS):
        return True
    if re.search(r"(去|前往).{1,20}(路线|怎么走|如何走)", combined):
        return True
    if re.search(r"(规划|安排).{0,8}(路线|行程)", combined):
        return True
    return False


def has_explicit_business_context(query: str, conversation_context: str = "") -> bool:
    """Stricter check used when a request explicitly looks like private travel."""
    combined = f"{normalize_query(query)}\n{conversation_context or ''}"
    explicit_terms = (
        "出差", "差旅", "商旅", "商务行程", "公务出行", "拜访客户", "客户拜访",
        "会议地点", "会场", "差旅任务", "出差任务", "报销", "差旅政策", "差旅制度",
    )
    return any(keyword in combined for keyword in explicit_terms)


def has_travel_information_context(query: str, conversation_context: str = "") -> bool:
    """Information tools are available only as support for a travel task."""
    return has_business_travel_context(query, conversation_context)


def is_pure_chitchat(query: str) -> bool:
    """Whether a query is pure social chitchat (safe to short-circuit).

    Exact greetings always qualify.  Longer inputs qualify only when every
    chitchat keyword can be stripped and nothing business-intent-like remains
    in the residue — so "你好，帮我查一下差旅报销标准" must NOT short-circuit,
    while "你能做什么" / "介绍一下你自己" still do.
    """
    q = normalize_query(query)
    q_lower = q.lower()
    if not q:
        return False
    if q_lower in CHITCHAT_EXACT or q in CHITCHAT_EXACT:
        return True
    if not any(keyword in q for keyword in CHITCHAT_KEYWORDS):
        return False
    residual = q
    for keyword in CHITCHAT_KEYWORDS:
        residual = residual.replace(keyword, "")
    residual = re.sub(r"[\s，。！？,!?、~～…—·：:;；（）()]+", "", residual)
    if not residual:
        return True
    if len(residual) > 4:
        return False
    return not any(keyword in residual for keyword in _CHITCHAT_RESIDUAL_BUSINESS_MARKERS)


def is_limited_chitchat(query: str) -> bool:
    """Allow social niceties and capability questions, not open-domain chat."""
    return is_pure_chitchat(query)


def _unclear(reason: str) -> GuardResult:
    return GuardResult(
        intent="unclear",
        confidence=0.9,
        reason=reason,
        should_call_skill=False,
        clarification="我还不太确定你的意思。你是想查询差旅政策、规划行程，还是查某个旅行信息？",
    )


def _chitchat() -> GuardResult:
    """寒暄/社交对话：路由到 chitchat skill（skill-backed）。"""
    return GuardResult(
        intent="chitchat",
        confidence=0.99,
        reason="明确的寒暄或社交对话",
        should_call_skill=True,
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
