"""Scoped task-DAG pipeline for all skill-backed intents."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from core.presentation import AnswerDocument

from .composer import AnswerComposer
from .capabilities import apply_capability_selection
from .decomposer import TaskDecomposer
from .events import phase_event
from .executor import ProgressCallback, TaskExecutor
from .graph_builder import TaskGraphBuilder
from .models import ExecutionTask, IntentTask, PipelineOutput, TaskResult
from .lifecycle import ExecutionLifecycle
from .state import WorkflowRunState
from .validator import TaskValidator

logger = logging.getLogger(__name__)


class MultiIntentPipeline:
    """One entry point: decompose → validate → compile → execute → compose.

    Durable state is an optional seam so pure pipeline tests need no database.
    In production the state store is the only owner of Run/Turn/Goal/Node state.
    """

    def __init__(
        self,
        *,
        model,
        composer_model,
        agent_runner,
        memory_hooks=None,
        state_store=None,
    ):
        self.decomposer = TaskDecomposer(model)
        self.validator = TaskValidator()
        self.graph_builder = TaskGraphBuilder()
        self.executor = TaskExecutor(agent_runner)
        self.composer = AnswerComposer(composer_model)
        self.memory_hooks = memory_hooks
        self.state_store = state_store

    async def run(
        self,
        original_query: str,
        intention_data: Dict[str, Any],
        base_context: Dict[str, Any],
        progress: Optional[ProgressCallback] = None,
        task_query: Optional[str] = None,
        session_id: str = "",
        request_id: str = "",
        existing_state: Optional[WorkflowRunState] = None,
    ) -> PipelineOutput:
        await self._emit(progress, phase_event("decomposing", "tasks_decomposing"))
        decomposition_query = task_query or original_query
        base_context = dict(base_context)
        base_context.setdefault("original_query", original_query)
        base_context.setdefault("agent_query", decomposition_query)
        stable_request_id = request_id or f"request-{hashlib.sha256(original_query.encode()).hexdigest()[:16]}"
        # Crash replay of the same request must reuse the persisted Goal/Node
        # identities and stable operation ids, rather than append a duplicate
        # Goal whose lifecycle has no matching NodeState.
        if (
            existing_state is not None
            and existing_state.current_request_id == stable_request_id
        ):
            return await self.resume_run(
                existing_state, original_query, base_context,
                progress=progress, request_id=stable_request_id,
            )
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

        # Explicit user opt-outs are deterministic request constraints, not
        # new intents. Applying them after validation keeps the recognition
        # authority boundary unchanged and persists them with semantic tasks.
        tasks = apply_capability_selection(tasks, original_query)

        if existing_state is not None:
            tasks = self._namespace_new_goals(tasks, existing_state, request_id or original_query)
        for task in tasks:
            if not task.group_id:
                task.group_id = stable_request_id

        execution_tasks = self.graph_builder.compile(tasks)
        node_goals = {task.task_id: task.goal_id for task in execution_tasks}
        lifecycle = None
        if self.state_store is not None and (session_id or existing_state is not None):
            if existing_state is None:
                state = await self.state_store.create_run(
                    session_id=session_id, request_id=stable_request_id,
                    original_query=original_query, intention_data=intention_data,
                    semantic_tasks=[task.model_dump(mode="json") for task in tasks],
                    node_ids=[task.task_id for task in execution_tasks],
                    node_goals=node_goals,
                    graph_hash=self._graph_hash(execution_tasks),
                    skill_versions=self._skill_versions(tasks),
                )
            else:
                combined_semantic = [
                    IntentTask.model_validate(item) for item in existing_state.semantic_tasks
                ] + tasks
                combined_execution = self.graph_builder.compile(combined_semantic)
                state = await self.state_store.add_goals(
                    existing_state.run_id, request_id=stable_request_id, text=original_query,
                    intention_data=intention_data,
                    semantic_tasks=[task.model_dump(mode="json") for task in tasks],
                    node_ids=[task.task_id for task in execution_tasks],
                    node_goals=node_goals,
                    graph_hash=self._graph_hash(combined_execution),
                    skill_versions=self._skill_versions(tasks),
                )
            lifecycle = ExecutionLifecycle(self.state_store, state)
        try:
            results, pause_infos = await self.executor.execute(
                execution_tasks, base_context, progress, lifecycle=lifecycle
            )
        except asyncio.CancelledError:
            if lifecycle is not None:
                await lifecycle.mark_interrupted()
            raise
        except Exception:
            if lifecycle is not None:
                await lifecycle.mark_completed(failed=True)
            raise

        if pause_infos:
            if lifecycle is not None:
                await lifecycle.mark_waiting(pause_infos)
            # 兄弟 goal 暂停时，本轮已 SUCCEEDED 的结果必须在返回前回写记忆；
            # 否则 resume 按 previous_ids 过滤后这些结果会被永久跳过（P1-6）。
            if self.memory_hooks is not None:
                await self.memory_hooks.apply(results)
            return await self._paused_output(
                tasks, execution_tasks, results, pause_infos, progress,
            )

        if lifecycle is not None and await lifecycle.should_interrupt():
            await lifecycle.mark_interrupted()
            return PipelineOutput(
                tasks=tasks, execution_tasks=execution_tasks,
                results=results, interrupted=True,
            )

        await self._emit(progress, phase_event("composing", "answer_composing"))
        hard_failure = self._has_hard_failure(execution_tasks, results)
        try:
            answer_document, compose_interrupted = await self._compose_interruptibly(
                original_query, tasks, results, lifecycle,
            )
            if compose_interrupted:
                await lifecycle.mark_interrupted()
                return PipelineOutput(
                    tasks=tasks, execution_tasks=execution_tasks,
                    results=results, interrupted=True,
                )
            if self.memory_hooks is not None:
                await self.memory_hooks.apply(results)
            if lifecycle is not None:
                if await lifecycle.should_interrupt():
                    await lifecycle.mark_interrupted()
                    return PipelineOutput(
                        tasks=tasks, execution_tasks=execution_tasks,
                        results=results, interrupted=True,
                    )
                await lifecycle.mark_completed(
                    failed=hard_failure,
                    delivered_goal_ids=(
                        set() if hard_failure else {task.task_id for task in tasks}
                    ),
                )
        except asyncio.CancelledError:
            if lifecycle is not None:
                await lifecycle.mark_interrupted()
            raise
        except Exception:
            if lifecycle is not None:
                await lifecycle.mark_completed(failed=True)
            raise
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
        pause_infos,
        progress: Optional[ProgressCallback],
    ) -> PipelineOutput:
        """暂停后交给 presentation；状态已在 durable boundary 保存。"""
        pause_info = pause_infos[0]
        presentation_document = self._build_presentation(pause_info)
        await self._emit(progress, phase_event("done", "trip_details_needed"))
        return PipelineOutput(
            tasks=tasks,
            execution_tasks=execution_tasks,
            results=results,
            paused=True,
            pause_info=pause_info,
            pause_infos=pause_infos,
            presentation_document=presentation_document,
        )

    async def resume_run(
        self,
        state: WorkflowRunState,
        original_query: str,
        base_context: Dict[str, Any],
        progress: Optional[ProgressCallback] = None,
        request_id: str = "",
    ) -> PipelineOutput:
        """Start a new turn on the same durable run and skip committed nodes."""
        if self.state_store is None:
            raise RuntimeError("resume requires a state store")
        base_context = dict(base_context)
        base_context.setdefault("original_query", original_query)
        base_context.setdefault("agent_query", original_query)
        if state.status == "WAITING_USER":
            # A slot answer belongs only to the focused Goal.  Resuming every
            # waiting Goal with the same short value corrupts multi-Goal runs.
            focused = state.goals.get(state.focused_goal_id or "")
            if focused is not None and focused.status == "WAITING_USER":
                target_goal_ids = {focused.goal_id}
            else:
                target_goal_ids = {state.waits[0].goal_id} if state.waits else set()
        else:
            target_goal_ids = {
                goal.goal_id for goal in state.goals.values()
                if goal.status == "INTERRUPTED"
            }
        state = await self.state_store.start_turn(
            state.run_id,
            request_id or f"resume-{hashlib.sha256(original_query.encode()).hexdigest()[:16]}",
            original_query, goal_ids=sorted(target_goal_ids),
        )
        await self._emit(progress, phase_event("decomposing", "tasks_decomposing"))
        tasks = [IntentTask.model_validate(item) for item in state.semantic_tasks]
        revised_goals = set(state.current_goal_ids)
        explicit_continue = "".join(original_query.strip().lower().split()) in {
            "继续", "接着做", "继续执行", "继续上次任务", "接着上次做",
            "恢复任务", "继续吧", "接着来", "继续规划",
        }
        for task in tasks:
            if task.task_id not in revised_goals:
                continue
            collected = {}
            for node in state.nodes.values():
                if node.goal_id != task.task_id or not node.result:
                    continue
                raw_data = node.result.get("data")
                if isinstance(raw_data, dict):
                    collected.update(raw_data)
            task.entities = {**collected, **task.entities}
            if not explicit_continue:
                task.query = original_query
        execution_tasks = self.graph_builder.compile(tasks)
        for execution_task in execution_tasks:
            if execution_task.goal_id in revised_goals:
                waiting_node = state.nodes.get(execution_task.task_id)
                if waiting_node is not None and waiting_node.status == "READY":
                    if not explicit_continue:
                        execution_task.query = original_query
                    break
        current_graph_hash = self._graph_hash(execution_tasks)
        if state.schema_version < 2:
            node_goals = {task.task_id: task.goal_id for task in execution_tasks}
            def upgrade(old):
                old.schema_version = 2
                old.graph_hash = current_graph_hash
                for node_id, goal_id in node_goals.items():
                    if node_id in old.nodes:
                        old.nodes[node_id].goal_id = goal_id
            state = await self.state_store.mutate(state.run_id, upgrade)
        if current_graph_hash != state.graph_hash:
            raise RuntimeError("workflow definition changed; the interrupted run cannot be resumed safely")
        previous = []
        for node in state.nodes.values():
            if node.status not in {"SUCCEEDED", "SKIPPED"} or not node.result:
                continue
            result = TaskResult.model_validate(node.result)
            # schema v1 persisted results before Goal ownership existed.  The
            # node has just been upgraded above, so repair the in-memory result
            # as well; otherwise resume composition silently drops it.
            if not result.goal_id:
                result.goal_id = node.goal_id
            previous.append(result)
        lifecycle = ExecutionLifecycle(self.state_store, state)
        previous_ids = {result.task_id for result in previous}
        try:
            results, pause_infos = await self.executor.execute(
                execution_tasks, base_context, progress,
                lifecycle=lifecycle, previous_results=previous,
                active_goal_ids=revised_goals,
            )
        except asyncio.CancelledError:
            await lifecycle.mark_interrupted()
            raise
        except Exception:
            await lifecycle.mark_completed(failed=True)
            raise

        if pause_infos:
            await lifecycle.mark_waiting(pause_infos)
            # 暂停轮同样要回写本轮新增结果的记忆；previous_ids 过滤已经排除了
            # 历史 SUCCEEDED 结果，天然防重放（P1-6）。
            if self.memory_hooks is not None:
                await self.memory_hooks.apply(
                    result for result in results if result.task_id not in previous_ids
                )
            return await self._paused_output(
                tasks, execution_tasks, results, pause_infos, progress,
            )
        if await lifecycle.should_interrupt():
            await lifecycle.mark_interrupted()
            return PipelineOutput(
                tasks=tasks, execution_tasks=execution_tasks,
                results=results, interrupted=True,
            )
        answer_goal_ids = set(revised_goals) | {
            goal.goal_id for goal in state.goals.values()
            if not goal.answer_delivered and goal.status in {"RUNNING", "SUCCEEDED", "FAILED"}
        }
        answer_tasks = [task for task in tasks if task.task_id in answer_goal_ids]
        answer_results = [result for result in results if result.goal_id in answer_goal_ids]
        answer_execution = [
            task for task in execution_tasks if task.goal_id in answer_goal_ids
        ]
        composition_queries = [state.original_query] + [
            state.goals[goal_id].query for goal_id in answer_goal_ids
            if goal_id in state.goals and state.goals[goal_id].query
        ]
        composition_query = "\n".join(dict.fromkeys(composition_queries)) or state.original_query
        await self._emit(progress, phase_event("composing", "answer_composing"))
        hard_failure = self._has_hard_failure(answer_execution, answer_results)
        try:
            # A resume Turn may contain only a slot value such as “1天”.
            # Composition must still answer the Run's complete original ask
            # (weather + policy + plan), using the new value from node results.
            answer_document, compose_interrupted = await self._compose_interruptibly(
                composition_query, answer_tasks, answer_results, lifecycle,
            )
            if compose_interrupted:
                await lifecycle.mark_interrupted()
                return PipelineOutput(
                    tasks=tasks, execution_tasks=execution_tasks,
                    results=results, interrupted=True,
                )
            if self.memory_hooks is not None:
                await self.memory_hooks.apply(
                    result for result in results if result.task_id not in previous_ids
                )
            if await lifecycle.should_interrupt():
                await lifecycle.mark_interrupted()
                return PipelineOutput(
                    tasks=tasks, execution_tasks=execution_tasks,
                    results=results, interrupted=True,
                )
            await lifecycle.mark_completed(
                failed=hard_failure,
                delivered_goal_ids=set() if hard_failure else answer_goal_ids,
            )
        except asyncio.CancelledError:
            await lifecycle.mark_interrupted()
            raise
        except Exception:
            await lifecycle.mark_completed(failed=True)
            raise
        await self._emit(progress, phase_event("done", "answer_ready"))
        return PipelineOutput(
            tasks=answer_tasks,
            execution_tasks=answer_execution,
            results=answer_results,
            answer_document=answer_document,
        )

    @staticmethod
    def _graph_hash(tasks: List[ExecutionTask]) -> str:
        payload = [
            {
                "id": task.task_id, "agent": task.agent_name,
                "goal_id": task.goal_id,
                "capabilities": task.capabilities,
                "priority": task.priority, "depends_on": task.depends_on,
                "failure_policy": task.failure_policy,
                "max_retries": task.max_retries,
            }
            for task in tasks
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _skill_versions(tasks: List[IntentTask]) -> Dict[str, str]:
        from core.intent_catalog import intent_to_skill
        from utils.skill_loader import SkillLoader
        definitions = SkillLoader().load_definitions()
        versions = {}
        for task in tasks:
            skill = intent_to_skill(task.intent)
            if skill and skill in definitions:
                versions[skill] = definitions[skill].version
        return versions

    @staticmethod
    def _has_hard_failure(tasks: List[ExecutionTask], results: List[TaskResult]) -> bool:
        by_id = {task.task_id: task for task in tasks}
        return any(
            result.status == "error"
            and by_id.get(result.task_id) is not None
            and by_id[result.task_id].failure_policy == "abort"
            for result in results
        )

    @staticmethod
    def _namespace_new_goals(
        tasks: List[IntentTask], state: WorkflowRunState, seed: str,
    ) -> List[IntentTask]:
        """Keep Goal identity distinct from intent across turns in one Run."""
        existing = set(state.goals)
        mapping: Dict[str, str] = {}
        for index, task in enumerate(tasks):
            if task.task_id not in existing and task.task_id not in mapping.values():
                continue
            suffix = hashlib.sha256(
                f"{seed}:{task.task_id}:{index}".encode()
            ).hexdigest()[:8]
            mapping[task.task_id] = f"{task.intent[:54]}_{suffix}"
        if not mapping:
            return tasks
        renamed = []
        for task in tasks:
            data = task.model_dump(mode="json")
            data["task_id"] = mapping.get(task.task_id, task.task_id)
            data["depends_on"] = [mapping.get(dep, dep) for dep in task.depends_on]
            renamed.append(IntentTask.model_validate(data))
        return renamed

    async def _compose_interruptibly(
        self, original_query: str, tasks, results,
        lifecycle: Optional[ExecutionLifecycle],
    ):
        """Apply the same stop/grace contract to answer composition."""
        if lifecycle is None:
            return await self.composer.compose(original_query, tasks, results), False
        compose_task = asyncio.create_task(
            self.composer.compose(original_query, tasks, results)
        )
        while not compose_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(compose_task), timeout=0.25)
            except asyncio.TimeoutError:
                if not await lifecycle.should_interrupt():
                    continue
                try:
                    await asyncio.wait_for(asyncio.shield(compose_task), timeout=3.0)
                except asyncio.TimeoutError:
                    compose_task.cancel()
                    await asyncio.gather(compose_task, return_exceptions=True)
                return None, True
        return await compose_task, False

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
