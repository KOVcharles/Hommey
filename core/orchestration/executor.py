"""Dependency-aware task execution with task-scoped context and progress events."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from core.intent_catalog import (
    pause_spec_for_intent,
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
        lifecycle=None,
        previous_results: Optional[List[TaskResult]] = None,
        active_goal_ids: Optional[set[str]] = None,
    ) -> tuple[List[TaskResult], List[PauseInfo]]:
        task_list = list(tasks)
        task_by_id = {task.task_id: task for task in task_list}
        dependency_ids = self._dependency_ancestors(task_list)
        results: List[TaskResult] = list(previous_results or [])
        completed_ids = {
            item.task_id for item in results if item.status in {"success", "skipped"}
        }
        pauses: List[PauseInfo] = []
        paused_goals: set[str] = set()
        blocked_ids: set[str] = set()
        skipped: set = set()

        for batch in TaskGraphBuilder.batches(task_list):
            if lifecycle is not None and await lifecycle.should_interrupt():
                await lifecycle.mark_interrupted()
                break
            active = [
                task for task in batch
                if task.task_id not in skipped
                and task.task_id not in completed_ids
                and task.task_id not in blocked_ids
                and task.goal_id not in paused_goals
                and (active_goal_ids is None or task.goal_id in active_goal_ids)
            ]
            if not active:
                continue
            for task in active:
                await self._emit(progress, task_event("queued", task.task_id, task.intent))
            batch_results = await asyncio.gather(*[
                self._run_one(
                    task,
                    base_context,
                    [
                        result for result in results
                        if result.task_id in dependency_ids[task.task_id]
                    ],
                    progress,
                    lifecycle,
                )
                for task in active
            ])
            results.extend(batch_results)

            # abort-halt：错误 + 步骤声明 abort → 下游（同意图后继 + 传递依赖）标跳过。
            # 不加 break：同一批内多个 goal 各自的 abort 失败都要处理，
            # _mark_downstream_skipped 经共享 skipped 集合幂等，重复处理安全。
            for result in batch_results:
                task = task_by_id.get(result.task_id)
                if result.status == "error" and task is not None and task.failure_policy == "abort":
                    newly_skipped = self._mark_downstream_skipped(
                        task_list, result, task_by_id, results, skipped
                    )
                    if lifecycle is not None:
                        for skipped_result in newly_skipped:
                            await lifecycle.node_finished(skipped_result)

            # pause gate：声明 pause 的步骤在 pause_field 为 False 时等待用户。
            for result in batch_results:
                info = self._pause_info(result, task_by_id, task_list)
                if info is not None:
                    if not any(item.goal_id == info.goal_id for item in pauses):
                        pauses.append(info)
                    paused_goals.add(info.goal_id)
                    self._block_waiting_goal(
                        task_list, result.task_id, info.goal_id, blocked_ids
                    )

        return sorted(results, key=lambda item: item.display_order), pauses

    @staticmethod
    def _dependency_ancestors(tasks: List[ExecutionTask]) -> Dict[str, set[str]]:
        """Return the declared dependency closure for every execution node."""
        parents = {task.task_id: set(task.depends_on) for task in tasks}
        memo: Dict[str, set[str]] = {}

        def visit(task_id: str, visiting: set[str]) -> set[str]:
            if task_id in memo:
                return memo[task_id]
            if task_id in visiting:
                raise ValueError(f"task dependency graph contains a cycle at {task_id}")
            visiting.add(task_id)
            ancestors: set[str] = set()
            for parent_id in parents.get(task_id, set()):
                ancestors.add(parent_id)
                ancestors.update(visit(parent_id, visiting))
            visiting.remove(task_id)
            memo[task_id] = ancestors
            return ancestors

        return {task_id: visit(task_id, set()) for task_id in parents}

    @staticmethod
    def _block_waiting_goal(
        task_list: List[ExecutionTask], paused_task_id: str,
        goal_id: str, blocked_ids: set[str],
    ) -> None:
        """Freeze only the waiting goal and dependency descendants.

        Unrelated goals remain runnable, so a missing field in a planning goal
        cannot truncate a parallel policy/weather goal.
        """
        blocked_ids.update(task.task_id for task in task_list if task.goal_id == goal_id)
        blocked_ids.discard(paused_task_id)
        changed = True
        while changed:
            changed = False
            for task in task_list:
                if task.task_id in blocked_ids:
                    continue
                if any(dep in blocked_ids or dep == paused_task_id for dep in task.depends_on):
                    blocked_ids.add(task.task_id)
                    changed = True

    async def _run_one(
        self,
        task: ExecutionTask,
        base_context: Dict[str, Any],
        previous: List[TaskResult],
        progress: Optional[ProgressCallback],
        lifecycle=None,
    ) -> TaskResult:
        await self._emit(progress, task_event("running", task.task_id, task.intent))
        if lifecycle is not None:
            await lifecycle.node_started(task)
        scoped_context = self._agent_context(task, base_context)
        previous_results = [self._legacy_result(item) for item in previous]
        operation_id = (
            f"{lifecycle.run_id}:{task.task_id}" if lifecycle is not None else task.task_id
        )
        runner_task = asyncio.create_task(self.agent_runner(
            agent_name=task.agent_name, context=scoped_context, reason=task.reason,
            expected_output=task.expected_output, previous_results=previous_results,
            task_params={
                "task_id": task.task_id, "intent": task.intent, "query": task.query,
                "operation_id": operation_id,
            },
            max_retries=task.max_retries,
        ))
        interrupted = False
        while not runner_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(runner_task), timeout=0.25)
            except asyncio.TimeoutError:
                if lifecycle is not None and await lifecycle.should_interrupt():
                    interrupted = True
                    try:
                        await asyncio.wait_for(asyncio.shield(runner_task), timeout=3.0)
                    except asyncio.TimeoutError:
                        runner_task.cancel()
                        await asyncio.gather(runner_task, return_exceptions=True)
                    break
        if interrupted and not runner_task.done():
            runtime = {}
        elif interrupted and runner_task.cancelled():
            runtime = {}
        else:
            runtime = await runner_task
        if interrupted and not runtime:
            result = TaskResult(
                task_id=task.task_id, goal_id=task.goal_id,
                intent=task.intent, agent_name=task.agent_name,
                status="skipped", error_code="TURN_INTERRUPTED",
                error_message="当前执行已由用户停止", display_order=task.display_order,
                operation_id=operation_id,
            )
            if lifecycle is not None:
                await lifecycle.node_finished(result)
            return result
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
            goal_id=task.goal_id,
            intent=task.intent,
            agent_name=task.agent_name,
            status=normalized_status,
            data=data,
            evidence=evidence,
            duration_sec=runtime.get("duration_sec"),
            attempts=int(runtime.get("attempts") or 1),
            error_code=error_code,
            error_message=error_message,
            operation_id=operation_id,
            display_order=task.display_order,
        )
        event_phase = "completed" if result.status == "success" else "failed"
        await self._emit(progress, task_event(event_phase, task.task_id, task.intent))
        if lifecycle is not None:
            await lifecycle.node_finished(result)
        return result

    @staticmethod
    def _agent_context(task: ExecutionTask, base_context: Dict[str, Any]) -> Dict[str, Any]:
        """Build the smallest model-visible context for one execution node.

        ``IntentGroup.query`` and ``IntentGroup.entities`` are already isolated
        by the intention layer.  Passing the request-wide routing envelope or
        duplicate query aliases to every skill only increases token usage and
        lets a child expand back into a sibling Goal.  Dependency outputs travel
        in ``previous_results`` separately; durable memory is opt-in by the
        agent that can use it.
        """
        context = {
            # Kept for existing skills during the compatibility migration.
            "agent_query": task.query,
            "key_entities": dict(task.entities),
            "active_task": {
                "task_id": task.task_id,
                "intent": task.intent,
                "query": task.query,
                "entities": dict(task.entities),
                "capabilities": list(task.capabilities),
            },
        }
        memory = base_context.get("_memory_context") or {}
        runtime = base_context.get("_agent_runtime") or {}

        # Only agents that use current-trip state or preference data receive it.
        if task.agent_name == "event_collection":
            if base_context.get("_structured_trip_input"):
                context["structured_trip_input"] = dict(base_context["_structured_trip_input"])
            context["active_trip"] = memory.get("active_trip") or {}
            context["user_preferences"] = memory.get("user_preferences") or {}
            # The collector uses prior *user facts* only to complete a current
            # trip; avoid exposing assistant text and cap it defensively.
            context["recent_dialogue"] = [
                item for item in (memory.get("recent_dialogue") or [])[-3:]
                if isinstance(item, dict) and item.get("role") == "user"
            ]
        elif task.agent_name in {"train_query", "itinerary_planning"}:
            context["user_preferences"] = memory.get("user_preferences") or {}
        elif task.agent_name == "trip_compliance":
            context["active_trip"] = memory.get("active_trip") or {}
        elif task.agent_name == "rag_knowledge":
            # Retrieval mode and trace id are runtime controls consumed by the
            # RAG implementation, not conversational context.
            context["retrieval_mode"] = runtime.get("retrieval_mode", "standard")
            context["request_id"] = runtime.get("request_id", "")

        return context

    @staticmethod
    def _pause_info(
        result: TaskResult,
        task_by_id: Dict[str, ExecutionTask],
        task_list: List[ExecutionTask],
    ) -> Optional[PauseInfo]:
        task = task_by_id.get(result.task_id)
        if task is None:
            return None
        spec = pause_spec_for_intent(task.intent)
        if spec is None or not spec.enabled or spec.pause_agent is None:
            return None
        if task.agent_name != spec.pause_agent:
            return None
        data = _nested(result.data)
        if data.get(spec.pause_field) is not False:
            return None
        remaining = [
            step.model_dump(mode="json")
            for step in task_list
            if step.goal_id == task.goal_id and step.priority > task.priority
        ]
        return PauseInfo(
            intent=task.intent,
            goal_id=task.goal_id,
            node_id=task.task_id,
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
    ) -> List[TaskResult]:
        failed_task = task_by_id.get(failed.task_id)
        if failed_task is None:
            return []
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
            if task.goal_id == failed_task.goal_id and task.priority > failed_task.priority:
                downstream.add(task.task_id)

        executed = {item.task_id for item in results}
        newly_skipped: List[TaskResult] = []
        for task_id in downstream:
            if task_id in executed or task_id in skipped:
                continue
            task = task_by_id.get(task_id)
            if task is None:
                continue
            skipped.add(task_id)
            skipped_result = TaskResult(
                task_id=task_id, goal_id=task.goal_id,
                intent=task.intent,
                agent_name=task.agent_name,
                status="skipped",
                error_code="UPSTREAM_DEPENDENCY_FAILED",
                error_message="依赖的上游任务失败，未执行",
                display_order=task.display_order,
            )
            results.append(skipped_result)
            newly_skipped.append(skipped_result)
        return newly_skipped

    @staticmethod
    def _legacy_result(result: TaskResult) -> Dict[str, Any]:
        return {
            "task_id": result.task_id,
            "goal_id": result.goal_id,
            "intent": result.intent,
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
