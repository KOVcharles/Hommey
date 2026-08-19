"""Durable PostgreSQL control plane for RAG refresh jobs."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from context.postgres_pool import get_postgres_pool


T = TypeVar("T")


class ActiveRefreshJobError(RuntimeError):
    """Raised when one collection already has a queued or running refresh."""


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _status_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {
            "job_id": None,
            "status": "idle",
            "stage": "知识库已就绪",
            "progress": 0,
            "requested_by": None,
            "source_generation": None,
            "attempt": 0,
            "started_at": None,
            "finished_at": None,
            "report": None,
            "message": None,
        }
    return {
        "job_id": str(row["job_id"]),
        "status": row["status"],
        "stage": row["stage"],
        "progress": int(row["progress"]),
        "requested_by": row["requested_by"],
        "source_generation": int(row["source_generation"]),
        "attempt": int(row["attempt"]),
        "started_at": _iso(row.get("started_at") or row.get("created_at")),
        "finished_at": _iso(row.get("finished_at")),
        "report": row.get("report"),
        "message": row.get("message"),
        "lease_owner": row.get("lease_owner"),
        "lease_expires_at": _iso(row.get("lease_expires_at")),
    }


class PostgresRAGRefreshJobRepository:
    """Queue, lease and source-generation operations shared by all processes."""

    def __init__(
        self,
        postgres_dsn: str,
        collection_name: str,
        *,
        lease_seconds: int = 90,
        max_attempts: int = 3,
        retry_delay_seconds: int = 5,
        pool: Any | None = None,
    ):
        if not postgres_dsn and pool is None:
            raise ValueError("PostgreSQL RAG refresh requires a PostgreSQL DSN")
        self.postgres_dsn = postgres_dsn
        self.collection_name = collection_name
        self.lease_seconds = max(15, int(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(1, int(retry_delay_seconds))
        self._injected_pool = pool

    @property
    def pool(self):
        return self._injected_pool or get_postgres_pool(self.postgres_dsn)

    def _source_lock(self, cursor) -> None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"hommey:rag-source:{self.collection_name}",),
        )

    def run_source_change(self, publish: Callable[[], T]) -> tuple[T, int]:
        """Serialize a filesystem publication with the source generation bump."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._source_lock(cursor)
            cursor.execute(
                """
                INSERT INTO rag_source_generations(collection_name, generation)
                VALUES (%s, 0) ON CONFLICT (collection_name) DO NOTHING
                """,
                (self.collection_name,),
            )
            result = publish()
            cursor.execute(
                """
                UPDATE rag_source_generations
                SET generation=generation + 1, updated_at=NOW()
                WHERE collection_name=%s
                RETURNING generation
                """,
                (self.collection_name,),
            )
            generation = int(cursor.fetchone()["generation"])
        return result, generation

    def enqueue(
        self,
        requested_by: str,
        snapshot_factory: Callable[[], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        job_id = uuid.uuid4()
        with self.pool.connection() as connection, connection.cursor() as cursor:
            self._source_lock(cursor)
            cursor.execute(
                """
                INSERT INTO rag_source_generations(collection_name, generation)
                VALUES (%s, 0) ON CONFLICT (collection_name) DO NOTHING
                """,
                (self.collection_name,),
            )
            cursor.execute(
                """
                SELECT job_id FROM rag_refresh_jobs
                WHERE collection_name=%s AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (self.collection_name,),
            )
            if cursor.fetchone():
                raise ActiveRefreshJobError(self.collection_name)
            source_manifest = snapshot_factory()
            cursor.execute(
                "SELECT generation FROM rag_source_generations WHERE collection_name=%s",
                (self.collection_name,),
            )
            generation = int(cursor.fetchone()["generation"])
            cursor.execute(
                """
                INSERT INTO rag_refresh_jobs(
                    job_id, collection_name, status, stage, progress, requested_by,
                    source_generation, source_manifest, max_attempts
                ) VALUES (%s, %s, 'queued', %s, 5, %s, %s, %s::jsonb, %s)
                RETURNING *
                """,
                (
                    job_id,
                    self.collection_name,
                    "等待知识库刷新任务执行",
                    str(requested_by),
                    generation,
                    json.dumps(source_manifest, ensure_ascii=False),
                    self.max_attempts,
                ),
            )
            row = cursor.fetchone()
        return _status_payload(row)

    def latest_status(self) -> dict[str, Any]:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM rag_refresh_jobs
                WHERE collection_name=%s
                ORDER BY created_at DESC LIMIT 1
                """,
                (self.collection_name,),
            )
            return _status_payload(cursor.fetchone())

    def latest_manifest(self) -> dict[str, Any] | None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT result_manifest FROM rag_refresh_jobs
                WHERE collection_name=%s
                  AND status IN ('success', 'partial_success')
                  AND result_manifest IS NOT NULL
                ORDER BY finished_at DESC NULLS LAST LIMIT 1
                """,
                (self.collection_name,),
            )
            row = cursor.fetchone()
            return dict(row["result_manifest"]) if row and row.get("result_manifest") else None

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_refresh_jobs
                SET status=CASE WHEN attempt >= max_attempts THEN 'error' ELSE 'queued' END,
                    stage=CASE WHEN attempt >= max_attempts
                        THEN '知识库刷新重试次数已耗尽' ELSE '等待故障恢复重试' END,
                    message=CASE WHEN attempt >= max_attempts
                        THEN '刷新 worker 租约过期且重试次数已耗尽' ELSE message END,
                    error=CASE WHEN attempt >= max_attempts
                        THEN COALESCE(error, 'refresh worker lease expired') ELSE error END,
                    next_attempt_at=NOW(), lease_owner=NULL, lease_expires_at=NULL,
                    finished_at=CASE WHEN attempt >= max_attempts THEN NOW() ELSE NULL END,
                    updated_at=NOW()
                WHERE collection_name=%s AND status='running'
                  AND lease_expires_at < NOW()
                """,
                (self.collection_name,),
            )
            cursor.execute(
                """
                SELECT job_id FROM rag_refresh_jobs
                WHERE collection_name=%s AND status='queued' AND next_attempt_at <= NOW()
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED LIMIT 1
                """,
                (self.collection_name,),
            )
            candidate = cursor.fetchone()
            if not candidate:
                return None
            cursor.execute(
                """
                UPDATE rag_refresh_jobs
                SET status='running', stage='正在准备知识库刷新', progress=8,
                    lease_owner=%s,
                    lease_expires_at=NOW() + make_interval(secs => %s),
                    attempt=attempt + 1, started_at=COALESCE(started_at, NOW()),
                    updated_at=NOW()
                WHERE job_id=%s
                RETURNING *
                """,
                (worker_id, self.lease_seconds, candidate["job_id"]),
            )
            row = dict(cursor.fetchone())
        row["job_id"] = str(row["job_id"])
        return row

    def renew(self, job_id: str, worker_id: str) -> bool:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_refresh_jobs
                SET lease_expires_at=NOW() + make_interval(secs => %s), updated_at=NOW()
                WHERE job_id=%s AND status='running' AND lease_owner=%s
                RETURNING job_id
                """,
                (self.lease_seconds, job_id, worker_id),
            )
            return cursor.fetchone() is not None

    def update_progress(self, job_id: str, worker_id: str, stage: str, progress: int) -> bool:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_refresh_jobs
                SET stage=%s, progress=%s, updated_at=NOW()
                WHERE job_id=%s AND status='running' AND lease_owner=%s
                RETURNING job_id
                """,
                (str(stage), max(0, min(99, int(progress))), job_id, worker_id),
            )
            return cursor.fetchone() is not None

    def complete(
        self,
        job_id: str,
        worker_id: str,
        report: dict[str, Any],
        result_manifest: dict[str, Any],
    ) -> bool:
        status = str(report.get("status") or "error")
        if status not in {"success", "partial_success"}:
            status = "error"
        stage = "知识库刷新完成" if status == "success" else "知识库刷新完成，但有部分文档失败"
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE rag_refresh_jobs
                SET status=%s, stage=%s, progress=100, report=%s::jsonb,
                    result_manifest=%s::jsonb, message=%s, error=NULL,
                    lease_owner=NULL, lease_expires_at=NULL, finished_at=NOW(), updated_at=NOW()
                WHERE job_id=%s AND status='running' AND lease_owner=%s
                RETURNING job_id
                """,
                (
                    status,
                    stage,
                    json.dumps(report, ensure_ascii=False),
                    json.dumps(result_manifest, ensure_ascii=False),
                    report.get("message"),
                    job_id,
                    worker_id,
                ),
            )
            return cursor.fetchone() is not None

    def fail(self, job_id: str, worker_id: str, error: str, *, retryable: bool = True) -> bool:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempt, max_attempts FROM rag_refresh_jobs WHERE job_id=%s FOR UPDATE",
                (job_id,),
            )
            state = cursor.fetchone()
            if not state:
                return False
            retry = retryable and int(state["attempt"]) < int(state["max_attempts"])
            cursor.execute(
                """
                UPDATE rag_refresh_jobs
                SET status=%s, stage=%s, progress=CASE WHEN %s THEN progress ELSE 100 END,
                    message=%s, error=%s, lease_owner=NULL, lease_expires_at=NULL,
                    next_attempt_at=CASE WHEN %s
                        THEN NOW() + make_interval(secs => %s) ELSE next_attempt_at END,
                    finished_at=CASE WHEN %s THEN NULL ELSE NOW() END, updated_at=NOW()
                WHERE job_id=%s AND status='running' AND lease_owner=%s
                RETURNING job_id
                """,
                (
                    "queued" if retry else "error",
                    "等待刷新任务重试" if retry else "知识库刷新失败",
                    retry,
                    "刷新失败，正在等待自动重试" if retry else "刷新失败，请检查服务配置后重试",
                    str(error)[:4000],
                    retry,
                    self.retry_delay_seconds,
                    retry,
                    job_id,
                    worker_id,
                ),
            )
            return cursor.fetchone() is not None

    def heartbeat(self, worker_id: str, metadata: dict[str, Any] | None = None) -> None:
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO rag_worker_heartbeats(worker_id, collection_name, metadata)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (worker_id) DO UPDATE
                SET collection_name=EXCLUDED.collection_name,
                    last_seen_at=NOW(), metadata=EXCLUDED.metadata
                """,
                (worker_id, self.collection_name, json.dumps(metadata or {})),
            )

    def try_collection_lock(self, connection) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                (f"hommey:rag-refresh:{self.collection_name}",),
            )
            return bool(cursor.fetchone()["acquired"])

    def release_collection_lock(self, connection) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                (f"hommey:rag-refresh:{self.collection_name}",),
            )
