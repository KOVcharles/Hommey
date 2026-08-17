"""Bounded, fail-open handoff from chat requests to evaluation storage."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from evaluation.models import TurnEvaluationMetadata
from evaluation.repository import EvaluationRepository
from settings import EVALUATION_CONFIG
from utils.logging_safety import sanitize_for_log
from utils.observability import metrics

logger = logging.getLogger(__name__)


class NoopEvaluationSink:
    enabled = False

    def try_emit(self, metadata: TurnEvaluationMetadata) -> bool:
        return False

    async def close(self) -> None:
        return None


class BoundedEvaluationSink:
    """`try_emit` never waits for storage and never raises into chat."""

    enabled = True

    def __init__(
        self,
        repository: EvaluationRepository,
        *,
        queue_size: int = 256,
        evaluator_version: str = "turn-evaluator-v1",
        judge_model: str = "",
        judge_prompt_version: str = "turn-judge-v1",
        rubric_version: str = "travel-rubric-v1",
    ):
        self.repository = repository
        self.queue: asyncio.Queue[TurnEvaluationMetadata] = asyncio.Queue(maxsize=max(1, queue_size))
        self.evaluator_version = evaluator_version
        self.judge_model = judge_model
        self.judge_prompt_version = judge_prompt_version
        self.rubric_version = rubric_version
        self._writer_task: Optional[asyncio.Task] = None

    def try_emit(self, metadata: TurnEvaluationMetadata) -> bool:
        try:
            self._ensure_writer()
            self.queue.put_nowait(metadata)
            metrics.increment("evaluation_events_emitted_total")
            metrics.observe("evaluation_queue_depth", self.queue.qsize())
            return True
        except asyncio.QueueFull:
            metrics.increment("evaluation_event_dropped_total", {"reason": "queue_full"})
        except Exception as exc:
            metrics.increment("evaluation_event_dropped_total", {"reason": "sink_error"})
            logger.warning("evaluation_emit_failed error=%s", sanitize_for_log(exc))
        return False

    async def close(self) -> None:
        task = self._writer_task
        self._writer_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.repository.close)

    def _ensure_writer(self) -> None:
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.get_running_loop().create_task(
                self._writer(), name="evaluation-event-writer",
            )

    async def _writer(self) -> None:
        while True:
            metadata = await self.queue.get()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.repository.create_subject_and_run,
                        metadata,
                        evaluator_version=self.evaluator_version,
                        judge_model=self.judge_model,
                        judge_prompt_version=self.judge_prompt_version,
                        rubric_version=self.rubric_version,
                    ),
                    timeout=float(EVALUATION_CONFIG.get("database_timeout_sec", 5.0)),
                )
                metrics.increment("evaluation_runs_total", {"status": "pending"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics.increment("evaluation_event_dropped_total", {"reason": "writer_error"})
                logger.warning(
                    "evaluation_write_failed request_id=%s error=%s",
                    metadata.subject.request_id,
                    sanitize_for_log(exc),
                )
            finally:
                self.queue.task_done()
                metrics.observe("evaluation_queue_depth", self.queue.qsize())


def create_evaluation_sink():
    if not EVALUATION_CONFIG.get("enabled", False):
        return NoopEvaluationSink()
    return BoundedEvaluationSink(
        EvaluationRepository(),
        queue_size=int(EVALUATION_CONFIG.get("queue_size", 256)),
        evaluator_version=str(EVALUATION_CONFIG.get("evaluator_version", "turn-evaluator-v1")),
        judge_model=str(EVALUATION_CONFIG.get("judge_model", "")),
        judge_prompt_version=str(EVALUATION_CONFIG.get("judge_prompt_version", "turn-judge-v1")),
        rubric_version=str(EVALUATION_CONFIG.get("rubric_version", "travel-rubric-v1")),
    )


evaluation_sink = create_evaluation_sink()


async def close_evaluation_sink() -> None:
    await evaluation_sink.close()
