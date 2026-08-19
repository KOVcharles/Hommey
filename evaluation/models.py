"""Versioned contracts for turn evaluation capture and results."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


TURN_INPUT_SCHEMA_VERSION = "eval.turn.input.1"
TURN_RESULT_SCHEMA_VERSION = "eval.turn.result.1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvaluationSubject(StrictModel):
    request_id: str
    session_id: str
    run_id: str = ""
    turn_id: str = ""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConversationSnapshot(StrictModel):
    user_message: str
    assistant_message: str = ""
    previous_messages: List[Dict[str, str]] = Field(default_factory=list)


class RoutingSnapshot(StrictModel):
    intents: List[str] = Field(default_factory=list)
    selected_skills: List[str] = Field(default_factory=list)


class ExecutionSnapshot(StrictModel):
    terminal_state: Literal[
        "completed", "waiting_user", "degraded", "failed", "interrupted",
        "idempotent_replay",
    ] = "completed"
    paused: bool = False
    interrupted: bool = False
    tasks: List[Dict[str, Any]] = Field(default_factory=list)
    agent_results: List[Dict[str, Any]] = Field(default_factory=list)
    timings: Dict[str, float] = Field(default_factory=dict)
    error_codes: List[str] = Field(default_factory=list)


class AnswerSnapshot(StrictModel):
    answer_document: Optional[Dict[str, Any]] = None
    presentation_document: Optional[Dict[str, Any]] = None
    sources: List[Dict[str, Any]] = Field(default_factory=list)


class EvidenceSnapshot(StrictModel):
    retrieval_trace_ids: List[str] = Field(default_factory=list)
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ProducerVersions(StrictModel):
    git_revision: str = ""
    production_model: str = ""
    intent_prompt_version: str = ""
    composer_prompt_version: str = ""
    skill_versions: Dict[str, str] = Field(default_factory=dict)
    rag_index_version: str = ""


class TurnEvaluationMetadata(StrictModel):
    """Immutable, bounded snapshot handed to the evaluation subsystem."""

    schema_version: Literal["eval.turn.input.1"] = TURN_INPUT_SCHEMA_VERSION
    subject: EvaluationSubject
    conversation: ConversationSnapshot
    routing: RoutingSnapshot = Field(default_factory=RoutingSnapshot)
    execution: ExecutionSnapshot = Field(default_factory=ExecutionSnapshot)
    answer: AnswerSnapshot = Field(default_factory=AnswerSnapshot)
    evidence: EvidenceSnapshot = Field(default_factory=EvidenceSnapshot)
    versions: ProducerVersions = Field(default_factory=ProducerVersions)
    capture_mode: Literal["live", "reconciled", "offline"] = "live"
    evaluation_context_quality: Literal["full", "reduced"] = "full"
    metadata_truncated: bool = False
    truncated_fields: List[str] = Field(default_factory=list)


class TurnEvaluationFacts(StrictModel):
    primary_intent: str = ""
    selected_skills: List[str] = Field(default_factory=list)
    successful_agents: List[str] = Field(default_factory=list)
    failed_agents: List[str] = Field(default_factory=list)
    terminal_state: str
    answer_kind: Literal["answer_document", "presentation_document", "text", "none"]
    required_fields: List[str] = Field(default_factory=list)
    present_fields: List[str] = Field(default_factory=list)
    missing_fields: List[str] = Field(default_factory=list)
    source_count: int = 0
    evidence_count: int = 0
    error_codes: List[str] = Field(default_factory=list)
    total_latency: Optional[float] = None
    metadata_completeness: float = Field(ge=0.0, le=1.0)


class RuleResult(StrictModel):
    code: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    message: str
    subject_ref: str = ""
    hard: bool = False


class EvaluationFinding(StrictModel):
    code: str
    severity: Literal["low", "medium", "high", "critical"]
    message: str
    subject_ref: str = ""
    evidence_refs: List[str] = Field(default_factory=list)


class JudgeResult(StrictModel):
    schema_version: Literal["eval.turn.result.1"] = TURN_RESULT_SCHEMA_VERSION
    verdict: Literal["pass", "warning", "fail", "critical_fail", "unscored"]
    score: Optional[int] = Field(default=None, ge=0, le=100)
    dimensions: Dict[
        Literal["understanding", "task_progress", "groundedness", "safety", "clarity"],
        int,
    ]
    reason_codes: List[str] = Field(default_factory=list)
    critical_errors: List[str] = Field(default_factory=list)
    findings: List[EvaluationFinding] = Field(default_factory=list)
    review_required: bool = False
    summary: str

    @model_validator(mode="after")
    def validate_dimensions(self):
        expected = {"understanding", "task_progress", "groundedness", "safety", "clarity"}
        if set(self.dimensions) != expected:
            raise ValueError("dimensions must contain exactly the five rubric dimensions")
        if any(score < 0 or score > 4 for score in self.dimensions.values()):
            raise ValueError("dimension scores must be between 0 and 4")
        return self


class FinalEvaluationResult(StrictModel):
    verdict: Literal["pass", "warning", "fail", "critical_fail", "unscored"]
    score: Optional[int] = Field(default=None, ge=0, le=100)
    dimension_scores: Dict[str, int] = Field(default_factory=dict)
    reason_codes: List[str] = Field(default_factory=list)
    critical_errors: List[str] = Field(default_factory=list)
    rule_results: List[RuleResult] = Field(default_factory=list)
    explanation: str
    review_required: bool = False
