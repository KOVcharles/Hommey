"""Dependency-aware task execution with task-scoped context and progress events."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from core.intent_catalog import (
    checkpoint_spec_for_intent,
    execution_steps_for_intent,
    intent_to_skill,
)

from .events import task_event
from .graph_builder import TaskGraphBuilder
from .models import ExecutionTask, PauseInfo, ProgressEvent, TaskResult

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


def _nested(data: Dict[str, Any]) -> Dict[str, Any]:
    return data.get("data") if isinstance(data.get("data"), dict) else data


class TaskExecutor:
    def __init__(self, agent_runner):
        self.agent_runner = agent_runner

    async def execute(
        self,
        tasks: Iterable[ExecutionTask],
        base_context: Dict[str, Any],
        progress: Optional[ProgressCallback] = None,
    ) -> tuple[List[TaskResult], Optional[PauseInfo]]:
        task_list = list(tasks)
        task_by_id = {task.task_id: task for task in task_list}
        results: List[TaskResult] = []
        paused: Optional[PauseInfo] = None
        skipped: set = set()

        for batch in TaskGraphBuilder.batches(task_list):
            if paused is not None:
                break
            active = [task for task in batch if task.task_id not in skipped]
            if not active:
                continue
            for task in active:
                await self._emit(progress, task_event("queued", task.task_id, task.intent))
            batch_results = await asyncio.gather(*[
                self._run_one(task, base_context, results, progress)
                for task in active
            ])
            results.extend(batch_results)

            # abort-halt：错误 + 步骤声明 abort → 下游（同意图后继 + 传递依赖）标跳过。
            for result in batch_results:
                task = task_by_id.get(result.task_id)
                if result.status == "error" and task is not None and task.failure_policy == "abort":
                    self._mark_downstream_skipped(
                        task_list, result, task_by_id, results, skipped
                    )
                    break

            # pause gate：声明 checkpoint 的步骤在 pause_field 为 False 时暂停。
            for result in batch_results:
                info = self._pause_info(result, task_by_id)
                if info is not None:
                    paused = info
                    break

        return sorted(results, key=lambda item: item.display_order), paused

    async def _run_one(
        self,
        task: ExecutionTask,
        base_context: Dict[str, Any],
        previous: List[TaskResult],
        progress: Optional[ProgressCallback],
    ) -> TaskResult:
        await self._emit(progress, task_event("running", task.task_id, task.intent))
        scoped_context = dict(base_context)
        scoped_context["rewritten_query"] = task.query
        scoped_context["active_task"] = {
            "task_id": task.task_id,
            "intent": task.intent,
            "query": task.query,
            "entities": task.entities,
        }
        previous_results = [self._legacy_result(item) for item in previous]
        runtime = await self.agent_runner(
            agent_name=task.agent_name,
            context=scoped_context,
            reason=task.reason,
            expected_output=task.expected_output,
            previous_results=previous_results,
            task_params={"task_id": task.task_id, "intent": task.intent, "query": task.query},
            max_retries=task.max_retries,
        )
        status = runtime.get("status", "error")
        normalized_status = status if status in {"success", "error", "skipped"} else "error"
        data = runtime.get("data") if isinstance(runtime.get("data"), dict) else {}
        error_message = runtime.get("error_message")
        error_code = runtime.get("error_code")
        # 步骤级 result_rules（来自 skill 声明）取代按 intent 硬编码的特殊分支。
        if task.result_rules and data.get(task.result_rules.get("error_when_field")) is False:
            nested = data.get("results") if isinstance(data.get("results"), dict) else {}
            normalized_status = "error"
            error_code = error_code or task.result_rules.get("error_code")
            error_message = (
                error_message
                or nested.get("message")
                or nested.get("error")
                or "该查询暂时不可用，请稍后重试。"
            )
        evidence = self._evidence(data)
        result = TaskResult(
            task_id=task.task_id,
            intent=task.intent,
            agent_name=task.agent_name,
            status=normalized_status,
            data=data,
            evidence=evidence,
            duration_sec=runtime.get("duration_sec"),
            attempts=int(runtime.get("attempts") or 1),
            error_code=error_code,
            error_message=error_message,
            display_order=task.display_order,
        )
        event_phase = "completed" if result.status == "success" else "failed"
        await self._emit(progress, task_event(event_phase, task.task_id, task.intent))
        return result

    @staticmethod
    def _pause_info(result: TaskResult, task_by_id: Dict[str, ExecutionTask]) -> Optional[PauseInfo]:
        task = task_by_id.get(result.task_id)
        if task is None:
            return None
        spec = checkpoint_spec_for_intent(task.intent)
        if spec is None or not spec.enabled or spec.pause_agent is None:
            return None
        if task.agent_name != spec.pause_agent:
            return None
        data = _nested(result.data)
        if data.get(spec.pause_field) is not False:
            return None
        steps = execution_steps_for_intent(task.intent)
        remaining = [
            step.model_dump(mode="json") for step in steps if step.priority > task.priority
        ]
        return PauseInfo(
            intent=task.intent,
            skill=intent_to_skill(task.intent) or task.intent,
            pause_agent=spec.pause_agent,
            pause_field=spec.pause_field,
            planning_ready=False,
            steps_remaining=remaining,
            collected_facts=result.data,
            entities=task.entities,
        )

    @staticmethod
    def _mark_downstream_skipped(
        task_list: List[ExecutionTask],
        failed: TaskResult,
        task_by_id: Dict[str, ExecutionTask],
        results: List[TaskResult],
        skipped: set,
    ) -> None:
        failed_task = task_by_id.get(failed.task_id)
        if failed_task is None:
            return
        dependents: Dict[str, set] = {}
        for task in task_list:
            for dep in task.depends_on:
                dependents.setdefault(dep, set()).add(task.task_id)

        downstream: set = set()
        stack = [failed.task_id]
        while stack:
            current = stack.pop()
            for dependent in dependents.get(current, set()):
                if dependent not in downstream:
                    downstream.add(dependent)
                    stack.append(dependent)
        # 同意图、更高优先级的模板后继步骤也是下游（abort 会停住整个 workflow 链）。
        for task in task_list:
            if task.intent == failed.intent and task.priority > failed_task.priority:
                downstream.add(task.task_id)

        executed = {item.task_id for item in results}
        for task_id in downstream:
            if task_id in executed or task_id in skipped:
                continue
            task = task_by_id.get(task_id)
            if task is None:
                continue
            skipped.add(task_id)
            results.append(TaskResult(
                task_id=task_id,
                intent=task.intent,
                agent_name=task.agent_name,
                status="skipped",
                error_code="UPSTREAM_DEPENDENCY_FAILED",
                error_message="依赖的上游任务失败，未执行",
                display_order=task.display_order,
            ))

    @staticmethod
    def _legacy_result(result: TaskResult) -> Dict[str, Any]:
        return {
            "agent_name": result.agent_name,
            "priority": result.display_order + 1,
            "result": {
                "status": result.status,
                "data": result.data,
                "duration_sec": result.duration_sec,
                "attempts": result.attempts,
            },
        }

    @staticmethod
    def _evidence(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates = data.get("sources") or data.get("retrieved_documents") or []
        nested = data.get("results") if isinstance(data.get("results"), dict) else {}
        if not candidates:
            candidates = nested.get("sources") or []
        return [item for item in candidates if isinstance(item, dict)]

    @staticmethod
    async def _emit(progress: Optional[ProgressCallback], event: ProgressEvent) -> None:
        if progress is not None:
            await progress(event)
