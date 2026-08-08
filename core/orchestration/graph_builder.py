"""Compile semantic tasks into trusted execution tasks and dependency batches.

One IntentTask expands into the full skill execution template (one
ExecutionTask per step, ``task_id=f"{intent}-{agent}"``). Workflow intents
absorb standalone intents whose agent set is a subset of their own
(subsumption), so overlapping requests never double-execute an agent and each
step's query stays scope-pure. Cross-intent ``depends_on`` edges connect the
referenced intent's terminal step to this intent's first step.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from core.intent_catalog import (
    agent_for_intent,
    execution_steps_for_intent,
)

from .models import ExecutionTask, IntentTask

# 模板占位符 -> 可能的实体键（trip_intake 与识别层用词统一）。
_PLACEHOLDER_KEYS = {
    "origin": ("origin",),
    "destination": ("destination",),
    "start_date": ("start_date",),
    "duration": ("duration", "trip_length", "duration_days"),
    "purpose": ("purpose", "trip_purpose"),
}


class TaskGraphBuilder:
    def compile(self, tasks: Iterable[IntentTask]) -> List[ExecutionTask]:
        task_list = list(tasks)
        plans: List[Tuple[IntentTask, List]] = []
        for task in task_list:
            steps = execution_steps_for_intent(task.intent)
            if not steps:
                raise ValueError(f"no execution template for intent: {task.intent}")
            plans.append((task, steps))

        coverage = {
            task.intent: {step.agent_name for step in steps} for task, steps in plans
        }

        # subsumption：独立意图的 agent 集合 ⊆ 某 workflow 的 agent 集合 → 折叠。
        query_overrides: Dict[str, str] = {}
        entities_overrides: Dict[str, Dict[str, object]] = {}
        survivors: List[Tuple[IntentTask, List]] = []
        for task, steps in plans:
            workflow = next(
                (
                    other
                    for other, _ in plans
                    if other.intent != task.intent
                    and coverage[task.intent]
                    and coverage[task.intent] <= coverage[other.intent]
                ),
                None,
            )
            if workflow is not None:
                for step in steps:
                    query_overrides[step.agent_name] = task.query
                    entities_overrides[step.agent_name] = dict(task.entities)
            else:
                survivors.append((task, steps))

        execution: List[ExecutionTask] = []
        terminal_by_agent: Dict[str, str] = {}
        terminal_by_intent: Dict[str, str] = {}
        counter = 0
        for task, steps in survivors:
            terminal = max(steps, key=lambda step: step.priority)
            for step in steps:
                agent = step.agent_name
                if len(steps) == 1:
                    # 单步意图：语义任务 query 本身就是 scoped ask（LLM 或 fallback 生成）。
                    query = task.query
                    entities = dict(task.entities)
                elif agent in query_overrides:
                    query = query_overrides[agent]
                    entities = {**task.entities, **entities_overrides.get(agent, {})}
                else:
                    query = self._render_query(step.query, task.entities) or task.query
                    entities = dict(task.entities)
                step_id = f"{task.intent}-{agent}"
                execution.append(ExecutionTask(
                    **task.model_dump(exclude={
                        "task_id", "query", "entities", "depends_on",
                        "side_effect", "failure_policy", "display_order",
                    }),
                    task_id=step_id,
                    query=query,
                    entities=entities,
                    agent_name=agent,
                    priority=step.priority,
                    reason=step.reason or "",
                    expected_output=step.expected_output or "",
                    max_retries=step.max_retries,
                    # 模板拥有失败策略与结果判定规则；LLM 的 advisory 字段被覆盖。
                    failure_policy=step.on_failure,
                    side_effect=task.side_effect and step is terminal,
                    result_rules=dict(step.result_rules or {}),
                    display_order=counter,
                ))
                counter += 1
                terminal_by_agent[agent] = step_id
                if step is terminal:
                    terminal_by_intent[task.intent] = step_id

        # 跨 intent depends_on 边：被引用意图的终点步骤 → 本意图的起点步骤。
        for task, steps in survivors:
            deps = [
                self._resolve_dep_target(target, terminal_by_intent, terminal_by_agent)
                for target in task.depends_on
            ]
            if not deps:
                continue
            first = min(steps, key=lambda step: step.priority)
            first_id = f"{task.intent}-{first.agent_name}"
            for execution_task in execution:
                if execution_task.task_id == first_id:
                    execution_task.depends_on = sorted(
                        set(execution_task.depends_on) | set(deps)
                    )
                    break

        self.batches(execution)
        return execution

    @staticmethod
    def _resolve_dep_target(
        target: str,
        terminal_by_intent: Dict[str, str],
        terminal_by_agent: Dict[str, str],
    ) -> str:
        if target in terminal_by_intent:
            return terminal_by_intent[target]
        primary = agent_for_intent(target)
        if primary and primary in terminal_by_agent:
            return terminal_by_agent[primary]
        raise ValueError(f"depends_on target has no executable step: {target}")

    @classmethod
    def _render_query(cls, template: Optional[str], entities: Dict[str, object]) -> str:
        """渲染步骤级 scoped query 模板；空占位符段（出发地=、日期=）被剔除。

        静态模板（不含占位符）返回空串，交由语义任务 query 携带上下文。
        """
        if not template or "{" not in template:
            return ""
        query = template
        for placeholder, keys in _PLACEHOLDER_KEYS.items():
            value = next((entities.get(key) for key in keys if entities.get(key)), "")
            query = query.replace("{" + placeholder + "}", str(value))
        tokens = [
            part for part in query.split()
            if part and not part.endswith("=") and not part.endswith("＝")
        ]
        cleaned = " ".join(tokens).strip()
        cleaned = re.sub(r"\s*→\s*", "→", cleaned)
        # “、”-连接的中文模板：空占位符残留的“标签=”片段连同分隔符一并删除。
        pieces = re.split(r"([，、])", cleaned)
        out = []
        skip_next_sep = False
        for piece in pieces:
            if re.fullmatch(r"[，、]", piece):
                if not skip_next_sep:
                    out.append(piece)
                skip_next_sep = False
            elif re.search(r"[=＝]$", piece):
                skip_next_sep = True
            else:
                out.append(piece)
        return "".join(out).strip()

    @staticmethod
    def batches(tasks: Iterable[ExecutionTask]) -> List[List[ExecutionTask]]:
        pending: Dict[str, ExecutionTask] = {task.task_id: task for task in tasks}
        known = set(pending)
        for task in pending.values():
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(f"unknown task dependencies for {task.task_id}: {sorted(missing)}")

        completed = set()
        result = []
        while pending:
            dependency_ready = [
                task for task in pending.values()
                if set(task.depends_on) <= completed
            ]
            if not dependency_ready:
                raise ValueError("task dependency graph contains a cycle")
            next_priority = min(task.priority for task in dependency_ready)
            ready = [task for task in dependency_ready if task.priority == next_priority]
            ready.sort(key=lambda task: (task.priority, task.display_order, task.task_id))
            result.append(ready)
            for task in ready:
                completed.add(task.task_id)
                pending.pop(task.task_id)
        return result
