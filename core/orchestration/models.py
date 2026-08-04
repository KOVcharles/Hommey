"""Strict contracts shared by decomposition, execution, and presentation."""
from __future__ import annotations

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentTask(BaseModel):
    """A user-facing semantic task. It never names an executable implementation."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    intent: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=600)
    entities: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[str] = Field(default_factory=list)
    side_effect: bool = False
    failure_policy: Literal["abort", "continue"] = "continue"
    display_order: int = Field(default=0, ge=0)


class ExecutionTask(IntentTask):
    """A validated task bound to a trusted runtime agent by application code."""

    agent_name: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=1, ge=1)
    reason: str = ""
    expected_output: str = ""
    max_retries: int = Field(default=0, ge=0, le=2)


class TaskResult(BaseModel):
    """Normalized execution result, tagged with the originating semantic task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    intent: str
    agent_name: str
    status: Literal["success", "error", "skipped"]
    data: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    duration_sec: float | None = None
    attempts: int = 1
    error_code: str | None = None
    error_message: str | None = None
    display_order: int = 0


class ProgressEvent(BaseModel):
    """Backend-owned progress event rendered through frontend message keys."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["status", "task_status"] = "task_status"
    phase: Literal[
        "analyzing", "decomposing", "queued", "running", "completed",
        "failed", "composing", "done",
    ]
    message_key: str
    task_id: str | None = None
    intent: str | None = None
