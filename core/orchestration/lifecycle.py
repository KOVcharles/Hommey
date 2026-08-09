"""State-machine adapter used by the executor at durable boundaries."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .models import TaskResult
from .state import WaitState, WorkflowRunState, derive_goal_status
from .state_store import OrchestrationStateStore


class ExecutionLifecycle:
    def __init__(self, store: OrchestrationStateStore, state: WorkflowRunState):
        self.store = store
        self.run_id = state.run_id
        self.turn_id = state.current_turn_id or ""

    async def node_started(self, task) -> None:
        def apply(state):
            node = state.nodes[task.task_id]
            node.status = "RUNNING"
            node.attempts += 1
            goal = state.goals.get(node.goal_id)
            if goal:
                self._reconcile_goal(state, goal.goal_id)
        await self.store.mutate(self.run_id, apply)

    async def node_finished(self, result: TaskResult) -> None:
        def apply(state):
            node = state.nodes[result.task_id]
            if result.error_code == "TURN_INTERRUPTED":
                node.status = "INTERRUPTED"
            else:
                node.status = {
                    "success": "SUCCEEDED", "error": "FAILED", "skipped": "SKIPPED",
                }[result.status]
            node.result = result.model_dump(mode="json")
            node.error_code = result.error_code
            self._reconcile_goal(state, node.goal_id)
        await self.store.mutate(self.run_id, apply)

    async def mark_waiting(self, pause_infos) -> None:
        pause_infos = list(pause_infos)
        if not pause_infos:
            return
        def apply(state):
            waits = []
            paused_goal_ids = set()
            for pause_info in pause_infos:
                goal_id = pause_info.goal_id or pause_info.intent
                paused_goal_ids.add(goal_id)
                goal = state.goals.get(goal_id)
                if goal:
                    goal.status = "WAITING_USER"
                    goal.expected_inputs = list(
                        (pause_info.collected_facts.get("missing_fields") or [])
                        if isinstance(pause_info.collected_facts, dict) else []
                    )
                node_id = pause_info.node_id
                if node_id in state.nodes:
                    state.nodes[node_id].status = "WAITING_USER"
                waits.append(WaitState(
                    goal_id=goal_id,
                    expected_fields=list(goal.expected_inputs if goal else []),
                    pause_agent=pause_info.pause_agent,
                ))
            # 合并而非覆盖：上一轮已暂停的兄弟 goal 的 WaitState 必须保留
            # （不变量：waits 含每个 WAITING_USER goal 的 WaitState）。
            preserved = [
                wait for wait in state.waits
                if wait.goal_id not in paused_goal_ids
            ]
            state.focused_goal_id = pause_infos[0].goal_id or pause_infos[0].intent
            state.status = "WAITING_USER"
            state.waits = waits + preserved
            self._reconcile_goals(state)
            # 兜底：reconcile 后不再是 WAITING_USER 的残留 wait 一律清除（防 stale）。
            state.waits = [
                wait for wait in state.waits
                if state.goals.get(wait.goal_id)
                and state.goals[wait.goal_id].status == "WAITING_USER"
            ]
            state.expires_at = (
                datetime.now(timezone.utc) + timedelta(days=30)
            ).isoformat()
        await self.store.mutate(self.run_id, apply)
        await self.store.set_turn_status(self.turn_id, "COMPLETED")

    async def should_interrupt(self) -> bool:
        return await self.store.should_interrupt(self.run_id, self.turn_id)

    async def mark_interrupted(self) -> None:
        def apply(state):
            state.status = "INTERRUPTED"
            state.expires_at = (
                datetime.now(timezone.utc) + timedelta(days=30)
            ).isoformat()
            for node in state.nodes.values():
                if node.status == "RUNNING":
                    node.status = "INTERRUPTED"
            self._reconcile_goals(state)
            # READY downstream nodes have not failed; the Goal is nevertheless
            # resumable because the user stopped the Turn before it finished.
            for goal_id in state.current_goal_ids:
                goal = state.goals.get(goal_id)
                if goal and goal.status in {"PENDING", "RUNNING"}:
                    goal.status = "INTERRUPTED"
        await asyncio.shield(self.store.mutate(self.run_id, apply))
        await asyncio.shield(self.store.set_turn_status(self.turn_id, "INTERRUPTED"))

    async def mark_completed(
        self, *, failed: bool = False,
        delivered_goal_ids: set[str] | None = None,
    ) -> None:
        def apply(state):
            for goal_id in delivered_goal_ids or set():
                goal = state.goals.get(goal_id)
                if goal:
                    goal.answer_delivered = True
            self._reconcile_goals(state)
            if failed:
                state.status = "FAILED"
            elif any(goal.status == "WAITING_USER" for goal in state.goals.values()):
                state.status = "WAITING_USER"
                waiting_ids = [
                    wait.goal_id for wait in state.waits
                    if state.goals.get(wait.goal_id)
                    and state.goals[wait.goal_id].status == "WAITING_USER"
                ]
                if waiting_ids:
                    state.focused_goal_id = waiting_ids[0]
            elif any(goal.status == "INTERRUPTED" for goal in state.goals.values()):
                state.status = "INTERRUPTED"
            else:
                state.status = "COMPLETED"
                state.waits = []
            retention_days = 30 if state.status in {"WAITING_USER", "INTERRUPTED"} else 90
            state.expires_at = (
                datetime.now(timezone.utc) + timedelta(days=retention_days)
            ).isoformat()
        await self.store.mutate(self.run_id, apply)
        await self.store.set_turn_status(self.turn_id, "FAILED" if failed else "COMPLETED")

    @staticmethod
    def _reconcile_goal(state, goal_id: str) -> None:
        goal = state.goals.get(goal_id)
        if goal is None or goal.status == "ABANDONED":
            return
        nodes = [node for node in state.nodes.values() if node.goal_id == goal_id]
        goal.status = derive_goal_status(nodes)

    @classmethod
    def _reconcile_goals(cls, state) -> None:
        for goal_id in state.goals:
            cls._reconcile_goal(state, goal_id)
