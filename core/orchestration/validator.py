"""Deterministic validation boundary for LLM-produced semantic tasks.

Authorization is intent-level and declarative: the validator only trusts
intents previously authorized by the recognition layer, merges duplicate
tasks at the intent node, checks each task's query stays inside the
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
    """Accept one merged scoped task per previously authorized intent."""

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

        # 意图节点级去重：同一意图的多个任务合并，避免图构建阶段重复执行。
        merged = self._merge_by_intent(parsed)

        original_query = str(intention_data.get("rewritten_query") or "")
        recognized_entities = self._normalized_entities(
            intention_data.get("key_entities") or {}
        )
        by_intent: Dict[str, IntentTask] = {}
        for task in merged:
            if task.intent not in allowed:
                raise ValueError(f"task intent was not authorized: {task.intent}")
            task.entities = {**recognized_entities, **task.entities}
            self._restore_query_anchors(task)
            self._check_scope(task, original_query)
            if task.side_effect and not side_effect_allowed(task.intent):
                raise ValueError(f"side effect not allowed for intent: {task.intent}")
            if task.intent in task.depends_on:
                raise ValueError(f"task must not depend on itself: {task.intent}")
            by_intent[task.intent] = task

        self._check_dependencies(merged, by_intent)

        missing = set(allowed) - set(by_intent)
        if missing:
            raise ValueError(f"decomposer omitted authorized intents: {sorted(missing)}")

        return sorted(merged, key=lambda task: task.display_order)

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
    def _merge_by_intent(parsed: List[IntentTask]) -> List[IntentTask]:
        merged: Dict[str, IntentTask] = {}
        for task in parsed:
            if task.intent in merged:
                merged[task.intent] = TaskValidator._merge_tasks(merged[task.intent], task)
            else:
                merged[task.intent] = task
        return list(merged.values())

    @staticmethod
    def _merge_tasks(first: IntentTask, second: IntentTask) -> IntentTask:
        query = first.query if len(first.query) >= len(second.query) else second.query
        return IntentTask(
            task_id=first.task_id,
            group_id=first.group_id or second.group_id,
            intent=first.intent,
            query=query,
            entities={**second.entities, **first.entities},
            depends_on=list(dict.fromkeys([*first.depends_on, *second.depends_on])),
            side_effect=first.side_effect or second.side_effect,
            failure_policy=first.failure_policy,
            display_order=min(first.display_order, second.display_order),
            capability_selection={
                "include": list(dict.fromkeys([
                    *first.capability_selection.include,
                    *second.capability_selection.include,
                ])),
                "exclude": list(dict.fromkeys([
                    *first.capability_selection.exclude,
                    *second.capability_selection.exclude,
                ])),
            },
        )

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
    def _check_dependencies(
        merged: List[IntentTask],
        by_intent: Dict[str, IntentTask],
    ) -> None:
        known = set(by_intent)
        for task in merged:
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(
                    f"unknown dependency target for {task.intent}: {sorted(missing)}"
                )

        visiting: set = set()
        visited: set = set()

        def visit(intent: str) -> None:
            if intent in visiting:
                raise ValueError(f"dependency cycle detected at intent: {intent}")
            if intent in visited:
                return
            visiting.add(intent)
            for target in by_intent[intent].depends_on:
                visit(target)
            visiting.remove(intent)
            visited.add(intent)

        for intent in known:
            visit(intent)
