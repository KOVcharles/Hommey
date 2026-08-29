"""Fast rule-based intent routing before LLM intent recognition.

router 只在高置信度、可证明无歧义时短路（LLM 主导识别）：关键词表作为数据来自
core/guard_rules.py。这里只识别和授权意图，不生成可执行计划。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Tuple

from core.intent_guard import (
    GuardResult,
    can_call_information_query,
    guard_user_input,
    has_business_travel_context,
    has_travel_policy_context,
    is_pure_chitchat,
    passes_confidence_gate,
)
from core.guard_rules import (
    COMPLIANCE_KEYWORDS,
    GENERIC_POLICY_KEYWORDS,
    MEMORY_KEYWORDS,
    PLACE_INFORMATION_KEYWORDS,
    POLICY_KEYWORDS,
    PREFERENCE_KEYWORDS,
    SEARCH_KEYWORDS,
    TRAIN_KEYWORDS,
    TRIP_KEYWORDS,
    WEATHER_KEYWORDS,
)


@dataclass(frozen=True)
class IntentCandidate:
    type: str
    confidence: float
    reason: str
    source: str = "rule"


@dataclass(frozen=True)
class IntentRoute:
    intent_type: str
    confidence: float
    reason: str
    key_entities: Dict[str, Any]
    should_call_skill: bool = True
    # Keep all authorized candidates so callers can distinguish a genuinely
    # single fast route from a multi-intent request without inspecting a plan.
    intent_types: Tuple[str, ...] = ()
    # Fast routing is an optimization, not an authority boundary.  Callers may
    # bypass full recognition only when this flag proves the query is a complete
    # single-intent request rather than one recognized clause of a mixed ask.
    safe_to_short_circuit: bool = False

    def to_intention_data(self, user_query: str) -> Dict[str, Any]:
        return {
            "routing": {
                "intent": self.intent_type,
                "confidence": self.confidence,
                "reason": self.reason,
                "should_call_skill": self.should_call_skill,
            },
            "reasoning": f"Fast intent router: {self.reason}",
            "intents": [
                {
                    "type": self.intent_type,
                    "confidence": self.confidence,
                    "description": self.reason,
                    "reason": self.reason,
                    "should_call_skill": self.should_call_skill,
                }
            ],
            "key_entities": self.key_entities,
            "rewritten_query": user_query,
        }


def message_for_non_skill_intent(intent: str) -> str:
    """Default response for a recognized intent that cannot call a skill."""
    if intent == "unsupported":
        return (
            "这个问题不属于公司差旅规划或报销范围，我暂时无法处理。"
            "我可以帮你查询差旅政策、规划出差路线，或准备报销材料。"
        )
    return "我还不太确定这是否与公司差旅有关。请补充出差目的地、日期，或说明要查询的政策和报销问题。"


class FastIntentRouter:
    """Cheap high-confidence router for common user requests."""

    @classmethod
    def route(cls, user_query: str) -> Optional[IntentRoute]:
        q = (user_query or "").strip()
        if not q:
            return None

        guard_result = guard_user_input(q)
        if guard_result:
            return cls._from_guard_result(guard_result)

        if is_pure_chitchat(q):
            return cls._single("chitchat", 0.99, "明确的寒暄或社交对话")

        candidates = cls.detect(q)
        if candidates:
            intents = [
                {
                    "type": candidate.type,
                    "confidence": candidate.confidence,
                    "description": candidate.reason,
                    "reason": candidate.reason,
                    "should_call_skill": passes_confidence_gate(candidate.type, candidate.confidence),
                }
                for candidate in candidates
            ]
            callable_intents = [item for item in intents if item["should_call_skill"]]
            primary = callable_intents[0] if callable_intents else intents[0]
            return IntentRoute(
                intent_type=primary["type"],
                confidence=primary["confidence"],
                reason=primary["reason"],
                key_entities={},
                should_call_skill=bool(callable_intents),
                intent_types=tuple(item["type"] for item in callable_intents),
                safe_to_short_circuit=(
                    len(callable_intents) == 1 and not cls._potentially_composite(q)
                ),
            )

        return None

    @classmethod
    def detect(cls, user_query: str) -> List[IntentCandidate]:
        """Collect rule-based business intent candidates without first-match exit."""
        q = (user_query or "").strip()
        if not q:
            return []

        candidates: List[IntentCandidate] = []

        has_policy = (
            any(keyword in q for keyword in POLICY_KEYWORDS)
            or any(keyword in q for keyword in GENERIC_POLICY_KEYWORDS)
        ) and has_travel_policy_context(q)
        has_weather = any(keyword in q for keyword in WEATHER_KEYWORDS)
        has_search = any(keyword in q for keyword in SEARCH_KEYWORDS)
        has_place_information = any(keyword in q for keyword in PLACE_INFORMATION_KEYWORDS)
        has_compliance = any(keyword in q for keyword in COMPLIANCE_KEYWORDS)
        has_train = any(keyword in q for keyword in TRAIN_KEYWORDS)

        if any(keyword in q for keyword in MEMORY_KEYWORDS):
            candidates.append(IntentCandidate("memory_query", 0.9, "询问用户自己的历史或偏好记忆"))

        if any(keyword in q for keyword in PREFERENCE_KEYWORDS):
            candidates.append(IntentCandidate("preference", 0.9, "表达或更新用户偏好"))

        if has_policy:
            candidates.append(IntentCandidate("rag_knowledge", 0.88, "查询差旅制度、标准或报销政策"))

        if has_compliance:
            candidates.append(IntentCandidate("trip_compliance", 0.9, "依据公司制度检查差旅行程合规性"))

        # 车次/高铁/火车 → train_query。can_call_information_query 对车次句
        # 提前返回 unclear，因此这里不会与 information_query 候选同时出现。
        if has_train and has_business_travel_context(q) and not has_policy and not has_compliance:
            candidates.append(IntentCandidate("train_query", 0.9, "查询高铁/火车车次、时刻与余票"))

        if has_weather:
            info_guard = can_call_information_query(q, 0.9)
            if info_guard.intent == "information_query" and info_guard.should_call_skill:
                candidates.append(IntentCandidate("information_query", info_guard.confidence, info_guard.reason))

        if has_place_information and not cls._looks_like_trip_request(q):
            info_guard = can_call_information_query(q, 0.9)
            if info_guard.intent == "information_query" and info_guard.should_call_skill:
                candidates.append(IntentCandidate("information_query", info_guard.confidence, info_guard.reason))

        if cls._looks_like_trip_request(q):
            candidates.append(IntentCandidate("itinerary_planning", 0.88, "明确的行程规划或出行意图"))

        # Generic search verbs like “查一下” should not turn policy/RAG queries
        # such as “查一下出差补贴” into an external information_query.
        if has_search and not has_policy and not has_compliance:
            info_guard = can_call_information_query(q, 0.82)
            if info_guard.intent == "information_query" and info_guard.should_call_skill:
                candidates.append(IntentCandidate("information_query", info_guard.confidence, info_guard.reason))

        return cls._dedupe_candidates(candidates)

    @classmethod
    def _single(cls, intent_type: str, confidence: float, reason: str) -> IntentRoute:
        return IntentRoute(
            intent_type=intent_type,
            confidence=confidence,
            reason=reason,
            key_entities={},
            intent_types=(intent_type,),
            safe_to_short_circuit=True,
        )

    @classmethod
    def _from_guard_result(cls, result: GuardResult) -> IntentRoute:
        return IntentRoute(
            intent_type=result.intent,
            confidence=result.confidence,
            reason=result.reason,
            key_entities={},
            should_call_skill=result.should_call_skill and passes_confidence_gate(result.intent, result.confidence),
            intent_types=(result.intent,) if result.should_call_skill else (),
            safe_to_short_circuit=True,
        )

    @staticmethod
    def _potentially_composite(query: str) -> bool:
        """Conservatively reject fast-pathing when the wording joins clauses."""
        connectors = (
            "然后", "顺便", "同时", "以及", "并且", "另外", "还要", "还想",
            "再帮", "除此之外", "一并", "分别", "又", "并",
        )
        return any(term in (query or "") for term in connectors)

    @classmethod
    def _dedupe_candidates(cls, candidates: List[IntentCandidate]) -> List[IntentCandidate]:
        by_type: Dict[str, IntentCandidate] = {}
        order: List[str] = []
        for candidate in candidates:
            existing = by_type.get(candidate.type)
            if existing is None:
                by_type[candidate.type] = candidate
                order.append(candidate.type)
            elif candidate.confidence > existing.confidence:
                by_type[candidate.type] = candidate
        return [by_type[intent_type] for intent_type in order]

    @classmethod
    def _looks_like_trip_request(cls, query: str) -> bool:
        natural_plan = bool(re.search(
            r"(?:帮我)?(?:安排|规划)(?:一下)?(?:去|到|前往).{2,20}",
            query or "",
        ))
        structured_facts = cls._looks_like_structured_trip_facts(query)
        if not (has_business_travel_context(query) or natural_plan or structured_facts):
            return False
        explicit_plan = any(keyword in query for keyword in TRIP_KEYWORDS) or (
            "计划" in query and any(keyword in query for keyword in ("出差", "差旅", "去", "前往"))
        ) or natural_plan or structured_facts
        if explicit_plan:
            if natural_plan or structured_facts:
                return True
            if "从" in query and ("到" in query or "去" in query):
                return True
            return any(keyword in query for keyword in (
                "去", "规划", "安排", "计划", "行程", "路线", "出差", "差旅",
            ))
        return False

    @staticmethod
    def _looks_like_structured_trip_facts(query: str) -> bool:
        """Treat a bundle of trip slots as an implicit planning request."""
        text = query or ""
        if any(term in text for term in (
            "天气", "气温", "下雨", "车票", "车次", "高铁", "火车",
            "政策", "标准", "报销", "合规", "查", "查询",
        )):
            return False
        strong_markers = [
            bool(re.search(r"(?:出发地|从)[^，,。]{1,12}", text)),
            bool(re.search(r"(?:目的地)[^，,。]{1,12}", text)),
            bool(re.search(r"\d+\s*天", text)),
            any(term in text for term in ("客户拜访", "拜访客户", "会议", "培训", "出差目的")),
        ]
        return sum(strong_markers) >= 2 and has_business_travel_context(text)
