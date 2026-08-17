"""PostgreSQL-authoritative orchestration snapshots with a local file fallback."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading
from typing import Callable, Optional
import uuid

from settings import MEMORY_CONFIG
from utils.io_executor import run_blocking

from .state import (
    GoalState,
    NodeState,
    WorkflowRunState,
    WorkflowTurn,
    default_expiry,
    utc_now,
)


_RESUMABLE = ("ACTIVE", "WAITING_USER", "INTERRUPTING", "INTERRUPTED")


class StateConflictError(RuntimeError):
    pass


class OrchestrationStateStore:
    def __init__(self, user_id: str, postgres_dsn: str | None = None, storage_dir: str | None = None):
        self.user_id = user_id
        backend = MEMORY_CONFIG.get("long_term", {}).get("backend", "file")
        self.postgres_dsn = postgres_dsn if postgres_dsn is not None else (
            MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
        )
        self._postgres = bool(self.postgres_dsn) and (postgres_dsn is not None or backend == "postgres")
        configured_dir = (
            storage_dir
            or MEMORY_CONFIG.get("long_term", {}).get("storage_dir")
            or "data/memory"
        )
        base = Path(configured_dir)
        safe_user = "".join(ch for ch in user_id if ch.isalnum() or ch in "-_") or "user"
        self._dir = base / "orchestration" / safe_user
        self._file_lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return self._postgres

    async def create_run(
        self, *, session_id: str, request_id: str, original_query: str,
        intention_data: dict, semantic_tasks: list[dict], node_ids: list[str],
        graph_hash: str, skill_versions: dict[str, str],
        node_goals: dict[str, str] | None = None,
    ) -> WorkflowRunState:
        run_id = f"run_{uuid.uuid4().hex}"
        turn_id = f"turn_{uuid.uuid4().hex}"
        goals = {
            task["task_id"]: {
                "goal_id": task["task_id"], "intent": task["intent"],
                "status": "RUNNING", "query": task.get("query", ""),
            }
            for task in semantic_tasks
        }
        state = WorkflowRunState(
            run_id=run_id, user_id=self.user_id, session_id=session_id,
            current_turn_id=turn_id, current_request_id=request_id,
            focused_goal_id=next(iter(goals), None),
            current_goal_ids=list(goals),
            original_query=original_query, intention_data=intention_data,
            semantic_tasks=semantic_tasks, goals=goals,
            nodes={
                node_id: {
                    "node_id": node_id,
                    "goal_id": (node_goals or {}).get(node_id, node_id.split("-", 1)[0]),
                    "operation_id": f"{run_id}:{node_id}",
                }
                for node_id in node_ids
            },
            graph_hash=graph_hash, skill_versions=skill_versions,
        )
        turn = WorkflowTurn(turn_id=turn_id, run_id=run_id, request_id=request_id, input=original_query)
        if self._postgres:
            await run_blocking(self._pg_insert, state, turn)
        else:
            await run_blocking(self._file_insert, state, turn)
        return state

    async def get(self, run_id: str) -> Optional[WorkflowRunState]:
        if self._postgres:
            return await run_blocking(self._pg_get, run_id)
        return await run_blocking(self._file_get, run_id)

    async def get_active(self, session_id: str) -> Optional[WorkflowRunState]:
        if self._postgres:
            return await run_blocking(self._pg_active, session_id)
        return await run_blocking(self._file_active, session_id)

    async def list_resumable(self, session_id: str | None = None) -> list[WorkflowRunState]:
        if self._postgres:
            return await run_blocking(self._pg_resumable, session_id)
        return await run_blocking(self._file_resumable, session_id)

    async def mutate(self, run_id: str, fn: Callable[[WorkflowRunState], None]) -> WorkflowRunState:
        if self._postgres:
            return await run_blocking(self._pg_mutate, run_id, fn)
        return await run_blocking(self._file_mutate, run_id, fn)

    async def start_turn(
        self, run_id: str, request_id: str, text: str,
        goal_ids: list[str] | None = None,
    ) -> WorkflowRunState:
        current = await self.get(run_id)
        if current is not None and current.current_request_id == request_id:
            return current
        turn = WorkflowTurn(
            turn_id=f"turn_{uuid.uuid4().hex}", run_id=run_id,
            request_id=request_id, input=text,
        )
        def apply(state):
            selected = set(goal_ids or [])
            state.current_turn_id = turn.turn_id
            state.current_request_id = request_id
            state.current_goal_ids = list(selected)
            state.status = "ACTIVE"
            for node in state.nodes.values():
                if (
                    node.goal_id in selected
                    and node.status in {"INTERRUPTED", "WAITING_USER", "RUNNING"}
                ):
                    node.status = "READY"
            for goal_id in selected:
                goal = state.goals.get(goal_id)
                if goal and goal.status in {"INTERRUPTED", "WAITING_USER"}:
                    goal.status = "RUNNING"
            state.waits = [wait for wait in state.waits if wait.goal_id not in selected]
        if self._postgres:
            return await run_blocking(self._pg_start_turn, run_id, turn, apply)
        return await run_blocking(self._file_start_turn, run_id, turn, apply)

    async def add_goals(
        self, run_id: str, *, request_id: str, text: str,
        intention_data: dict, semantic_tasks: list[dict], node_ids: list[str],
        graph_hash: str, skill_versions: dict[str, str],
        node_goals: dict[str, str] | None = None,
    ) -> WorkflowRunState:
        """Append independent goals and make them the new active turn atomically."""
        current = await self.get(run_id)
        if current is not None and current.current_request_id == request_id:
            return current
        turn = WorkflowTurn(
            turn_id=f"turn_{uuid.uuid4().hex}", run_id=run_id,
            request_id=request_id, input=text,
        )

        def apply(state):
            existing_task_ids = {item.get("task_id") for item in state.semantic_tasks}
            for task in semantic_tasks:
                task_id = task.get("task_id")
                if task_id in existing_task_ids:
                    raise StateConflictError(f"goal already exists: {task_id}")
                state.semantic_tasks.append(task)
                intent = task["intent"]
                state.goals[task_id] = GoalState(
                    goal_id=task_id, intent=intent, status="RUNNING",
                    query=task.get("query", ""),
                )
            for node_id in node_ids:
                if node_id in state.nodes:
                    raise StateConflictError(f"node already exists: {node_id}")
                state.nodes[node_id] = NodeState(
                    node_id=node_id,
                    goal_id=(node_goals or {}).get(node_id, node_id.split("-", 1)[0]),
                    operation_id=f"{run_id}:{node_id}",
                )
            existing_intents = list(state.intention_data.get("intents") or [])
            known = {item.get("type") for item in existing_intents}
            existing_intents.extend(
                item for item in (intention_data.get("intents") or [])
                if item.get("type") not in known
            )
            state.intention_data["intents"] = existing_intents
            state.skill_versions.update(skill_versions)
            state.graph_hash = graph_hash
            state.current_turn_id = turn.turn_id
            state.current_request_id = request_id
            state.current_goal_ids = [task["task_id"] for task in semantic_tasks]
            state.focused_goal_id = semantic_tasks[0]["task_id"] if semantic_tasks else state.focused_goal_id
            state.status = "ACTIVE"

        if self._postgres:
            return await run_blocking(self._pg_start_turn, run_id, turn, apply)
        return await run_blocking(self._file_start_turn, run_id, turn, apply)

    async def request_interrupt(
        self, run_id: str, turn_id: str, *, request_id: str | None = None,
    ) -> WorkflowRunState:
        def apply(state):
            if state.current_turn_id != turn_id:
                raise StateConflictError("turn is no longer active")
            if request_id and state.current_request_id != request_id:
                raise StateConflictError("request is no longer active")
            if state.status not in {"ACTIVE", "INTERRUPTING"}:
                return
            state.status = "INTERRUPTING"
        state = await self.mutate(run_id, apply)
        await self.set_turn_status(turn_id, "INTERRUPTING")
        return state

    async def recover_orphaned_active_run(
        self, run_id: str, *, incoming_request_id: str = "",
    ) -> WorkflowRunState:
        """Convert a previous process's ACTIVE snapshot into resumable state.

        The caller must already hold the user-level request lock.  Therefore a
        different request observing ACTIVE cannot race a still-running Turn;
        it represents a process loss between durable boundaries.
        """
        interrupted_turn_id = ""

        def apply(state):
            nonlocal interrupted_turn_id
            if state.status != "ACTIVE":
                return
            if incoming_request_id and state.current_request_id == incoming_request_id:
                return
            interrupted_turn_id = state.current_turn_id or ""
            state.status = "INTERRUPTED"
            state.expires_at = default_expiry(30)
            for node in state.nodes.values():
                if node.status == "RUNNING":
                    node.status = "INTERRUPTED"
            for goal_id in state.current_goal_ids:
                goal = state.goals.get(goal_id)
                if goal and goal.status in {"PENDING", "RUNNING"}:
                    goal.status = "INTERRUPTED"

        state = await self.mutate(run_id, apply)
        if interrupted_turn_id:
            await self.set_turn_status(interrupted_turn_id, "INTERRUPTED")
        return state

    async def set_turn_status(self, turn_id: str, status: str) -> None:
        if self._postgres:
            await run_blocking(self._pg_turn_status, turn_id, status)
        else:
            await run_blocking(self._file_turn_status, turn_id, status)

    async def should_interrupt(self, run_id: str, turn_id: str) -> bool:
        state = await self.get(run_id)
        return bool(
            state is None or state.current_turn_id != turn_id
            or state.status in {"INTERRUPTING", "INTERRUPTED", "ABANDONED", "EXPIRED"}
        )

    async def finish_run(self, run_id: str, status: str) -> WorkflowRunState:
        if status not in {"COMPLETED", "FAILED", "ABANDONED"}:
            raise ValueError(f"invalid terminal run status: {status}")

        def apply(state):
            state.status = status
            state.waits = []
            state.expires_at = default_expiry(90)
            for goal in state.goals.values():
                if goal.status in {"SUCCEEDED", "FAILED", "ABANDONED"}:
                    continue
                goal.status = "SUCCEEDED" if status == "COMPLETED" else "ABANDONED"

        return await self.mutate(run_id, apply)

    # -- file backend -----------------------------------------------------
    def _path(self, run_id: str) -> Path:
        return self._dir / f"{run_id}.json"

    def _turn_path(self, turn_id: str) -> Path:
        return self._dir / "turns" / f"{turn_id}.json"

    def _write_json(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _file_insert(self, state: WorkflowRunState, turn: WorkflowTurn) -> None:
        with self._file_lock:
            self._write_json(self._path(state.run_id), state.model_dump_json(indent=2))
            self._file_write_turn(turn)

    def _file_get(self, run_id: str) -> Optional[WorkflowRunState]:
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            return WorkflowRunState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _file_mutate(self, run_id: str, fn) -> WorkflowRunState:
        with self._file_lock:
            state = self._file_get(run_id)
            if state is None:
                raise KeyError(run_id)
            fn(state)
            state.revision += 1
            state.updated_at = utc_now()
            self._write_json(self._path(run_id), state.model_dump_json(indent=2))
            return state

    def _file_start_turn(self, run_id: str, turn: WorkflowTurn, fn) -> WorkflowRunState:
        with self._file_lock:
            state = self._file_get(run_id)
            if state is None:
                raise KeyError(run_id)
            if state.current_request_id == turn.request_id:
                return state
            turns_dir = self._dir / "turns"
            paths = turns_dir.glob("turn_*.json") if turns_dir.exists() else []
            for path in paths:
                existing = WorkflowTurn.model_validate_json(path.read_text(encoding="utf-8"))
                if existing.run_id == run_id and existing.request_id == turn.request_id:
                    raise StateConflictError("request_id was already used by this run")
            fn(state)
            state.revision += 1
            state.updated_at = utc_now()
            self._write_json(self._path(run_id), state.model_dump_json(indent=2))
            self._file_write_turn(turn)
            return state

    def _file_all(self) -> list[WorkflowRunState]:
        if not self._dir.exists():
            return []
        return [state for path in self._dir.glob("run_*.json") if (state := self._file_get(path.stem))]

    def _file_active(self, session_id: str) -> Optional[WorkflowRunState]:
        now = utc_now()
        rows = [
            s for s in self._file_all()
            if s.session_id == session_id and s.status in _RESUMABLE and s.expires_at > now
        ]
        return max(rows, key=lambda s: s.updated_at, default=None)

    def _file_resumable(self, session_id: str | None) -> list[WorkflowRunState]:
        now = utc_now()
        rows = [
            s for s in self._file_all()
            if s.status in _RESUMABLE and s.expires_at > now
            and (session_id is None or s.session_id == session_id)
        ]
        return sorted(rows, key=lambda s: s.updated_at, reverse=True)

    def _file_write_turn(self, turn: WorkflowTurn) -> None:
        self._write_json(self._turn_path(turn.turn_id), turn.model_dump_json(indent=2))

    def _file_turn_status(self, turn_id: str, status: str) -> None:
        path = self._turn_path(turn_id)
        if not path.exists():
            return
        turn = WorkflowTurn.model_validate_json(path.read_text(encoding="utf-8"))
        turn.status = status
        turn.updated_at = utc_now()
        self._write_json(path, turn.model_dump_json(indent=2))

    # -- postgres backend -------------------------------------------------
    @contextmanager
    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.postgres_dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn

    def _pg_insert(self, state: WorkflowRunState, turn: WorkflowTurn) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestration_runs
                SET status='EXPIRED', state=jsonb_set(state, '{status}', '"EXPIRED"'),
                    updated_at=NOW()
                WHERE user_id=%s AND session_id=%s
                  AND status = ANY(%s) AND expires_at <= NOW()
            """, (self.user_id, state.session_id, list(_RESUMABLE)))
            cur.execute("""
                INSERT INTO orchestration_runs
                    (run_id, user_id, session_id, status, revision, schema_version,
                     focused_goal_id, graph_hash, state, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::timestamptz)
            """, (state.run_id, state.user_id, state.session_id, state.status, state.revision,
                  state.schema_version, state.focused_goal_id, state.graph_hash,
                  state.model_dump_json(), state.expires_at))
            self._insert_turn(cur, turn)

    @staticmethod
    def _insert_turn(cur, turn: WorkflowTurn) -> None:
        cur.execute("""
            INSERT INTO orchestration_turns
                (turn_id, run_id, request_id, status, input, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s::timestamptz,%s::timestamptz)
            ON CONFLICT (run_id, request_id) DO NOTHING
        """, (turn.turn_id, turn.run_id, turn.request_id, turn.status, turn.input,
              turn.created_at, turn.updated_at))

    def _row_state(self, row) -> Optional[WorkflowRunState]:
        if not row:
            return None
        raw = row["state"]
        return WorkflowRunState.model_validate(json.loads(raw) if isinstance(raw, str) else raw)

    def _pg_get(self, run_id: str) -> Optional[WorkflowRunState]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM orchestration_runs WHERE run_id=%s AND user_id=%s", (run_id, self.user_id))
            return self._row_state(cur.fetchone())

    def _pg_active(self, session_id: str) -> Optional[WorkflowRunState]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT state FROM orchestration_runs
                WHERE user_id=%s AND session_id=%s AND status = ANY(%s)
                  AND expires_at > NOW()
                ORDER BY updated_at DESC LIMIT 1
            """, (self.user_id, session_id, list(_RESUMABLE)))
            return self._row_state(cur.fetchone())

    def _pg_resumable(self, session_id: str | None) -> list[WorkflowRunState]:
        with self._conn() as conn, conn.cursor() as cur:
            sql = "SELECT state FROM orchestration_runs WHERE user_id=%s AND status = ANY(%s) AND expires_at > NOW()"
            params: list = [self.user_id, list(_RESUMABLE)]
            if session_id is not None:
                sql += " AND session_id=%s"
                params.append(session_id)
            sql += " ORDER BY updated_at DESC"
            cur.execute(sql, params)
            return [self._row_state(row) for row in cur.fetchall()]

    def _pg_mutate(self, run_id: str, fn) -> WorkflowRunState:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM orchestration_runs WHERE run_id=%s AND user_id=%s FOR UPDATE", (run_id, self.user_id))
            state = self._row_state(cur.fetchone())
            if state is None:
                raise KeyError(run_id)
            fn(state)
            state.revision += 1
            state.updated_at = utc_now()
            cur.execute("""
                UPDATE orchestration_runs SET status=%s, revision=%s, focused_goal_id=%s,
                    schema_version=%s, graph_hash=%s, state=%s::jsonb,
                    expires_at=%s::timestamptz, updated_at=NOW()
                WHERE run_id=%s AND user_id=%s
            """, (state.status, state.revision, state.focused_goal_id,
                  state.schema_version, state.graph_hash, state.model_dump_json(),
                  state.expires_at, run_id, self.user_id))
            return state

    def _pg_start_turn(self, run_id: str, turn: WorkflowTurn, fn) -> WorkflowRunState:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM orchestration_runs WHERE run_id=%s AND user_id=%s FOR UPDATE", (run_id, self.user_id))
            state = self._row_state(cur.fetchone())
            if state is None:
                raise KeyError(run_id)
            if state.current_request_id == turn.request_id:
                return state
            cur.execute(
                "SELECT turn_id FROM orchestration_turns WHERE run_id=%s AND request_id=%s",
                (run_id, turn.request_id),
            )
            if cur.fetchone() is not None:
                raise StateConflictError("request_id was already used by this run")
            fn(state)
            state.revision += 1
            state.updated_at = utc_now()
            cur.execute("""
                UPDATE orchestration_runs SET status=%s, revision=%s,
                    focused_goal_id=%s, schema_version=%s, graph_hash=%s, state=%s::jsonb,
                    expires_at=%s::timestamptz, updated_at=NOW()
                WHERE run_id=%s AND user_id=%s
            """, (state.status, state.revision, state.focused_goal_id,
                  state.schema_version, state.graph_hash, state.model_dump_json(), state.expires_at,
                  run_id, self.user_id))
            self._insert_turn(cur, turn)
            return state

    def _pg_turn_status(self, turn_id: str, status: str) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestration_turns SET status=%s, updated_at=NOW(),
                    interrupted_at=CASE WHEN %s='INTERRUPTED' THEN NOW() ELSE interrupted_at END,
                    completed_at=CASE WHEN %s IN ('COMPLETED','FAILED') THEN NOW() ELSE completed_at END
                WHERE turn_id=%s
            """, (status, status, status, turn_id))
