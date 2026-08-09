"""Declarative memory write-back: which agent's result triggers which effect.

The legacy ``_update_memory`` had three hardcoded agent branches. This module
replaces them with the skill's ``memory_hooks`` declarations — adding a new
memory side effect is a yaml change, not a Python branch. Idempotency relies on
the memory manager's ``current_request_id`` (a turn may re-apply the same hooks
without duplicating trips or preferences).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

from core.intent_catalog import memory_hooks_for_intent
from utils.memory_safety import filter_safe_memory_mapping, is_safe_preference_value

from .models import TaskResult

logger = logging.getLogger(__name__)


def _nested(data: Dict[str, Any]) -> Dict[str, Any]:
    return data.get("data") if isinstance(data.get("data"), dict) else data


class MemoryHookExecutor:
    """Apply declared memory effects against a memory_manager-compatible object."""

    def __init__(self, memory_manager):
        self.memory_manager = memory_manager
        self._effects = {
            "update_active_trip": self._update_active_trip,
            "save_preference": self._save_preference,
            "complete_trip": self._complete_trip,
        }

    async def apply(self, results: Iterable[TaskResult]) -> None:
        if self.memory_manager is None:
            return
        for result in results:
            if result.status != "success":
                continue
            hooks = memory_hooks_for_intent(result.intent)
            for hook in hooks:
                if hook.agent != result.agent_name:
                    continue
                effect = self._effects.get(hook.effect)
                if effect is None:
                    logger.warning("unknown memory hook effect: %s", hook.effect)
                    continue
                try:
                    effect(result, hook)
                except Exception as exc:  # 记忆回写失败不阻断主流程
                    logger.warning("memory hook %s failed for %s: %s",
                                   hook.effect, result.agent_name, exc)

    # ---- effects ----------------------------------------------------------

    def _update_active_trip(self, result: TaskResult, hook) -> None:
        data = _nested(result.data)
        event_data = filter_safe_memory_mapping(data)
        if not any(event_data.get(key) for key in (
            "origin", "destination", "start_date", "end_date", "work_location",
        )):
            return
        self.memory_manager.update_active_trip(event_data)

    def _save_preference(self, result: TaskResult, hook) -> None:
        data = _nested(result.data)
        preferences = data.get("preferences")
        if isinstance(preferences, dict):
            preferences = [
                {"type": key, "value": value, "action": "replace"}
                for key, value in preferences.items()
                if value and key not in ("has_preferences", "error")
            ]
        for pref in preferences or []:
            if not isinstance(pref, dict):
                continue
            pref_type = pref.get("type")
            pref_value = pref.get("value")
            if not pref_type or not pref_value:
                continue
            if not is_safe_preference_value(pref_value):
                logger.warning("skipped sensitive preference value for %s", pref_type)
                continue
            action = pref.get("action", "replace")
            if action == "append":
                self._append_preference(pref_type, pref_value)
            else:
                self.memory_manager.long_term.save_preference(pref_type, pref_value)

    def _append_preference(self, pref_type: str, pref_value: Any) -> None:
        existing = self.memory_manager.long_term.get_preference(pref_type)
        if isinstance(existing, list):
            if pref_value not in existing:
                existing.append(pref_value)
            self.memory_manager.long_term.save_preference(pref_type, existing)
        else:
            new_list = [existing, pref_value] if existing else [pref_value]
            self.memory_manager.long_term.save_preference(pref_type, new_list)

    def _complete_trip(self, result: TaskResult, hook) -> None:
        data = _nested(result.data)
        if hook.require_field and not data.get(hook.require_field):
            return
        itinerary = data.get("itinerary")
        if not itinerary and hook.require_field == "itinerary":
            return
        # 行程事实以 event_collection 收集的 active trip 为准（事实源）。
        active = self.memory_manager.get_active_trip()
        if not active:
            return
        self.memory_manager.long_term.save_trip_history({
            "origin": active.get("origin"),
            "destination": active.get("destination"),
            "start_date": active.get("start_date"),
            "end_date": active.get("end_date"),
            "purpose": active.get("trip_purpose", "公司出差"),
            # Stable per-node key: a stopped Turn may resume under a new request_id.
            "request_id": result.operation_id or getattr(
                self.memory_manager, "current_request_id", None
            ),
        })
        self.memory_manager.complete_active_trip(reason="planning_completed")
