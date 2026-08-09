"""Stable progress-event vocabulary for orchestration clients."""
from __future__ import annotations

from core.intent_catalog import progress_key_for_intent

from .models import ProgressEvent


def phase_event(phase: str, message_key: str) -> ProgressEvent:
    return ProgressEvent(type="status", phase=phase, message_key=message_key)


def task_event(phase: str, task_id: str, intent: str) -> ProgressEvent:
    suffix = {
        "queued": "queued",
        "running": progress_key_for_intent(intent),
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
