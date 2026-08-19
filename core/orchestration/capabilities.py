"""Resolve explicit user opt-outs against workflow capability metadata."""
from __future__ import annotations

import re
from typing import Iterable, List

from core.intent_catalog import execution_steps_for_intent

from .models import CapabilitySelection, IntentTask


_NEGATION = (
    r"(?:不需要|不想要|不想看|不用|无需|不要|别查|别看|不查询|不查|"
    r"跳过|忽略|排除|去掉|取消)"
)
_OPTIONAL_ACTION = r"(?:再?查询|再?查|提供|包含|考虑|展示|显示|获取|看)?"


def _explicitly_excluded(query: str, aliases: Iterable[str]) -> bool:
    for alias in aliases:
        escaped = re.escape(alias)
        if re.search(
            _NEGATION + _OPTIONAL_ACTION + r"[^，。！？；,!?;]{0,8}" + escaped,
            query,
        ):
            return True
        if re.search(
            escaped + r"[^，。！？；,!?;]{0,6}" + _NEGATION,
            query,
        ):
            return True
    return False


def apply_capability_selection(
    tasks: Iterable[IntentTask], user_query: str,
) -> List[IntentTask]:
    """Attach only explicit opt-outs; defaults remain owned by skill metadata.

    Mandatory workflow steps do not declare capabilities and therefore cannot
    be disabled by user wording. This keeps policy retrieval and compliance
    checks mandatory for company-trip planning.
    """
    resolved = list(tasks)
    query = str(user_query or "")
    for task in resolved:
        excluded = list(task.capability_selection.exclude)
        for step in execution_steps_for_intent(task.intent):
            for capability in step.capabilities:
                aliases = capability.aliases or [capability.name]
                if _explicitly_excluded(query, aliases):
                    excluded.append(capability.name)
        task.capability_selection = CapabilitySelection(
            include=list(dict.fromkeys(task.capability_selection.include)),
            exclude=list(dict.fromkeys(excluded)),
        )
    return resolved
