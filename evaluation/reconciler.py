"""Best-effort repair for evaluation events lost before queue persistence."""
from __future__ import annotations

import asyncio
from typing import Any

from evaluation.collector import producer_versions
from evaluation.models import TurnEvaluationMetadata
from evaluation.repository import EvaluationRepository
from utils.observability import metrics


class TurnEvaluationReconciler:
    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        evaluator_version: str,
        judge_model: str = "",
        judge_prompt_version: str = "turn-judge-v1",
        rubric_version: str = "travel-rubric-v1",
    ):
        self.repository = repository
        self.evaluator_version = evaluator_version
        self.judge_model = judge_model
        self.judge_prompt_version = judge_prompt_version
        self.rubric_version = rubric_version

    async def run_once(self, *, limit: int = 100) -> int:
        rows = await asyncio.to_thread(self.repository.missing_turn_subjects, limit=limit)
        reconciled = 0
        for row in rows:
            metadata = self._metadata(row)
            await asyncio.to_thread(
                self.repository.create_subject_and_run,
                metadata,
                evaluator_version=self.evaluator_version,
                judge_model=self.judge_model,
                judge_prompt_version=self.judge_prompt_version,
                rubric_version=self.rubric_version,
            )
            reconciled += 1
            metrics.increment("evaluation_reconciled_total")
        return reconciled

    @staticmethod
    def _metadata(row: dict[str, Any]) -> TurnEvaluationMetadata:
        answer_document = row.get("answer_document")
        presentation_document = row.get("presentation_document")
        sources = answer_document.get("sources") if isinstance(answer_document, dict) else []
        waiting = isinstance(presentation_document, dict)
        return TurnEvaluationMetadata.model_validate({
            "schema_version": "eval.turn.input.1",
            "subject": {
                "request_id": str(row["request_id"]),
                "session_id": str(row["session_id"]),
                "run_id": "",
                "turn_id": str(row.get("turn_id") or ""),
                "occurred_at": row["occurred_at"],
            },
            "conversation": {
                "user_message": str(row.get("user_message") or ""),
                "assistant_message": str(row.get("assistant_message") or ""),
                "previous_messages": [],
            },
            "routing": {"intents": [], "selected_skills": []},
            "execution": {
                "terminal_state": "waiting_user" if waiting else "completed",
                "paused": waiting,
                "interrupted": False,
                "tasks": [],
                "agent_results": [],
                "timings": {},
                "error_codes": [],
            },
            "answer": {
                "answer_document": answer_document if isinstance(answer_document, dict) else None,
                "presentation_document": presentation_document if waiting else None,
                "sources": sources if isinstance(sources, list) else [],
            },
            "evidence": {"retrieval_trace_ids": [], "items": []},
            "versions": producer_versions().model_dump(mode="json"),
            "capture_mode": "reconciled",
            "evaluation_context_quality": "reduced",
            "metadata_truncated": False,
            "truncated_fields": [],
        })
