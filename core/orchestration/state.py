"""Durable orchestration state shared by single- and multi-intent runs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


RunStatus = Literal[
    "ACTIVE", "WAITING_USER", "INTERRUPTING", "INTERRUPTED",
    "COMPLETED", "FAILED", "ABANDONED", "EXPIRED",
]
TurnStatus = Literal["IN_PROGRESS", "INTERRUPTING", "INTERRUPTED", "COMPLETED", "FAILED"]
GoalStatus = Literal[
    "PENDING", "RUNNING", "WAITING_USER", "INTERRUPTED",
    "SUCCEEDED", "FAILED", "ABANDONED",
]
NodeStatus = Literal[
    "READY", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED", "WAITING_USER",
    "BLOCKED", "SKIPPED",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_expiry(days: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


class GoalState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str
    intent: str
    status: GoalStatus = "PENDING"
    query: str = ""
    expected_inputs: List[str] = Field(default_factory=list)
    answer_delivered: bool = False


class NodeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    goal_id: str
    status: NodeStatus = "READY"
    operation_id: str
    attempts: int = 0
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None


class WaitState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str
    reason: str = "missing_input"
    expected_fields: List[str] = Field(default_factory=list)
    pause_agent: str = ""


class WorkflowRunState(BaseModel):
    """Canonical JSONB snapshot; executable bindings are deliberately absent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    run_id: str
    user_id: str
    session_id: str
    revision: int = 0
    status: RunStatus = "ACTIVE"
    current_turn_id: Optional[str] = None
    current_request_id: str = ""
    current_goal_ids: List[str] = Field(default_factory=list)
    focused_goal_id: Optional[str] = None
    original_query: str
    intention_data: Dict[str, Any] = Field(default_factory=dict)
    semantic_tasks: List[Dict[str, Any]] = Field(default_factory=list)
    goals: Dict[str, GoalState] = Field(default_factory=dict)
    nodes: Dict[str, NodeState] = Field(default_factory=dict)
    waits: List[WaitState] = Field(default_factory=list)
    graph_hash: str = ""
    skill_versions: Dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    expires_at: str = Field(default_factory=default_expiry)

    @property
    def resumable(self) -> bool:
        return self.status in {"WAITING_USER", "INTERRUPTING", "INTERRUPTED"}

    @property
    def has_consistent_waiting_state(self) -> bool:
        """A waiting Run is valid only when Goal and WaitState agree."""
        if self.status != "WAITING_USER":
            return True
        waiting_goals = {
            goal.goal_id for goal in self.goals.values()
            if goal.status == "WAITING_USER"
        }
        wait_goals = {wait.goal_id for wait in self.waits}
        return bool(waiting_goals) and waiting_goals <= wait_goals


class WorkflowTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str
    run_id: str
    request_id: str
    status: TurnStatus = "IN_PROGRESS"
    input: str = ""
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


def derive_goal_status(nodes: List[NodeState]) -> GoalStatus:
    """Derive one Goal status from its Nodes using the canonical state rules.

    Agents never choose this value.  In particular, a dependency-caused skip
    is a failed Goal, while an ordinary terminal skip can still be successful.
    """
    if not nodes:
        return "PENDING"
    if any(
        node.status == "FAILED"
        or (
            node.status == "SKIPPED"
            and node.error_code == "UPSTREAM_DEPENDENCY_FAILED"
        )
        for node in nodes
    ):
        return "FAILED"
    if any(node.status == "WAITING_USER" for node in nodes):
        return "WAITING_USER"
    if any(node.status == "INTERRUPTED" for node in nodes):
        return "INTERRUPTED"
    if all(node.status in {"SUCCEEDED", "SKIPPED"} for node in nodes):
        return "SUCCEEDED"
    if any(node.status in {"READY", "RUNNING", "BLOCKED"} for node in nodes):
        return "RUNNING"
    return "PENDING"
