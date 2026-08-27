"""Request-scoped, I/O-free construction of bounded evaluation snapshots."""
from __future__ import annotations

import os
import uuid
from contextvars import ContextVar
from functools import lru_cache
from typing import Any, Iterable

from core.intent_catalog import intent_to_skill
from evaluation.models import (
    AnswerSnapshot,
    ConversationSnapshot,
    EvaluationSubject,
    ExecutionSnapshot,
    EvidenceSnapshot,
    ProducerVersions,
    RoutingSnapshot,
    TurnEvaluationMetadata,
)
from settings import LLM_CONFIG
from utils.memory_safety import redact_sensitive_text
from utils.skill_loader import SkillLoader


_current_collector: ContextVar["TurnEvaluationCollector | None"] = ContextVar(
    "turn_evaluation_collector", default=None,
)


def current_collector() -> "TurnEvaluationCollector | None":
    return _current_collector.get()


def set_current_collector(collector: "TurnEvaluationCollector"):
    return _current_collector.set(collector)


def reset_current_collector(token) -> None:
    _current_collector.reset(token)


@lru_cache(maxsize=1)
def producer_versions() -> ProducerVersions:
    definitions = SkillLoader().load_definitions(strict=False)
    return ProducerVersions(
        git_revision=os.getenv("HOMMEY_GIT_REVISION", ""),
        production_model=str(LLM_CONFIG.get("model_name") or ""),
        intent_prompt_version=os.getenv("HOMMEY_INTENT_PROMPT_VERSION", ""),
        composer_prompt_version=os.getenv("HOMMEY_COMPOSER_PROMPT_VERSION", ""),
        skill_versions={name: item.version for name, item in definitions.items()},
        rag_index_version=os.getenv("HOMMEY_RAG_INDEX_VERSION", ""),
    )


class TurnEvaluationCollector:
    """Collect structured facts without performing storage or model calls."""

    MAX_MESSAGE_CHARS = 12_000
    MAX_CONTEXT_MESSAGES = 6
    MAX_CONTEXT_MESSAGE_CHARS = 4_000
    MAX_TASKS = 30
    MAX_RESULTS = 30
    MAX_EVIDENCE = 20
    MAX_EVIDENCE_EXCERPT_CHARS = 1_200

    def __init__(self, *, request_id: str | None, session_id: str, user_message: str):
        self.request_id = request_id or uuid.uuid4().hex
        self.session_id = session_id
        self.user_message = user_message
        self.previous_messages: list[dict[str, str]] = []
        self.intents: list[str] = []
        self.selected_skills: list[str] = []
        self.tasks: list[dict[str, Any]] = []
        self.agent_results: list[dict[str, Any]] = []
        self.evidence_items: list[dict[str, Any]] = []
        self.trace_ids: list[str] = []
        self.run_id = ""
        self.turn_id = ""
        self.truncated_fields: set[str] = set()

    def record_context(self, messages: Iterable[Any]) -> None:
        snapshots: list[dict[str, str]] = []
        for item in list(messages)[-self.MAX_CONTEXT_MESSAGES:]:
            role = str(item.get("role", "") if isinstance(item, dict) else getattr(item, "role", ""))
            content = item.get("content", "") if isinstance(item, dict) else getattr(item, "content", "")
            if role not in {"user", "assistant"}:
                continue
            snapshots.append({
                "role": role,
                "content": self._text(content, self.MAX_CONTEXT_MESSAGE_CHARS, "conversation.previous_messages"),
            })
        self.previous_messages = snapshots

    def record_routing(self, intention_data: dict[str, Any]) -> None:
        groups = intention_data.get("groups") or []
        intents = [
            str(item.get("intent")) for item in groups
            if isinstance(item, dict) and item.get("intent")
        ]
        raw_intents = intention_data.get("intents") or []
        if not intents:
            intents = [
                str(item.get("type")) for item in raw_intents
                if isinstance(item, dict) and item.get("type")
            ]
        if not intents:
            primary = (intention_data.get("routing") or {}).get("intent")
            intents = [str(primary)] if primary else []
        self.intents = list(dict.fromkeys(intents))
        decisions = intention_data.get("policy_decisions") or []
        callable_intents = [
            str(item.get("intent")) for item in decisions
            if isinstance(item, dict) and item.get("intent") and item.get("authorized")
        ]
        if not decisions:
            callable_intents = [
                str(item.get("type")) for item in raw_intents
                if isinstance(item, dict) and item.get("type")
                and item.get("should_call_skill") is not False
            ]
        if not callable_intents and (intention_data.get("routing") or {}).get("should_call_skill"):
            callable_intents = intents
        self.selected_skills = list(dict.fromkeys(
            skill for intent in callable_intents if (skill := intent_to_skill(intent))
        ))

    def record_pipeline(self, output: Any) -> None:
        tasks = list(getattr(output, "execution_tasks", None) or getattr(output, "tasks", None) or [])
        results = list(getattr(output, "results", None) or [])
        if len(tasks) > self.MAX_TASKS:
            self.truncated_fields.add("execution.tasks")
        if len(results) > self.MAX_RESULTS:
            self.truncated_fields.add("execution.agent_results")
        self.tasks = [self._dump(item) for item in tasks[:self.MAX_TASKS]]
        self.agent_results = [self._result_summary(item) for item in results[:self.MAX_RESULTS]]
        for result in results:
            for evidence in list(getattr(result, "evidence", None) or []) + self._result_sources(result):
                self._add_evidence(evidence)
        state = getattr(output, "state", None)
        self.run_id = str(getattr(state, "run_id", "") or "")
        self.turn_id = str(getattr(state, "current_turn_id", "") or "")

    def freeze(self, result: dict[str, Any], *, session_id: str) -> TurnEvaluationMetadata:
        terminal_state = self._terminal_state(result)
        error_codes = [
            str(item.get("error_code")) for item in self.agent_results
            if item.get("error_code")
        ]
        answer_document = result.get("answer_document")
        presentation_document = result.get("presentation_document")
        sources = self._answer_sources(answer_document)
        for source in sources:
            self._add_evidence(source)
        timings = {
            str(key): float(value) for key, value in (result.get("timings") or {}).items()
            if isinstance(value, (int, float))
        }
        return TurnEvaluationMetadata(
            subject=EvaluationSubject(
                request_id=self.request_id,
                session_id=session_id,
                run_id=self.run_id,
                turn_id=self.turn_id,
            ),
            conversation=ConversationSnapshot(
                user_message=self._text(self.user_message, self.MAX_MESSAGE_CHARS, "conversation.user_message"),
                assistant_message=self._text(result.get("response", ""), self.MAX_MESSAGE_CHARS, "conversation.assistant_message"),
                previous_messages=self.previous_messages,
            ),
            routing=RoutingSnapshot(intents=self.intents, selected_skills=self.selected_skills),
            execution=ExecutionSnapshot(
                terminal_state=terminal_state,
                paused=terminal_state == "waiting_user",
                interrupted=terminal_state == "interrupted",
                tasks=self.tasks,
                agent_results=self.agent_results,
                timings=timings,
                error_codes=list(dict.fromkeys(error_codes)),
            ),
            answer=AnswerSnapshot(
                answer_document=answer_document if isinstance(answer_document, dict) else None,
                presentation_document=presentation_document if isinstance(presentation_document, dict) else None,
                sources=sources,
            ),
            evidence=EvidenceSnapshot(
                retrieval_trace_ids=list(dict.fromkeys(self.trace_ids)),
                items=self.evidence_items,
            ),
            versions=producer_versions(),
            metadata_truncated=bool(self.truncated_fields),
            truncated_fields=sorted(self.truncated_fields),
        )

    @staticmethod
    def _terminal_state(result: dict[str, Any]) -> str:
        if result.get("idempotent_replay"):
            return "idempotent_replay"
        if result.get("interrupted"):
            return "interrupted"
        if result.get("presentation_document"):
            return "waiting_user"
        agents = result.get("agents") or []
        if any(item.get("status") == "error" for item in agents if isinstance(item, dict)):
            return "degraded"
        return "completed"

    def _result_summary(self, result: Any) -> dict[str, Any]:
        raw = self._dump(result)
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        return {
            "task_id": raw.get("task_id", ""),
            "intent": raw.get("intent", ""),
            "agent_name": raw.get("agent_name", ""),
            "status": raw.get("status", ""),
            "duration_sec": raw.get("duration_sec"),
            "error_code": raw.get("error_code"),
            "missing_info": self._strings(data.get("missing_info")),
            "present_fields": self._present_trip_fields(data),
        }

    @staticmethod
    def _present_trip_fields(data: dict[str, Any]) -> list[str]:
        nested = data.get("data") if isinstance(data.get("data"), dict) else data
        fields = ("origin", "destination", "start_date", "end_date", "purpose", "work_location")
        return [field for field in fields if nested.get(field)]

    def _result_sources(self, result: Any) -> list[dict[str, Any]]:
        raw = self._dump(result)
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        nested = data.get("data") if isinstance(data.get("data"), dict) else data
        sources = nested.get("sources") or nested.get("retrieved_documents") or []
        return [item for item in sources if isinstance(item, dict)]

    def _answer_sources(self, answer_document: Any) -> list[dict[str, Any]]:
        if not isinstance(answer_document, dict):
            return []
        sources = answer_document.get("sources") or []
        return [self._bounded_mapping(item) for item in sources if isinstance(item, dict)][:self.MAX_EVIDENCE]

    def _add_evidence(self, item: dict[str, Any]) -> None:
        if len(self.evidence_items) >= self.MAX_EVIDENCE:
            self.truncated_fields.add("evidence.items")
            return
        normalized = self._bounded_mapping(item)
        metadata = normalized.get("metadata") if isinstance(normalized.get("metadata"), dict) else {}
        trace_id = normalized.get("trace_id") or metadata.get("trace_id")
        if trace_id:
            self.trace_ids.append(str(trace_id))
        identity = normalized.get("chunk_id") or metadata.get("chunk_id") or normalized.get("id")
        if identity and any(
            (entry.get("chunk_id") or entry.get("id")) == identity
            for entry in self.evidence_items
        ):
            return
        self.evidence_items.append(normalized)

    def _bounded_mapping(self, item: dict[str, Any]) -> dict[str, Any]:
        raw = self._dump(item)
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        excerpt = (
            raw.get("excerpt")
            or raw.get("content")
            or raw.get("text")
            or raw.get("display_text")
            or metadata.get("display_text")
            or ""
        )
        normalized = {
            "trace_id": raw.get("trace_id") or raw.get("retrieval_trace_id") or metadata.get("trace_id") or metadata.get("retrieval_trace_id") or "",
            "chunk_id": raw.get("chunk_id") or metadata.get("chunk_id") or "",
            "chunk_hash": raw.get("chunk_hash") or raw.get("hash") or metadata.get("chunk_hash") or metadata.get("hash") or "",
            "document_id": raw.get("document_id") or metadata.get("document_id") or "",
            "document_version": raw.get("document_version") or metadata.get("document_version") or "",
            "file": raw.get("file") or raw.get("title") or metadata.get("filename") or metadata.get("file_name") or metadata.get("source") or "",
            "page": raw.get("page") or metadata.get("page_start") or metadata.get("page") or metadata.get("page_number"),
            "section": raw.get("section") or metadata.get("section") or metadata.get("title") or "",
            "excerpt": self._text(excerpt, self.MAX_EVIDENCE_EXCERPT_CHARS, "evidence.items.excerpt"),
        }
        return {key: value for key, value in normalized.items() if value not in (None, "")}

    def _text(self, value: Any, limit: int, field: str) -> str:
        text = redact_sensitive_text(str(value or ""))
        if len(text) > limit:
            self.truncated_fields.add(field)
            return text[:limit]
        return text

    @staticmethod
    def _strings(value: Any) -> list[str]:
        return [str(item) for item in value] if isinstance(value, list) else []

    @staticmethod
    def _dump(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return dict(value) if isinstance(value, dict) else {}
