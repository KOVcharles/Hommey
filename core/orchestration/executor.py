"""Dependency-aware task execution with task-scoped context and progress events."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from .events import task_event
from .graph_builder import TaskGraphBuilder
from .models import ExecutionTask, ProgressEvent, TaskResult

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


class TaskExecutor:
    def __init__(self, agent_runner):
        self.agent_runner = agent_runner

    async def execute(
        self,
        tasks: Iterable[ExecutionTask],
        base_context: Dict[str, Any],
        progress: Optional[ProgressCallback] = None,
    ) -> List[TaskResult]:
        task_list = list(tasks)
        results: List[TaskResult] = []
        for batch in TaskGraphBuilder.batches(task_list):
            for task in batch:
                await self._emit(progress, task_event("queued", task.task_id, task.intent))
            batch_results = await asyncio.gather(*[
                self._run_one(task, base_context, results, progress)
                for task in batch
            ])
            results.extend(batch_results)
        return sorted(results, key=lambda item: item.display_order)

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
        if task.intent == "information_query" and data.get("query_success") is False:
            nested = data.get("results") if isinstance(data.get("results"), dict) else {}
            normalized_status = "error"
            error_code = error_code or "INFORMATION_QUERY_UNAVAILABLE"
            error_message = error_message or nested.get("message") or nested.get("error") or "外部信息暂时不可用"
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
