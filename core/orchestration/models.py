"""Strict contracts shared by decomposition, execution, and presentation."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CapabilitySelection(BaseModel):
    """Per-request overrides for declarative workflow capabilities."""

    model_config = ConfigDict(extra="forbid")

    include: List[str] = Field(default_factory=list)
    exclude: List[str] = Field(default_factory=list)


class IntentTask(BaseModel):
    """A user-facing semantic task. It never names an executable implementation."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    group_id: str = Field(default="", max_length=80)
    intent: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=600)
    entities: Dict[str, Any] = Field(default_factory=dict)
    depends_on: List[str] = Field(default_factory=list)
    side_effect: bool = False
    failure_policy: Literal["abort", "continue"] = "continue"
    display_order: int = Field(default=0, ge=0)
    capability_selection: CapabilitySelection = Field(default_factory=CapabilitySelection)


class ExecutionTask(IntentTask):
    """A validated task bound to a trusted runtime agent by application code.

    ``task_id`` becomes step-scoped (``f"{intent}-{agent_name}"``) after the
    graph builder expands a semantic task into its full skill execution
    template, so one IntentTask can produce multiple ExecutionTasks.
    """

    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,159}$")
    agent_name: str = Field(min_length=1, max_length=64)
    goal_id: str = Field(default="", max_length=64)
    priority: int = Field(default=1, ge=1)
    reason: str = ""
    expected_output: str = ""
    max_retries: int = Field(default=0, ge=0, le=2)
    # 步骤级结果判定规则（来自 skill 声明），取代按 intent 硬编码的特殊状态分支。
    result_rules: Dict[str, Any] = Field(default_factory=dict)
    # The exact capability facets selected for this node. Empty means the
    # execution step is mandatory or does not expose user-controllable facets.
    capabilities: List[str] = Field(default_factory=list)


class TaskResult(BaseModel):
    """Normalized execution result, tagged with the originating semantic task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    goal_id: str = ""
    intent: str
    agent_name: str
    status: Literal["success", "error", "skipped"]
    data: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    duration_sec: float | None = None
    attempts: int = 1
    error_code: str | None = None
    error_message: str | None = None
    operation_id: str | None = None
    display_order: int = 0


class PauseInfo(BaseModel):
    """跨轮"收集→暂停"现场：计划中断时保存的步骤剩余与已收集事实。"""

    model_config = ConfigDict(extra="forbid")

    intent: str
    goal_id: str = ""
    node_id: str = ""
    skill: str
    pause_agent: str
    pause_field: str = "planning_ready"
    planning_ready: bool = False
    steps_remaining: List[Dict[str, Any]] = Field(default_factory=list)
    collected_facts: Dict[str, Any] = Field(default_factory=dict)
    entities: Dict[str, Any] = Field(default_factory=dict)


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


class PipelineOutput(BaseModel):
    """Unified pipeline result: answer doc XOR pause presentation."""

    model_config = ConfigDict(extra="ignore")

    tasks: List[IntentTask] = Field(default_factory=list)
    execution_tasks: List[ExecutionTask] = Field(default_factory=list)
    results: List[TaskResult] = Field(default_factory=list)
    answer_document: Optional[Any] = None
    paused: bool = False
    interrupted: bool = False
    pause_info: Optional[PauseInfo] = None
    pause_infos: List[PauseInfo] = Field(default_factory=list)
    presentation_document: Optional[Any] = None
