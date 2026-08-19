"""Deterministic normalization of captured metadata into evaluation facts."""
from __future__ import annotations

from evaluation.models import TurnEvaluationFacts, TurnEvaluationMetadata


TRIP_FIELDS = ("origin", "destination", "start_date", "end_date", "purpose", "work_location")


def extract_facts(metadata: TurnEvaluationMetadata) -> TurnEvaluationFacts:
    results = metadata.execution.agent_results
    successful = [str(item.get("agent_name")) for item in results if item.get("status") == "success"]
    failed = [str(item.get("agent_name")) for item in results if item.get("status") == "error"]
    missing = _unique(
        field
        for item in results
        for field in (item.get("missing_info") or [])
        if field
    )
    present = _unique(
        field
        for item in results
        for field in (item.get("present_fields") or [])
        if field
    )
    answer_kind = "text"
    if metadata.answer.answer_document is not None:
        answer_kind = "answer_document"
    elif metadata.answer.presentation_document is not None:
        answer_kind = "presentation_document"
    elif not metadata.conversation.assistant_message:
        answer_kind = "none"

    required_fields = list(TRIP_FIELDS) if "plan-trip" in metadata.routing.selected_skills else []
    completeness_checks = (
        bool(metadata.conversation.user_message),
        bool(metadata.conversation.assistant_message) or metadata.execution.interrupted,
        bool(metadata.routing.intents),
        bool(metadata.versions.production_model),
        not metadata.metadata_truncated and metadata.evaluation_context_quality == "full",
    )
    latency = metadata.execution.timings.get("total")
    return TurnEvaluationFacts(
        primary_intent=metadata.routing.intents[0] if metadata.routing.intents else "",
        selected_skills=metadata.routing.selected_skills,
        successful_agents=_unique(successful),
        failed_agents=_unique(failed),
        terminal_state=metadata.execution.terminal_state,
        answer_kind=answer_kind,
        required_fields=required_fields,
        present_fields=present,
        missing_fields=missing,
        source_count=len(metadata.answer.sources),
        evidence_count=len(metadata.evidence.items),
        error_codes=metadata.execution.error_codes,
        total_latency=latency,
        metadata_completeness=sum(completeness_checks) / len(completeness_checks),
    )


def _unique(values) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value))
