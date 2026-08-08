"""Cross-turn checkpoint persistence for interrupted workflows.

A paused plan-trip stores the remaining execution steps and the facts
collected so far. ``active_trip_contexts`` remains the source of truth for
trip facts; this store only answers "where do we resume from". Persistence is
PostgreSQL when configured, else a JSON file under ``storage_dir`` (so the
pure-file memory backend keeps working end-to-end).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from settings import MEMORY_CONFIG


class Checkpoint(BaseModel):
    """Serialized pause scene; mirrors PauseInfo minus derived presentation."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    skill: str
    request_id: str
    pause_agent: str
    pause_field: str = "planning_ready"
    steps_remaining: List[Dict[str, Any]] = Field(default_factory=list)
    collected_facts: Dict[str, Any] = Field(default_factory=dict)
    entities: Dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


@dataclass
class _FileBackend:
    """Thread-safe JSON file store (asyncio.to_thread), safe for FileLongTermMemory."""

    path: Path

    def read(self) -> Optional[Checkpoint]:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return Checkpoint.model_validate(raw)
        except Exception:
            return None

    def write(self, checkpoint: Checkpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            checkpoint.model_dump_json(indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink(missing_ok=True)


class CheckpointStore:
    def __init__(
        self,
        user_id: str,
        postgres_dsn: Optional[str] = None,
        storage_dir: Optional[str] = None,
    ):
        self.user_id = user_id
        backend = MEMORY_CONFIG.get("long_term", {}).get("backend", "file")
        self.postgres_dsn = postgres_dsn if postgres_dsn is not None else (
            MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
        )
        self._enabled = bool(self.postgres_dsn) and (postgres_dsn is not None or backend == "postgres")
        base = Path(storage_dir or MEMORY_CONFIG.get("long_term", {}).get("storage_dir", "data/memory"))
        safe_user = "".join(ch for ch in user_id if ch.isalnum() or ch in "-_") or "user"
        self._file_backend = _FileBackend(base / "checkpoints" / f"{safe_user}.json")

    @property
    def configured(self) -> bool:
        return self._enabled

    def _to_checkpoint(self, pause_info) -> Checkpoint:
        return Checkpoint(
            user_id=self.user_id,
            skill=pause_info.skill,
            request_id="",
            pause_agent=pause_info.pause_agent,
            pause_field=pause_info.pause_field,
            steps_remaining=pause_info.steps_remaining,
            collected_facts=pause_info.collected_facts,
            entities=pause_info.entities,
        )

    async def save(self, pause_info) -> None:
        checkpoint = self._to_checkpoint(pause_info)
        if self.configured:
            await asyncio.to_thread(self._save_postgres, checkpoint)
        else:
            await asyncio.to_thread(self._file_backend.write, checkpoint)

    async def get(self) -> Optional[Checkpoint]:
        if self.configured:
            return await asyncio.to_thread(self._get_postgres)
        return await asyncio.to_thread(self._file_backend.read)

    async def clear(self) -> None:
        if self.configured:
            await asyncio.to_thread(self._clear_postgres)
        else:
            await asyncio.to_thread(self._file_backend.clear)

    # ---- Postgres backend -------------------------------------------------

    def _conn(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(
            self.postgres_dsn,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
        )

    def _save_postgres(self, checkpoint: Checkpoint) -> None:
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO orchestration_checkpoints (
                        user_id, skill, request_id, steps_remaining,
                        collected_facts, entities, updated_at
                    ) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        skill = EXCLUDED.skill,
                        request_id = EXCLUDED.request_id,
                        steps_remaining = EXCLUDED.steps_remaining,
                        collected_facts = EXCLUDED.collected_facts,
                        entities = EXCLUDED.entities,
                        updated_at = NOW()
                    """,
                    (
                        checkpoint.user_id,
                        checkpoint.skill,
                        checkpoint.request_id,
                        json.dumps(checkpoint.steps_remaining, ensure_ascii=False),
                        json.dumps(checkpoint.collected_facts, ensure_ascii=False),
                        json.dumps(checkpoint.entities, ensure_ascii=False),
                    ),
                )
        except Exception:
            # Postgres 不可用时静默降级为文件后端（下次 save 会走文件路径）。
            self._enabled = False

    def _get_postgres(self) -> Optional[Checkpoint]:
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT skill, request_id, pause_agent, pause_field,
                           steps_remaining, collected_facts, entities, updated_at
                    FROM orchestration_checkpoints
                    WHERE user_id = %s
                    """,
                    (self.user_id,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return Checkpoint(
                user_id=self.user_id,
                skill=row["skill"],
                request_id=row["request_id"],
                pause_agent=row.get("pause_agent") or "",
                pause_field=row.get("pause_field") or "planning_ready",
                steps_remaining=row["steps_remaining"] or [],
                collected_facts=row["collected_facts"] or {},
                entities=row["entities"] or {},
                updated_at=str(row["updated_at"]),
            )
        except Exception:
            self._enabled = False
            return None

    def _clear_postgres(self) -> None:
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM orchestration_checkpoints WHERE user_id = %s",
                    (self.user_id,),
                )
        except Exception:
            self._enabled = False
