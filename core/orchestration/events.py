"""Stable progress-event vocabulary for orchestration clients."""
from __future__ import annotations

from .models import ProgressEvent


RUNNING_MESSAGE_KEYS = {
    "rag_knowledge": "policy_searching",
    "information_query": "travel_info_searching",
    "memory_query": "memory_searching",
    "preference": "preference_updating",
    "event_collection": "trip_details_collecting",
    "itinerary_planning": "trip_planning",
    "trip_compliance": "compliance_checking",
}


def phase_event(phase: str, message_key: str) -> ProgressEvent:
    return ProgressEvent(type="status", phase=phase, message_key=message_key)


def task_event(phase: str, task_id: str, intent: str) -> ProgressEvent:
    suffix = {
        "queued": "queued",
        "running": RUNNING_MESSAGE_KEYS.get(intent, "task_running"),
        "completed": "task_completed",
        "failed": "task_failed",
    }[phase]
    return ProgressEvent(
        type="task_status",
        phase=phase,
        message_key=suffix,
        task_id=task_id,
        intent=intent,
    )
