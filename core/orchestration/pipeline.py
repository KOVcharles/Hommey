"""Scoped task-DAG pipeline for all skill-backed intents."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from core.presentation import AnswerDocument

from .composer import AnswerComposer
from .decomposer import TaskDecomposer
from .events import phase_event
from .executor import ProgressCallback, TaskExecutor
from .graph_builder import TaskGraphBuilder
from .models import ExecutionTask, IntentTask, PipelineOutput, TaskResult
from .validator import TaskValidator

logger = logging.getLogger(__name__)


class MultiIntentPipeline:
    """One entry point: decompose → validate → compile → execute → compose.

    Stage 2 adds ``memory_hooks``; stage 3 adds ``checkpoint_store`` and
    ``run_resume``. Those seams are kept optional so the pipeline is testable
    without them and each stage stays independently shippable.
    """

    def __init__(
        self,
        *,
        model,
        composer_model,
        agent_runner,
        memory_hooks=None,
        checkpoint_store=None,
    ):
        self.decomposer = TaskDecomposer(model)
        self.validator = TaskValidator()
        self.graph_builder = TaskGraphBuilder()
        self.executor = TaskExecutor(agent_runner)
        self.composer = AnswerComposer(composer_model)
        self.memory_hooks = memory_hooks
        self.checkpoint_store = checkpoint_store

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
                intention_data.get("key_entities") or {},
            )
            tasks = self.validator.validate(raw_tasks, intention_data)

        execution_tasks = self.graph_builder.compile(tasks)
        results, paused = await self.executor.execute(execution_tasks, base_context, progress)

        if paused is not None:
            return await self._paused_output(tasks, execution_tasks, results, paused, progress)

        await self._emit(progress, phase_event("composing", "answer_composing"))
        answer_document = await self.composer.compose(original_query, tasks, results)
        if self.memory_hooks is not None:
            await self.memory_hooks.apply(results)
        await self._emit(progress, phase_event("done", "answer_ready"))
        return PipelineOutput(
            tasks=tasks,
            execution_tasks=execution_tasks,
            results=results,
            answer_document=answer_document,
        )

    async def _paused_output(
        self,
        tasks: List[IntentTask],
        execution_tasks: List[ExecutionTask],
        results,
        pause_info,
        progress: Optional[ProgressCallback],
    ) -> PipelineOutput:
        """检查点暂停：保存现场并交给 presentation。"""
        if self.checkpoint_store is not None:
            await self.checkpoint_store.save(pause_info)
        presentation_document = self._build_presentation(pause_info)
        await self._emit(progress, phase_event("done", "trip_details_needed"))
        return PipelineOutput(
            tasks=tasks,
            execution_tasks=execution_tasks,
            results=results,
            paused=True,
            pause_info=pause_info,
            presentation_document=presentation_document,
        )

    async def run_resume(
        self,
        original_query: str,
        intention_data: Dict[str, Any],
        base_context: Dict[str, Any],
        progress: Optional[ProgressCallback] = None,
        task_query: Optional[str] = None,
    ) -> PipelineOutput:
        """跨轮续跑：重跑 intake 步骤并判定，再执行剩余步骤或继续暂停。

        调用方负责在检查点存在且当前意图命中其 resume 集时进入本方法。
        未命中检查点则退化为常规 ``run``。
        """
        if self.checkpoint_store is None:
            return await self.run(original_query, intention_data, base_context, progress, task_query)
        checkpoint = await self.checkpoint_store.get()
        if checkpoint is None:
            return await self.run(original_query, intention_data, base_context, progress, task_query)

        await self._emit(progress, phase_event("decomposing", "tasks_decomposing"))
        # skill 目录名 ↔ intent 1:1（skill_to_intent），无需在表里冗余存 intent。
        from core.intent_catalog import skill_to_intent

        intent = skill_to_intent(checkpoint.skill) or checkpoint.skill
        # 事实源合并：本轮新消息通过重跑 intake 步骤增量补充。
        facts = dict(checkpoint.collected_facts)
        entities = dict(checkpoint.entities)
        intake_task = self._build_intake_task(checkpoint, intent, entities)
        remaining = self._build_resume_tasks(checkpoint, intent, facts, entities)
        tasks_to_run = ([intake_task] if intake_task else []) + remaining
        if not tasks_to_run:
            await self.checkpoint_store.clear()
            return await self.run(original_query, intention_data, base_context, progress, task_query)

        results, paused = await self.executor.execute(tasks_to_run, base_context, progress)

        if paused is not None:
            # 重跑 intake 后仍不齐：合并新事实，更新检查点并继续暂停。
            intake_result = next(
                (r for r in results if r.task_id == intake_task.task_id), None
            ) if intake_task else None
            if intake_result is not None and isinstance(intake_result.data, dict):
                facts = dict(facts)
                facts.update(
                    intake_result.data.get("data")
                    if isinstance(intake_result.data.get("data"), dict)
                    else intake_result.data
                )
            updated = self._merge_facts_into_pause(paused, facts)
            await self.checkpoint_store.save(updated)
            presentation_document = self._build_presentation(facts)
            await self._emit(progress, phase_event("done", "trip_details_needed"))
            return PipelineOutput(
                tasks=[],
                execution_tasks=tasks_to_run,
                results=results,
                paused=True,
                pause_info=updated,
                presentation_document=presentation_document,
            )

        synthetic = IntentTask(
            task_id=intent,
            intent=intent,
            query=original_query,
            entities=entities,
            display_order=0,
        )
        await self._emit(progress, phase_event("composing", "answer_composing"))
        answer_document = await self.composer.compose(original_query, [synthetic], results)
        if self.memory_hooks is not None:
            await self.memory_hooks.apply(results)
        await self.checkpoint_store.clear()
        await self._emit(progress, phase_event("done", "answer_ready"))
        return PipelineOutput(
            tasks=[synthetic],
            execution_tasks=tasks_to_run,
            results=results,
            answer_document=answer_document,
        )

    @staticmethod
    def _build_intake_task(checkpoint, intent: str, entities: Dict[str, Any]) -> Optional[ExecutionTask]:
        from core.intent_catalog import execution_steps_for_intent

        for step in execution_steps_for_intent(intent):
            if step.agent_name != checkpoint.pause_agent:
                continue
            return ExecutionTask(
                intent=intent,
                task_id=f"{intent}-{step.agent_name}",
                query=TaskGraphBuilder._render_query(step.query, entities) or "",
                entities=entities,
                agent_name=step.agent_name,
                priority=step.priority,
                reason=step.reason or "",
                expected_output=step.expected_output or "",
                max_retries=step.max_retries,
                failure_policy=step.on_failure,
                result_rules=dict(step.result_rules or {}),
                display_order=0,
            )
        return None

    @staticmethod
    def _build_resume_tasks(
        checkpoint,
        intent: str,
        facts: Dict[str, Any],
        entities: Dict[str, Any],
    ) -> List[ExecutionTask]:
        merged = {**entities, **facts}
        tasks = []
        for order, step in enumerate(checkpoint.steps_remaining, start=1):
            if not isinstance(step, dict):
                continue
            agent = step.get("agent_name")
            query = TaskGraphBuilder._render_query(step.get("query"), merged)
            tasks.append(ExecutionTask(
                intent=intent,
                task_id=f"{intent}-{agent}",
                query=query or "",
                entities=entities,
                agent_name=agent,
                priority=int(step.get("priority", 1)),
                reason=step.get("reason") or "",
                expected_output=step.get("expected_output") or "",
                max_retries=int(step.get("max_retries", 0)),
                failure_policy=step.get("on_failure", "abort"),
                result_rules=dict(step.get("result_rules") or {}),
                display_order=order,
            ))
        return tasks

    @staticmethod
    def _merge_facts_into_pause(pause_info, facts: Dict[str, Any]):
        """把重跑 intake 后的最新事实写回 PauseInfo（collected_facts 更新）。"""
        from .models import PauseInfo

        return PauseInfo(
            intent=pause_info.intent,
            skill=pause_info.skill,
            pause_agent=pause_info.pause_agent,
            pause_field=pause_info.pause_field,
            planning_ready=pause_info.planning_ready,
            steps_remaining=pause_info.steps_remaining,
            collected_facts=facts,
            entities=pause_info.entities,
        )

    @staticmethod
    def _build_presentation(pause_info):
        from core.presentation.trip_intake_document import build_trip_intake_document

        facts = (
            pause_info.collected_facts
            if hasattr(pause_info, "collected_facts")
            else pause_info
        )
        return build_trip_intake_document(facts)

    @staticmethod
    async def _emit(progress: Optional[ProgressCallback], event) -> None:
        if progress is not None:
            await progress(event)
