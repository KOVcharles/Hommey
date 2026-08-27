"""Deterministic validation boundary for LLM-produced semantic tasks.

Authorization is group-level and declarative: the validator only trusts
groups previously authorized by OrchestrationPolicy, preserves multiple Goals
with the same intent, and checks each task's query stays inside the
skill-declared scope, requires every side effect to be declared, and verifies
the dependency graph is acyclic. Agent binding happens later in the graph
builder, so the LLM can never name an executable.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from pydantic import ValidationError

from core.intent_catalog import (
    _definition_for_intent,
    is_skill_intent,
    side_effect_allowed,
)

from .models import IntentTask


def callable_intents(intention_data: Dict[str, Any]) -> List[str]:
    decisions = intention_data.get("policy_decisions") or []
    if decisions:
        return [
            str(item.get("intent"))
            for item in decisions
            if item.get("intent") and item.get("authorized")
        ]
    return [
        str(item.get("type"))
        for item in intention_data.get("intents", [])
        if item.get("type") and item.get("should_call_skill")
    ]


def supports_task_pipeline(intention_data: Dict[str, Any]) -> bool:
    """Any non-empty set of skill-backed intents is pipeline-eligible."""
    intents = callable_intents(intention_data)
    return bool(intents) and all(is_skill_intent(intent) for intent in intents)


class TaskValidator:
    """Accept one scoped task per previously authorized semantic group."""

    def validate(
        self,
        raw_tasks: Iterable[Dict[str, Any]],
        intention_data: Dict[str, Any],
    ) -> List[IntentTask]:
        allowed = callable_intents(intention_data)
        if not allowed:
            raise ValueError("no callable intents were authorized")

        parsed: List[IntentTask] = []
        try:
            for raw in raw_tasks:
                parsed.append(IntentTask.model_validate(raw))
        except (ValidationError, TypeError) as exc:
            raise ValueError(f"invalid semantic task: {exc}") from exc

        ids = [task.task_id for task in parsed]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id values must be unique")

        original_query = str(
            intention_data.get("original_query")
            or intention_data.get("rewritten_query")
            or ""
        )
        recognized_entities = self._normalized_entities(
            intention_data.get("key_entities") or {}
        )
        decision_by_group = {
            str(item.get("group_id")): item
            for item in intention_data.get("policy_decisions") or []
            if item.get("authorized") and item.get("group_id")
        }
        for task in parsed:
            if decision_by_group:
                decision = decision_by_group.get(task.task_id)
                if decision is None:
                    raise ValueError(f"task group was not authorized: {task.task_id}")
                if decision.get("intent") != task.intent:
                    raise ValueError(
                        f"task intent does not match policy decision: {task.task_id}"
                    )
            elif task.intent not in allowed:
                raise ValueError(f"task intent was not authorized: {task.intent}")
            task.entities = {**recognized_entities, **task.entities}
            self._restore_query_anchors(task)
            self._check_scope(task, original_query)
            if task.side_effect and not side_effect_allowed(task.intent):
                raise ValueError(f"side effect not allowed for intent: {task.intent}")
            if task.task_id in task.depends_on or task.intent in task.depends_on:
                raise ValueError(f"task must not depend on itself: {task.task_id}")
        self._check_dependencies(parsed)

        if decision_by_group:
            missing = set(decision_by_group) - {task.task_id for task in parsed}
            if missing:
                raise ValueError(f"intent adapter omitted authorized groups: {sorted(missing)}")
        else:
            missing = set(allowed) - {task.intent for task in parsed}
            if missing:
                raise ValueError(f"intent adapter omitted authorized intents: {sorted(missing)}")

        return sorted(parsed, key=lambda task: task.display_order)

    @staticmethod
    def _normalized_entities(entities: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {
            key: value for key, value in entities.items()
            if value is not None and value != "" and value != []
        }
        aliases = {
            "date": "start_date",
            "trip_length": "duration",
            "duration_days": "duration",
            "trip_purpose": "purpose",
        }
        for source, target in aliases.items():
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
        return normalized

    @staticmethod
    def _restore_query_anchors(task: IntentTask) -> None:
        """Restore user-provided entity anchors dropped by decomposition."""
        definition = _definition_for_intent(task.intent)
        scope = getattr(definition, "scope", None) if definition else None
        fields = list(getattr(scope, "query_anchor_fields", []) or [])
        anchors = []
        for field in fields:
            value = task.entities.get(field)
            if value is None or value == "" or str(value) in task.query:
                continue
            # Natural-language anchors preserve both vector semantics and BM25
            # token matching; field-label syntax can degrade matching against
            # corpus text that contains only the location value.
            anchors.append(str(value))
        if anchors:
            task.query = f"{' '.join(anchors)} {task.query}"

    @staticmethod
    def _check_scope(task: IntentTask, original_query: str) -> None:
        """查询不得引入该意图 skill 声明的禁区词或用户未提及的扩展词域。"""
        definition = _definition_for_intent(task.intent)
        scope = getattr(definition, "scope", None) if definition else None
        if scope is None:
            return
        for term in scope.forbidden_terms:
            if term in task.query:
                raise ValueError(f"{task.intent} task crossed into forbidden scope: {term}")
        introduced = [
            term for term in scope.expansion_terms
            if term in task.query and term not in original_query
        ]
        if introduced:
            raise ValueError(f"{task.intent} task expanded the user's scope: {introduced}")

    @staticmethod
    def _check_dependencies(tasks: List[IntentTask]) -> None:
        known_ids = {task.task_id for task in tasks}
        intent_counts: Dict[str, int] = {}
        for task in tasks:
            intent_counts[task.intent] = intent_counts.get(task.intent, 0) + 1
        alias_to_id = {
            task.intent: task.task_id
            for task in tasks
            if intent_counts[task.intent] == 1
        }
        known = known_ids | set(alias_to_id)
        for task in tasks:
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(
                    f"unknown dependency target for {task.intent}: {sorted(missing)}"
                )

        visiting: set = set()
        visited: set = set()

        by_id = {task.task_id: task for task in tasks}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"dependency cycle detected at task: {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for target in by_id[task_id].depends_on:
                visit(alias_to_id.get(target, target))
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in by_id:
            visit(task_id)
