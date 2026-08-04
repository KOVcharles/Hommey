"""Compile semantic tasks into trusted execution tasks and dependency batches."""
from __future__ import annotations

from typing import Dict, Iterable, List

from core.schedule_builder import SCHEDULE_RULES

from .models import ExecutionTask, IntentTask


class TaskGraphBuilder:
    def compile(self, tasks: Iterable[IntentTask]) -> List[ExecutionTask]:
        execution_tasks = []
        for task in tasks:
            rules = SCHEDULE_RULES.get(task.intent) or []
            if len(rules) != 1:
                raise ValueError(
                    f"phase-one intent must map to exactly one execution step: {task.intent}"
                )
            rule = rules[0]
            execution_tasks.append(ExecutionTask(
                **task.model_dump(),
                agent_name=rule["agent_name"],
                priority=int(rule.get("priority", 1)),
                reason=rule.get("reason", ""),
                expected_output=rule.get("expected_output", ""),
                max_retries=int(rule.get("max_retries", 0)),
            ))
        self.batches(execution_tasks)
        return execution_tasks

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
