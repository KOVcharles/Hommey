"""Deterministic authorization boundary between intent analysis and execution."""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from core.intent_catalog import (
    catalog_rank,
    execution_steps_for_intent,
    intent_to_skill,
    is_routable_intent,
    is_skill_intent,
)
from core.intent_guard import (
    can_call_information_query,
    guard_user_input,
    has_business_travel_context,
    has_train_intent,
    has_travel_policy_context,
    is_limited_chitchat,
    passes_confidence_gate,
)
from core.intent_result import IntentAnalysis, IntentGroup, coerce_intent_analysis


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str
    intent: str
    authorized: bool
    reason_code: str
    skill: Optional[str] = None


class PolicyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: IntentAnalysis
    decisions: List[PolicyDecision]
    primary_intent: str
    clarification: str = ""

    @property
    def authorized_decisions(self) -> List[PolicyDecision]:
        return [decision for decision in self.decisions if decision.authorized]

    def to_compatibility_dict(self, original_query: str) -> dict:
        """Expose the legacy envelope while callers migrate to groups/decisions."""
        decision_by_group = {decision.group_id: decision for decision in self.decisions}
        legacy_intents = []
        for group in self.analysis.groups:
            decision = decision_by_group[group.group_id]
            legacy_intents.append({
                "type": group.intent,
                "group_id": group.group_id,
                "query": group.query,
                "entities": dict(group.entities),
                "confidence": group.confidence,
                "description": "",
                "reason": decision.reason_code,
                "should_call_skill": decision.authorized,
            })

        authorized = self.authorized_decisions
        should_call = bool(authorized)
        routing_intent = self.primary_intent or "unclear"
        primary_group = next(
            (group for group in self.analysis.groups if group.intent == routing_intent),
            self.analysis.groups[0] if self.analysis.groups else None,
        )
        confidence = primary_group.confidence if primary_group is not None else 0.0
        reason = next(
            (
                decision.reason_code for decision in self.decisions
                if decision.intent == routing_intent
            ),
            "NO_RECOGNIZED_INTENT",
        )
        rewritten_query = (
            primary_group.query
            if len(self.analysis.groups) == 1 and primary_group is not None
            else original_query
        )
        payload = self.analysis.model_dump(by_alias=True)
        payload.update({
            "original_query": original_query,
            "policy_decisions": [item.model_dump() for item in self.decisions],
            "routing": {
                "intent": routing_intent,
                "primary_intent": routing_intent,
                "confidence": confidence,
                "reason": reason,
                "should_call_skill": should_call,
                "mode": "multi" if len(authorized) > 1 else ("single" if should_call else "none"),
            },
            "reasoning": "Routing derived by OrchestrationPolicy",
            "intents": legacy_intents,
            "key_entities": self._shared_entities(),
            "rewritten_query": rewritten_query,
        })
        if self.clarification:
            payload["clarification"] = self.clarification
        return payload

    def _shared_entities(self) -> dict:
        """Legacy projection includes only non-conflicting values across groups."""
        values: Dict[str, object] = {}
        conflicts: set[str] = set()
        for group in self.analysis.groups:
            for key, value in group.entities.items():
                if value is None or value == "" or key in conflicts:
                    continue
                if key in values and values[key] != value:
                    values.pop(key, None)
                    conflicts.add(key)
                else:
                    values[key] = value
        return values


class OrchestrationPolicy:
    """Authorize semantic groups without trusting LLM execution decisions."""

    def evaluate(
        self,
        analysis_or_payload: IntentAnalysis | dict,
        *,
        original_query: str,
        conversation_context: str = "",
    ) -> PolicyEvaluation:
        analysis = (
            analysis_or_payload
            if isinstance(analysis_or_payload, IntentAnalysis)
            else coerce_intent_analysis(analysis_or_payload, original_query)
        )
        input_guard = guard_user_input(original_query, conversation_context)
        decisions = [
            self._evaluate_group(group, original_query, conversation_context, input_guard)
            for group in analysis.groups
        ]
        decisions = self._apply_required_context_policy(analysis, decisions)
        authorized_groups = [
            group for group, decision in zip(analysis.groups, decisions)
            if decision.authorized
        ]
        if authorized_groups:
            primary = self._select_primary_intent(authorized_groups).intent
            clarification = ""
        else:
            recognized = {group.intent for group in analysis.groups}
            if "unsupported" in recognized:
                primary = "unsupported"
            elif "fallback" in recognized:
                primary = "fallback"
            else:
                primary = "unclear"
            clarification = (
                input_guard.clarification
                if input_guard is not None and input_guard.clarification
                else self._default_clarification(primary)
            )
        return PolicyEvaluation(
            analysis=analysis,
            decisions=decisions,
            primary_intent=primary,
            clarification=clarification,
        )

    @staticmethod
    def _apply_required_context_policy(
        analysis: IntentAnalysis,
        decisions: List[PolicyDecision],
    ) -> List[PolicyDecision]:
        """Fail closed when a Goal requires context that cannot be authorized."""
        by_group = {decision.group_id: decision for decision in decisions}
        changed = True
        while changed:
            changed = False
            for relation in analysis.relations:
                if relation.type != "required_context":
                    continue
                target = by_group[relation.target]
                if not target.authorized:
                    continue
                if any(not by_group[source].authorized for source in relation.sources):
                    by_group[relation.target] = target.model_copy(update={
                        "authorized": False,
                        "reason_code": "REQUIRED_CONTEXT_NOT_AUTHORIZED",
                    })
                    changed = True
        return [by_group[group.group_id] for group in analysis.groups]

    def _evaluate_group(
        self,
        group: IntentGroup,
        original_query: str,
        conversation_context: str,
        input_guard,
    ) -> PolicyDecision:
        skill = intent_to_skill(group.intent)
        if input_guard is not None and input_guard.intent in {"unclear", "unsupported"}:
            return PolicyDecision(
                group_id=group.group_id,
                intent=group.intent,
                authorized=False,
                reason_code=f"INPUT_GUARD_{input_guard.intent.upper()}",
                skill=skill,
            )
        if not is_skill_intent(group.intent) or not is_routable_intent(group.intent):
            return PolicyDecision(
                group_id=group.group_id,
                intent=group.intent,
                authorized=False,
                reason_code="NON_SKILL_INTENT",
                skill=None,
            )

        query = group.query or original_query
        authorized = self._should_authorize(
            query=query,
            intent=group.intent,
            confidence=group.confidence,
            conversation_context=conversation_context,
        )
        return PolicyDecision(
            group_id=group.group_id,
            intent=group.intent,
            authorized=authorized,
            reason_code="AUTHORIZED" if authorized else "POLICY_REJECTED",
            skill=skill,
        )

    @staticmethod
    def _should_authorize(
        *, query: str, intent: str, confidence: float, conversation_context: str,
    ) -> bool:
        if intent == "chitchat":
            return is_limited_chitchat(query) and passes_confidence_gate(intent, confidence)
        if intent == "information_query":
            result = can_call_information_query(query, confidence, conversation_context)
            return result.intent == "information_query" and result.should_call_skill
        if intent == "train_query":
            return has_train_intent(query) and passes_confidence_gate(intent, confidence)
        if intent in {"preference", "memory_query"}:
            return passes_confidence_gate(intent, confidence)
        if intent == "rag_knowledge":
            return (
                has_travel_policy_context(query, conversation_context)
                and passes_confidence_gate(intent, confidence)
            )
        return (
            has_business_travel_context(query, conversation_context)
            and passes_confidence_gate(intent, confidence)
        )

    @staticmethod
    def _select_primary_intent(groups: List[IntentGroup]) -> IntentGroup:
        def sort_key(group: IntentGroup):
            return (
                -len(execution_steps_for_intent(group.intent)),
                catalog_rank(group.intent),
                -group.confidence,
            )
        return min(groups, key=sort_key)

    @staticmethod
    def _default_clarification(intent: str) -> str:
        if intent == "unsupported":
            return (
                "这个问题不属于公司差旅规划或报销范围，我暂时无法处理。"
                "我可以帮你查询差旅政策、规划出差路线，或准备报销材料。"
            )
        return (
            "我还不太确定这是否与公司差旅有关。请补充出差目的地、日期，"
            "或说明要查询的差旅政策和报销问题。"
        )
