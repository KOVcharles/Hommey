import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.collector import TurnEvaluationCollector
from evaluation.decision import decide
from evaluation.facts import extract_facts
from evaluation.models import JudgeResult, RuleResult, TurnEvaluationMetadata
from evaluation.report import build_markdown
from evaluation.reconciler import TurnEvaluationReconciler
from evaluation.rules import run_rules
from evaluation.sink import BoundedEvaluationSink
from evaluation.worker import TurnEvaluationWorker
import webui_new.manager as manager_module
from webui_new.manager import HommeyWebInstance


def _metadata(
    *,
    request_id="req-1",
    skills=None,
    intents=None,
    evidence=None,
    sources=None,
    terminal_state="completed",
):
    return TurnEvaluationMetadata.model_validate({
        "schema_version": "eval.turn.input.1",
        "subject": {
            "request_id": request_id,
            "session_id": "session-1",
            "run_id": "",
            "turn_id": "turn-1",
            "occurred_at": "2026-08-12T00:00:00Z",
        },
        "conversation": {
            "user_message": "上海出差的住宿标准是多少？",
            "assistant_message": "根据制度，住宿标准见来源。",
            "previous_messages": [],
        },
        "routing": {
            "intents": intents or ["rag_knowledge"],
            "selected_skills": skills or ["ask-question"],
        },
        "execution": {
            "terminal_state": terminal_state,
            "paused": terminal_state == "waiting_user",
            "interrupted": False,
            "tasks": [],
            "agent_results": [],
            "timings": {"total": 0.4},
            "error_codes": [],
        },
        "answer": {
            "answer_document": None,
            "presentation_document": None,
            "sources": sources or [],
        },
        "evidence": {"retrieval_trace_ids": [], "items": evidence or []},
        "versions": {
            "git_revision": "abc",
            "production_model": "model-a",
            "intent_prompt_version": "v1",
            "composer_prompt_version": "v1",
            "skill_versions": {"ask-question": "1.0.0"},
            "rag_index_version": "r1",
        },
        "capture_mode": "live",
        "evaluation_context_quality": "full",
        "metadata_truncated": False,
        "truncated_fields": [],
    })


def test_collector_bounds_and_redacts_snapshot():
    collector = TurnEvaluationCollector(
        request_id="req-1",
        session_id="session-1",
        user_message="手机号 13800138000 " + "x" * 13_000,
    )
    collector.record_routing({
        "routing": {"intent": "rag_knowledge", "should_call_skill": True},
        "intents": [{"type": "rag_knowledge", "should_call_skill": True}],
    })
    snapshot = collector.freeze({
        "response": "请查看制度",
        "answer_document": None,
        "presentation_document": None,
        "agents": [],
        "timings": {"total": 0.2},
    }, session_id="session-1")

    assert "13800138000" not in snapshot.conversation.user_message
    assert len(snapshot.conversation.user_message) <= collector.MAX_MESSAGE_CHARS
    assert snapshot.metadata_truncated is True
    assert snapshot.routing.selected_skills == ["ask-question"]


def test_collector_keeps_only_bounded_evidence_fields():
    collector = TurnEvaluationCollector(
        request_id="req-1", session_id="session-1", user_message="政策是什么？",
    )
    collector._add_evidence({
        "retrieval_trace_id": "trace-1",
        "content": "e" * 2_000,
        "metadata": {"chunk_id": "chunk-1", "document_version": "v1", "secret": "drop"},
        "unbounded_internal_payload": {"drop": "x" * 10_000},
    })

    item = collector.evidence_items[0]
    assert item["trace_id"] == "trace-1"
    assert item["chunk_id"] == "chunk-1"
    assert len(item["excerpt"]) == collector.MAX_EVIDENCE_EXCERPT_CHARS
    assert "metadata" not in item
    assert "unbounded_internal_payload" not in item


def test_source_missing_is_hard_and_judge_cannot_override_it():
    metadata = _metadata()
    rules = run_rules(metadata, extract_facts(metadata))
    judge = JudgeResult.model_validate({
        "schema_version": "eval.turn.result.1",
        "verdict": "pass",
        "score": 98,
        "dimensions": {
            "understanding": 4,
            "task_progress": 4,
            "groundedness": 4,
            "safety": 4,
            "clarity": 4,
        },
        "reason_codes": [],
        "critical_errors": [],
        "findings": [],
        "review_required": False,
        "summary": "看起来很好。",
    })

    result = decide(rules, judge, judge_expected=True)

    assert "SOURCE_MISSING" in result.reason_codes
    assert result.verdict == "fail"
    assert result.score == 49


def test_critical_rule_forces_critical_fail():
    result = decide([
        RuleResult(
            code="CROSS_USER_DATA_RISK",
            severity="critical",
            message="cross-user data",
            hard=True,
        )
    ], None, judge_expected=False)

    assert result.verdict == "critical_fail"
    assert result.review_required is True


def test_waiting_user_does_not_require_policy_evidence():
    metadata = _metadata(terminal_state="waiting_user", skills=["plan-trip"], intents=["itinerary_planning"])

    rules = run_rules(metadata, extract_facts(metadata))

    assert "SOURCE_MISSING" not in {rule.code for rule in rules}


def test_definite_policy_amount_without_evidence_is_critical():
    metadata = _metadata()
    metadata.conversation.assistant_message = "公司住宿标准是每天 800 元。"

    rules = run_rules(metadata, extract_facts(metadata))

    rule = next(item for item in rules if item.code == "SHOULD_RETURN_UNKNOWN")
    assert rule.severity == "critical"


class _RecordingRepository:
    def __init__(self, block=False):
        self.block = block
        self.calls = []

    def create_subject_and_run(self, metadata, **kwargs):
        self.calls.append((metadata.subject.request_id, kwargs))
        if self.block:
            import time
            time.sleep(0.1)
        return "subject", "evaluation"

    def close(self):
        pass


@pytest.mark.asyncio
async def test_bounded_sink_drops_when_full_without_waiting():
    repository = _RecordingRepository(block=True)
    sink = BoundedEvaluationSink(repository, queue_size=1)
    metadata = _metadata()

    # Stop automatic consumption so queue capacity can be tested deterministically.
    sink._ensure_writer = lambda: None
    assert sink.try_emit(metadata) is True
    assert sink.try_emit(metadata) is False
    await sink.close()


class _WorkerRepository:
    def __init__(self, metadata):
        self.metadata = metadata
        self.completed = []
        self.skipped = []

    def recover_expired_leases(self, **kwargs):
        return 0

    def claim_pending(self, **kwargs):
        if self.metadata is None:
            return []
        payload = self.metadata.model_dump(mode="json")
        self.metadata = None
        return [{"evaluation_id": "eval-1", "payload": payload}]

    def complete(self, evaluation_id, **kwargs):
        self.completed.append((evaluation_id, kwargs))
        return True

    def skip(self, evaluation_id, **kwargs):
        self.skipped.append((evaluation_id, kwargs))
        return True

    def fail(self, evaluation_id, **kwargs):
        raise AssertionError(f"unexpected failure: {evaluation_id} {kwargs}")


@pytest.mark.asyncio
async def test_worker_completes_rules_only_failure_without_calling_judge():
    repository = _WorkerRepository(_metadata())

    class Judge:
        async def reply(self, _message):
            raise AssertionError("hard deterministic failures must skip Judge")

    worker = TurnEvaluationWorker(repository, judge_agent=Judge(), concurrency=1)
    assert await worker.run_once() == 1

    saved = repository.completed[0][1]["result"]
    assert saved.verdict == "fail"
    assert "SOURCE_MISSING" in saved.reason_codes


@pytest.mark.asyncio
async def test_worker_skips_chitchat():
    repository = _WorkerRepository(_metadata(skills=["chitchat"], intents=["chitchat"]))
    worker = TurnEvaluationWorker(repository, judge_agent=None, concurrency=1)

    await worker.run_once()

    assert repository.skipped[0][1]["reason"] == "SKIP_CHITCHAT"
    assert repository.completed == []


def test_report_contains_skill_and_review_queue_without_conversation_text():
    report = build_markdown({
        "days": 7,
        "assistant_turns": 10,
        "rows": [{
            "subject_id": "s1",
            "evaluation_id": "e1",
            "status": "completed",
            "verdict": "fail",
            "reason_codes": ["SOURCE_MISSING"],
            "review_required": True,
            "selected_skills": ["ask-question"],
        }],
    })

    assert "Capture coverage: 10.0%" in report
    assert "ask-question" in report
    assert "SOURCE_MISSING" in report
    assert "上海出差" not in report


def test_evaluation_migration_has_idempotency_and_lease_constraints():
    sql = Path("webui_new/auth/migrations/0018_agent_evaluation.sql").read_text(encoding="utf-8")

    assert "UNIQUE (subject_type, request_id)" in sql
    assert "UNIQUE (subject_id, evaluator_version)" in sql
    assert "lease_owner" in sql
    assert "lease_expires_at" in sql
    assert "evaluation_reviews" in sql
    assert "DROP TABLE" not in sql.upper()


@pytest.mark.asyncio
async def test_reconciler_builds_reduced_snapshot_without_future_context():
    class Repository:
        def __init__(self):
            self.created = []

        def missing_turn_subjects(self, **_kwargs):
            return [{
                "request_id": "req-1",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "occurred_at": "2026-08-12T00:00:00Z",
                "user_message": "A 轮问题",
                "assistant_message": "A 轮回答",
                "answer_document": None,
                "presentation_document": None,
            }]

        def create_subject_and_run(self, metadata, **kwargs):
            self.created.append((metadata, kwargs))
            return "subject", "evaluation"

    repository = Repository()
    reconciler = TurnEvaluationReconciler(repository, evaluator_version="v1")

    assert await reconciler.run_once() == 1
    metadata = repository.created[0][0]
    assert metadata.capture_mode == "reconciled"
    assert metadata.evaluation_context_quality == "reduced"
    assert metadata.conversation.previous_messages == []
    assert metadata.conversation.user_message == "A 轮问题"
    assert "B 轮" not in metadata.model_dump_json()


@pytest.mark.asyncio
async def test_chat_return_is_unchanged_when_capture_is_enabled(monkeypatch):
    emitted = []

    class Sink:
        enabled = True

        def try_emit(self, metadata):
            emitted.append(metadata)
            return True

    expected = {
        "response": "已完成",
        "answer_document": None,
        "presentation_document": None,
        "agents": [],
        "preferences_updated": False,
        "timings": {"total": 0.1},
    }
    instance = HommeyWebInstance("u1")
    instance.initialized = True
    instance.memory_manager = SimpleNamespace(_current_turn_id="turn-1")

    async def implementation(
        _message, request_id=None, attachment_ids=None, retrieval_mode="standard"
    ):
        return dict(expected)

    monkeypatch.setattr(manager_module, "evaluation_sink", Sink())
    monkeypatch.setattr(instance, "_process_message_impl", implementation)

    result = await instance.process_message("测试", request_id="req-1")

    assert result == expected
    assert emitted[0].subject.request_id == "req-1"
    assert emitted[0].conversation.assistant_message == "已完成"


@pytest.mark.asyncio
async def test_capture_failure_does_not_fail_chat(monkeypatch):
    class Sink:
        enabled = True

        def try_emit(self, _metadata):
            raise RuntimeError("storage down")

    instance = HommeyWebInstance("u1")
    instance.initialized = True
    instance.memory_manager = SimpleNamespace(_current_turn_id="turn-1")

    async def implementation(
        _message, request_id=None, attachment_ids=None, retrieval_mode="standard"
    ):
        return {"response": "仍然成功", "agents": []}

    monkeypatch.setattr(manager_module, "evaluation_sink", Sink())
    monkeypatch.setattr(instance, "_process_message_impl", implementation)

    result = await instance.process_message("测试", request_id="req-1")

    assert result["response"] == "仍然成功"
