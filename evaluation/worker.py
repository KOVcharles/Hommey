"""Lease-based execution of deterministic checks and the optional Judge Agent."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from typing import Any

from agentscope.message import Msg

from core.intent_result import parse_json_object
from evaluation.decision import decide
from evaluation.facts import extract_facts
from evaluation.models import JudgeResult, RuleResult, TurnEvaluationMetadata
from evaluation.repository import EvaluationRepository
from evaluation.rules import has_hard_failure, run_rules
from settings import EVALUATION_CONFIG
from utils.logging_safety import sanitize_for_log
from utils.observability import metrics

logger = logging.getLogger(__name__)


class TurnEvaluationWorker:
    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        judge_agent=None,
        worker_id: str | None = None,
        concurrency: int = 2,
        lease_seconds: int = 90,
        sample_rate: float = 0.2,
        judge_enabled: bool | None = None,
    ):
        self.repository = repository
        self.judge_agent = judge_agent
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:10]}"
        self.concurrency = max(1, concurrency)
        self.lease_seconds = max(1, lease_seconds)
        self.sample_rate = min(max(sample_rate, 0.0), 1.0)
        self.judge_enabled = judge_agent is not None if judge_enabled is None else judge_enabled

    async def run_once(self) -> int:
        await asyncio.to_thread(
            self.repository.recover_expired_leases,
            max_attempts=int(EVALUATION_CONFIG.get("max_attempts", 3)),
        )
        rows = await asyncio.to_thread(
            self.repository.claim_pending,
            worker_id=self.worker_id,
            batch_size=self.concurrency,
            lease_seconds=self.lease_seconds,
        )
        await asyncio.gather(*(self._evaluate(row) for row in rows))
        return len(rows)

    async def run_forever(self, *, poll_interval: float = 2.0) -> None:
        while True:
            claimed = await self.run_once()
            if not claimed:
                await asyncio.sleep(max(0.1, poll_interval))

    async def _evaluate(self, row: dict[str, Any]) -> None:
        evaluation_id = str(row["evaluation_id"])
        started = time.perf_counter()
        try:
            payload = row.get("payload")
            if isinstance(payload, str):
                payload = json.loads(payload)
            metadata = TurnEvaluationMetadata.model_validate(payload)
            facts = extract_facts(metadata)
            rules = run_rules(metadata, facts)
        except Exception as exc:
            logger.warning("evaluation_fact_extraction_failed id=%s error=%s", evaluation_id, sanitize_for_log(exc))
            await asyncio.to_thread(
                self.repository.fail,
                evaluation_id,
                worker_id=self.worker_id,
                error_code="EVALUATION_FACT_EXTRACTION_FAILED",
            )
            metrics.increment("evaluation_runs_total", {"status": "failed"})
            return

        eligible, skip_reason = self._judge_eligibility(metadata)
        if skip_reason:
            await asyncio.to_thread(
                self.repository.skip,
                evaluation_id,
                worker_id=self.worker_id,
                reason=skip_reason,
            )
            metrics.increment("evaluation_runs_total", {"status": "skipped"})
            return

        judge_result = None
        judge_expected = eligible and self.judge_enabled and not has_hard_failure(rules)
        if judge_expected:
            if self.judge_agent is None:
                rules.append(RuleResult(
                    code="JUDGE_UNAVAILABLE",
                    severity="medium",
                    message="Judge 未配置或当前不可用。",
                    subject_ref="versions",
                ))
            else:
                try:
                    judge_result = await asyncio.wait_for(
                        self._call_judge(metadata, facts.model_dump(mode="json")),
                        timeout=float(EVALUATION_CONFIG.get("judge_timeout_sec", 30.0)),
                    )
                except Exception as exc:
                    logger.warning("evaluation_judge_failed id=%s error=%s", evaluation_id, sanitize_for_log(exc))
                    metrics.increment("evaluation_judge_errors_total", {"error_code": "JUDGE_FAILED"})
                    rules.append(RuleResult(
                        code="JUDGE_UNAVAILABLE",
                        severity="medium",
                        message="Judge 调用或结构化输出失败。",
                        subject_ref="versions",
                    ))

        result = decide(rules, judge_result, judge_expected=judge_expected)
        latency_ms = int((time.perf_counter() - started) * 1000)
        saved = await asyncio.to_thread(
            self.repository.complete,
            evaluation_id,
            worker_id=self.worker_id,
            result=result,
            latency_ms=latency_ms,
        )
        if saved:
            metrics.increment("evaluation_runs_total", {"status": "completed", "verdict": result.verdict})
            metrics.observe("evaluation_judge_latency_ms", latency_ms)
            # Only deterministic rule codes become metric labels; free-form
            # Judge codes stay in storage to avoid unbounded label cardinality.
            for rule in result.rule_results:
                metrics.increment("evaluation_rule_failures_total", {"reason_code": rule.code})

    async def _call_judge(self, metadata: TurnEvaluationMetadata, facts: dict[str, Any]) -> JudgeResult:
        message = Msg(
            name="evaluation_worker",
            role="user",
            content=json.dumps({
                "metadata": metadata.model_dump(mode="json"),
                "facts": facts,
            }, ensure_ascii=False),
        )
        response = await self.judge_agent.reply(message)
        raw = response.content if hasattr(response, "content") else response
        if isinstance(raw, str):
            raw = parse_json_object(raw)
        return JudgeResult.model_validate(raw)

    def _judge_eligibility(self, metadata: TurnEvaluationMetadata) -> tuple[bool, str]:
        state = metadata.execution.terminal_state
        skills = set(metadata.routing.selected_skills)
        if state in {"interrupted", "idempotent_replay"}:
            return False, f"SKIP_{state.upper()}"
        if skills == {"chitchat"} or (not skills and "chitchat" in metadata.routing.intents):
            return False, "SKIP_CHITCHAT"
        if state == "failed":
            return False, ""
        if skills.intersection({"ask-question", "check-trip-compliance", "plan-trip"}):
            return True, ""
        if state == "degraded":
            return True, ""
        sampled = _stable_sample(metadata.subject.request_id) < self.sample_rate
        return sampled, "" if sampled else "SKIP_NOT_SAMPLED"


def _stable_sample(request_id: str) -> float:
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)
