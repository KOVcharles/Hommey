"""Build executable agent schedules from callable intents.

Legacy vestigial field: the DAG pipeline compiles execution from skill
templates directly, so ``agent_schedule`` is only kept for contract
compatibility. It is a pure flatten of each callable intent's template steps
(no dedup, no workflow special cases — those semantics live in the graph
builder's subsumption).
"""
from __future__ import annotations

from typing import Any, Dict, List

from utils.skill_loader import SkillLoader


def _load_schedule_rules() -> Dict[str, List[Dict[str, Any]]]:
    rules: Dict[str, List[Dict[str, Any]]] = {}
    for definition in SkillLoader().load_definitions().values():
        if not definition.intent:
            continue
        rules[definition.intent] = [step.model_dump() for step in definition.execution]
    return rules


SCHEDULE_RULES = _load_schedule_rules()


def build_agent_schedule(intents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten callable intents into a priority-sorted agent schedule."""
    schedule = []
    for intent in intents:
        intent_type = intent.get("type")
        if not intent_type:
            continue
        for item in SCHEDULE_RULES.get(intent_type, []):
            runtime_item = dict(item)
            runtime_item.pop("skill", None)
            schedule.append(runtime_item)
    return sorted(schedule, key=lambda item: item.get("priority", 999))
