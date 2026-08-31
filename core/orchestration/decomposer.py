"""Deterministic conversion from authorized intent groups to semantic Goals."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from core.intent_catalog import execution_steps_for_intent
from core.intent_result import coerce_intent_analysis

from .graph_builder import TaskGraphBuilder

_DATE_PATTERN = re.compile(r"(今天|明天|后天|这两天|未来两天|未来几天|本周|下周|\d{1,2}月\d{1,2}日)")
_DESTINATION_PATTERNS = (
    re.compile(r"(?:去|到|前往)([一-鿿]{2,10}?)(?:市)?(?:出差|差旅)"),
    re.compile(
        r"(?:查|查询|看看|了解)?(?:一下)?(?:今天|明天|后天|这两天|未来两天|未来几天)?"
        r"([一-鿿]{2,8}?)(?:市)?(?:的)?天气"
    ),
)


class TaskDecomposer:
    """Compatibility name for the now deterministic IntentGroup adapter."""

    def __init__(self, model=None):
        # ``model`` remains accepted while runtime construction rolls forward.
        # It is intentionally unused: query isolation now happens in IntentionAgent.
        self.model = None

    async def decompose(self, query: str, intention_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self.from_analysis(query, intention_data)

    @staticmethod
    def from_analysis(query: str, intention_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        analysis = coerce_intent_analysis(intention_data, query)
        authorized = TaskDecomposer._authorized_group_ids(intention_data, analysis)
        dependencies: Dict[str, List[str]] = {}
        for relation in analysis.relations:
            if relation.type not in {"required_context", "sequence"}:
                continue
            if relation.target not in authorized:
                continue
            dependencies.setdefault(relation.target, []).extend(
                source for source in relation.sources if source in authorized
            )

        tasks = []
        for order, group in enumerate(analysis.groups):
            if group.group_id not in authorized:
                continue
            tasks.append({
                "task_id": group.group_id,
                "intent": group.intent,
                "query": group.query,
                "entities": dict(group.entities),
                "depends_on": list(dict.fromkeys(dependencies.get(group.group_id, []))),
                "side_effect": False,
                "failure_policy": "continue",
                "display_order": order,
            })
        return tasks

    @staticmethod
    def _authorized_group_ids(intention_data, analysis) -> set[str]:
        decisions = intention_data.get("policy_decisions") or []
        if decisions:
            return {
                str(item.get("group_id")) for item in decisions
                if item.get("authorized") and item.get("group_id")
            }

        # Legacy callers authorize per intent item. Match explicit group_id
        # first, then preserve the old intent-level behavior during rollout.
        authorized_group_ids = {
            str(item.get("group_id")) for item in intention_data.get("intents", [])
            if item.get("should_call_skill") and item.get("group_id")
        }
        authorized_intents = {
            str(item.get("type")) for item in intention_data.get("intents", [])
            if item.get("should_call_skill") and item.get("type")
        }
        return authorized_group_ids | {
            group.group_id for group in analysis.groups if group.intent in authorized_intents
        }

    @classmethod
    def fallback(
        cls,
        query: str,
        intents: List[str],
        entities: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Legacy deterministic adapter for callers without canonical groups.

        任务 query 由 skill 执行模板渲染（占位符由 key_entities + 正则补全），
        单步意图直接用语义 query；工作流意图的每步 query 在 graph builder 展开时
        从模板生成，因此这里对全部 intent 一视同仁，不需要按意图分支。
        """
        merged_entities = dict(entities or {})
        destination = cls._extract_destination(query)
        date_phrase = cls._extract_date(query)
        if destination and "destination" not in merged_entities:
            merged_entities["destination"] = destination
        if date_phrase and "start_date" not in merged_entities:
            merged_entities["start_date"] = date_phrase

        tasks = []
        for order, intent in enumerate(intents):
            tasks.append({
                "task_id": intent,
                "intent": intent,
                "query": cls._task_query(intent, merged_entities, query),
                "entities": merged_entities,
                "depends_on": [],
                "side_effect": False,
                "failure_policy": "continue",
                "display_order": order,
            })
        return tasks

    @classmethod
    def _task_query(cls, intent: str, entities: Dict[str, Any], original_query: str) -> str:
        """单步意图渲染首个执行步骤的 scoped query；无模板或工作流则退回原始问题。"""
        steps = execution_steps_for_intent(intent)
        # A workflow Goal keeps the full user request because its child steps
        # need different slices. A capability may also expand to parallel
        # provider nodes (for example weather + place search); its Goal query
        # still uses the first scoped template so policy terms from sibling
        # Goals do not leak into scope validation.
        if len(steps) > 1 and intent == "itinerary_planning":
            return original_query
        if steps:
            template = steps[0].query
            if template and "{" in template:
                rendered = TaskGraphBuilder._render_query(template, entities)
                if rendered:
                    return rendered
        return original_query

    @staticmethod
    def _extract_destination(query: str) -> str:
        for pattern in _DESTINATION_PATTERNS:
            match = pattern.search(query or "")
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _extract_date(query: str) -> str:
        match = _DATE_PATTERN.search(query or "")
        return match.group(1) if match else ""
