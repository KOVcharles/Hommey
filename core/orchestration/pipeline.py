"""Phase-one task-scoped multi-intent pipeline."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional

from core.presentation import AnswerDocument

from .composer import AnswerComposer
from .decomposer import TaskDecomposer
from .events import phase_event
from .executor import ProgressCallback, TaskExecutor
from .graph_builder import TaskGraphBuilder
from .models import ExecutionTask, IntentTask, TaskResult
from .validator import TaskValidator

logger = logging.getLogger(__name__)


@dataclass
class PipelineOutput:
    answer_document: AnswerDocument
    tasks: List[IntentTask]
    execution_tasks: List[ExecutionTask]
    results: List[TaskResult]


class MultiIntentPipeline:
    def __init__(self, *, model, composer_model, agent_runner):
        self.decomposer = TaskDecomposer(model)
        self.validator = TaskValidator()
        self.graph_builder = TaskGraphBuilder()
        self.executor = TaskExecutor(agent_runner)
        self.composer = AnswerComposer(composer_model)

    async def run(
        self,
        original_query: str,
        intention_data: Dict[str, Any],
        base_context: Dict[str, Any],
        progress: Optional[ProgressCallback] = None,
        task_query: Optional[str] = None,
    ) -> PipelineOutput:
        await self._emit(progress, phase_event("decomposing", "tasks_decomposing"))
        decomposition_query = task_query or original_query
        raw_tasks = await self.decomposer.decompose(decomposition_query, intention_data)
        try:
            tasks = self.validator.validate(raw_tasks, intention_data)
        except ValueError as exc:
            logger.warning("Rejected decomposed tasks; using deterministic fallback: %s", exc)
            raw_tasks = self.decomposer.fallback(
                decomposition_query,
                [
                    item.get("type") for item in intention_data.get("intents", [])
                    if item.get("should_call_skill")
                ],
            )
            tasks = self.validator.validate(raw_tasks, intention_data)

        execution_tasks = self.graph_builder.compile(tasks)
        results = await self.executor.execute(execution_tasks, base_context, progress)
        await self._emit(progress, phase_event("composing", "answer_composing"))
        answer_document = await self.composer.compose(original_query, tasks, results)
        await self._emit(progress, phase_event("done", "answer_ready"))
        return PipelineOutput(answer_document, tasks, execution_tasks, results)

    @staticmethod
    async def _emit(progress: Optional[ProgressCallback], event) -> None:
        if progress is not None:
            await progress(event)
