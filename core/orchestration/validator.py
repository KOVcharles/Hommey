"""Deterministic validation boundary for LLM-produced semantic tasks."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from pydantic import ValidationError

from .models import IntentTask


PHASE_ONE_INTENTS = frozenset({"rag_knowledge", "information_query"})
_WEATHER_TERMS = ("天气", "气温", "下雨", "降水", "预报")
_POLICY_TERMS = ("制度", "标准", "补贴", "报销", "审批", "公司规定")
_POLICY_CATEGORIES = ("住宿", "交通", "补贴", "报销", "审批", "餐饮", "餐费")


def callable_intents(intention_data: Dict[str, Any]) -> List[str]:
    return [
        str(item.get("type"))
        for item in intention_data.get("intents", [])
        if item.get("type") and item.get("should_call_skill")
    ]


def supports_phase_one(intention_data: Dict[str, Any]) -> bool:
    return set(callable_intents(intention_data)) == PHASE_ONE_INTENTS


class TaskValidator:
    """Accept only one scoped task for each previously authorized intent."""

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

        by_intent: Dict[str, IntentTask] = {}
        original_query = str(intention_data.get("rewritten_query") or "")
        for task in parsed:
            if task.intent not in allowed:
                raise ValueError(f"task intent was not authorized: {task.intent}")
            if task.intent in by_intent:
                raise ValueError(f"phase one allows one task per intent: {task.intent}")
            if task.depends_on:
                raise ValueError("phase-one policy and information tasks must be independent")
            if task.side_effect:
                raise ValueError("phase-one tasks must be read-only")
            if task.intent == "rag_knowledge" and any(term in task.query for term in _WEATHER_TERMS):
                raise ValueError("policy task crossed into weather scope")
            if task.intent == "rag_knowledge":
                introduced = [
                    term for term in _POLICY_CATEGORIES
                    if term in task.query and term not in original_query
                ]
                if introduced:
                    raise ValueError(f"policy task expanded the user's scope: {introduced}")
            if task.intent == "information_query" and any(term in task.query for term in _POLICY_TERMS):
                raise ValueError("information task crossed into policy scope")
            by_intent[task.intent] = task

        missing = set(allowed) - set(by_intent)
        if missing:
            raise ValueError(f"decomposer omitted authorized intents: {sorted(missing)}")

        return sorted(parsed, key=lambda task: task.display_order)
