"""Compile semantic Goals into trusted execution nodes with explicit edges."""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from core.intent_catalog import execution_steps_for_intent

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
        plans = []
        for task in task_list:
            steps = execution_steps_for_intent(task.intent)
            if not steps:
                raise ValueError(f"no execution template for intent: {task.intent}")
            plans.append((task, steps))

        # A user-requested single-step Goal can satisfy the same capability
        # inside a workflow.  Compile it once under its own Goal; workflow
        # downstream nodes depend on it explicitly instead of stealing its
        # intent/query identity as the old subsumption logic did.
        standalone_by_agent: Dict[str, List[tuple[IntentTask, object]]] = {}
        for task, steps in plans:
            if len(steps) == 1:
                standalone_by_agent.setdefault(steps[0].agent_name, []).append((task, steps[0]))

        execution: List[ExecutionTask] = []
        by_id: Dict[str, ExecutionTask] = {}
        first_by_goal: Dict[str, str] = {}
        terminal_by_goal: Dict[str, str] = {}
        terminal_by_intent: Dict[str, str] = {}
        intent_counts: Dict[str, int] = {}
        for task in task_list:
            intent_counts[task.intent] = intent_counts.get(task.intent, 0) + 1
        counter = 0

        def add_node(owner: IntentTask, step) -> ExecutionTask:
            nonlocal counter
            node_id = f"{owner.task_id}-{step.agent_name}"
            existing = by_id.get(node_id)
            if existing is not None:
                return existing
            owner_steps = execution_steps_for_intent(owner.intent)
            first_priority = min(item.priority for item in owner_steps)
            if len(owner_steps) == 1 or step.priority == first_priority:
                # Intake must see the complete scoped planning Goal so it can
                # extract facts. Later capability phases receive only their
                # declared query plus upstream structured results.
                query = owner.query
            else:
                query = self._render_query(step.query, owner.entities) or step.reason
            node = ExecutionTask(
                **owner.model_dump(exclude={
                    "task_id", "query", "entities", "depends_on",
                    "side_effect", "failure_policy", "display_order",
                }),
                task_id=node_id, goal_id=owner.task_id,
                query=query, entities=dict(owner.entities),
                agent_name=step.agent_name, priority=step.priority,
                reason=step.reason or "", expected_output=step.expected_output or "",
                max_retries=step.max_retries, failure_policy=step.on_failure,
                side_effect=(
                    owner.side_effect
                    and step.priority == max(
                        item.priority for item in owner_steps
                    )
                ),
                result_rules=dict(step.result_rules or {}), display_order=counter,
            )
            counter += 1
            execution.append(node)
            by_id[node_id] = node
            return node

        # Standalone Goals are first-class and always keep their own scoped query.
        for task, steps in plans:
            if len(steps) == 1:
                node = add_node(task, steps[0])
                first_by_goal[task.task_id] = node.task_id
                terminal_by_goal[task.task_id] = node.task_id

        # Compile workflow-owned nodes and wire every phase to all preceding
        # workflow requirements. Substituted standalone nodes remain independent
        # (so they can finish while intake waits) but are prerequisites of plan.
        for task, steps in plans:
            if len(steps) == 1:
                continue
            refs_by_priority: Dict[int, List[str]] = {}
            for step in steps:
                substitutes = standalone_by_agent.get(step.agent_name, [])
                substitute = next((
                    item for item in substitutes
                    if item[0].task_id != task.task_id
                    # Capability sharing is valid only inside the same
                    # decomposition cohort.  A Goal added in a later Turn must
                    # never retroactively become a dependency of an old Goal.
                    and (
                        item[0].group_id == task.group_id
                        and (bool(task.group_id) or not item[0].group_id)
                    )
                ), None)
                if substitute is not None:
                    ref_id = f"{substitute[0].task_id}-{step.agent_name}"
                else:
                    ref_id = add_node(task, step).task_id
                refs_by_priority.setdefault(step.priority, []).append(ref_id)
            ordered_prior: List[str] = []
            for priority in sorted(refs_by_priority):
                current = refs_by_priority[priority]
                for node_id in current:
                    node = by_id[node_id]
                    if node.goal_id == task.task_id:
                        node.depends_on = sorted(set(node.depends_on) | set(ordered_prior))
                ordered_prior.extend(current)
            first_by_goal[task.task_id] = refs_by_priority[min(refs_by_priority)][0]
            terminal_by_goal[task.task_id] = refs_by_priority[max(refs_by_priority)][-1]

        for task in task_list:
            if intent_counts[task.intent] == 1:
                terminal_by_intent[task.intent] = terminal_by_goal[task.task_id]

        # Semantic cross-Goal dependencies target Goal ids first; intent aliases
        # are accepted only when unambiguous for backward-compatible LLM output.
        for task in task_list:
            deps = [
                self._resolve_dep_target(target, terminal_by_goal, terminal_by_intent)
                for target in task.depends_on
            ]
            if not deps:
                continue
            first_id = first_by_goal[task.task_id]
            by_id[first_id].depends_on = sorted(set(by_id[first_id].depends_on) | set(deps))

        self.batches(execution)
        return execution

    @staticmethod
    def _resolve_dep_target(
        target: str,
        terminal_by_goal: Dict[str, str],
        terminal_by_intent: Dict[str, str],
    ) -> str:
        if target in terminal_by_goal:
            return terminal_by_goal[target]
        if target in terminal_by_intent:
            return terminal_by_intent[target]
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
