"""PostgreSQL persistence and lease ownership for evaluation work."""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Optional

from evaluation.models import FinalEvaluationResult, TurnEvaluationMetadata
from settings import EVALUATION_CONFIG, MEMORY_CONFIG


class EvaluationRepository:
    """Owns only evaluation tables and uses a dedicated lazy connection pool."""

    def __init__(self, postgres_dsn: Optional[str] = None):
        self.postgres_dsn = postgres_dsn if postgres_dsn is not None else (
            MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
        )
        self._pool = None

    @property
    def configured(self) -> bool:
        return bool(self.postgres_dsn)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def create_subject_and_run(
        self,
        metadata: TurnEvaluationMetadata,
        *,
        evaluator_version: str,
        judge_model: str,
        judge_prompt_version: str,
        rubric_version: str,
    ) -> tuple[str, str]:
        payload = metadata.model_dump(mode="json")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        payload_hash = _sha256(payload_json)
        subject_id = str(uuid.uuid4())
        evaluation_id = str(uuid.uuid4())
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evaluation_subjects (
                    subject_id, subject_type, request_id, session_id, run_id, turn_id,
                    capture_mode, schema_version, payload, producer_versions, payload_hash
                ) VALUES (
                    %s, 'turn', %s, %s, NULLIF(%s, ''), NULLIF(%s, ''),
                    %s, %s, %s::jsonb, %s::jsonb, %s
                )
                ON CONFLICT (subject_type, request_id) DO NOTHING
                RETURNING subject_id
                """,
                (
                    subject_id,
                    metadata.subject.request_id,
                    metadata.subject.session_id,
                    metadata.subject.run_id,
                    metadata.subject.turn_id,
                    metadata.capture_mode,
                    metadata.schema_version,
                    payload_json,
                    json.dumps(metadata.versions.model_dump(mode="json"), ensure_ascii=False),
                    payload_hash,
                ),
            )
            row = cur.fetchone()
            if row:
                subject_id = str(_value(row, "subject_id", 0))
            else:
                cur.execute(
                    """
                    SELECT subject_id FROM evaluation_subjects
                    WHERE subject_type = 'turn' AND request_id = %s
                    """,
                    (metadata.subject.request_id,),
                )
                subject_id = str(_value(cur.fetchone(), "subject_id", 0))

            cur.execute(
                """
                INSERT INTO evaluation_runs (
                    evaluation_id, subject_id, status, evaluator_version, judge_model,
                    judge_prompt_version, rubric_version
                ) VALUES (%s, %s, 'pending', %s, %s, %s, %s)
                ON CONFLICT (subject_id, evaluator_version) DO NOTHING
                RETURNING evaluation_id
                """,
                (
                    evaluation_id, subject_id, evaluator_version, judge_model,
                    judge_prompt_version, rubric_version,
                ),
            )
            row = cur.fetchone()
            if row:
                evaluation_id = str(_value(row, "evaluation_id", 0))
            else:
                cur.execute(
                    """
                    SELECT evaluation_id FROM evaluation_runs
                    WHERE subject_id = %s AND evaluator_version = %s
                    """,
                    (subject_id, evaluator_version),
                )
                evaluation_id = str(_value(cur.fetchone(), "evaluation_id", 0))
        return subject_id, evaluation_id

    def claim_pending(
        self,
        *,
        worker_id: str,
        batch_size: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    SELECT evaluation_id
                    FROM evaluation_runs
                    WHERE status = 'pending'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE evaluation_runs AS run
                SET status = 'running',
                    lease_owner = %s,
                    lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                    attempts = attempts + 1,
                    started_at = COALESCE(started_at, NOW())
                FROM claimed
                WHERE run.evaluation_id = claimed.evaluation_id
                RETURNING run.*,
                    (SELECT payload FROM evaluation_subjects WHERE subject_id = run.subject_id) AS payload
                """,
                (max(1, batch_size), worker_id, max(1, lease_seconds)),
            )
            return [dict(row) for row in (cur.fetchall() or [])]

    def recover_expired_leases(self, *, max_attempts: int) -> int:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evaluation_runs
                SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                    error_code = CASE WHEN attempts >= %s THEN 'EVALUATION_MAX_ATTEMPTS' ELSE NULL END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    completed_at = CASE WHEN attempts >= %s THEN NOW() ELSE completed_at END
                WHERE status = 'running' AND lease_expires_at < NOW()
                """,
                (max_attempts, max_attempts, max_attempts),
            )
            return cur.rowcount

    def complete(
        self,
        evaluation_id: str,
        *,
        worker_id: str,
        result: FinalEvaluationResult,
        token_usage: dict[str, Any] | None = None,
        latency_ms: int | None = None,
    ) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evaluation_runs
                SET status = 'completed', verdict = %s, score = %s,
                    dimension_scores = %s::jsonb, reason_codes = %s::jsonb,
                    critical_errors = %s::jsonb, rule_results = %s::jsonb,
                    explanation = %s, review_required = %s,
                    token_usage = %s::jsonb, latency_ms = %s,
                    lease_owner = NULL, lease_expires_at = NULL, completed_at = NOW()
                WHERE evaluation_id = %s AND status = 'running' AND lease_owner = %s
                """,
                (
                    result.verdict,
                    result.score,
                    _json(result.dimension_scores),
                    _json(result.reason_codes),
                    _json(result.critical_errors),
                    _json([item.model_dump(mode="json") for item in result.rule_results]),
                    result.explanation,
                    result.review_required,
                    _json(token_usage or {}),
                    latency_ms,
                    evaluation_id,
                    worker_id,
                ),
            )
            return cur.rowcount == 1

    def skip(self, evaluation_id: str, *, worker_id: str, reason: str) -> bool:
        return self._finish_state(
            evaluation_id, worker_id=worker_id, status="skipped", error_code=reason,
        )

    def fail(self, evaluation_id: str, *, worker_id: str, error_code: str) -> bool:
        return self._finish_state(
            evaluation_id, worker_id=worker_id, status="failed", error_code=error_code,
        )

    def report_rows(self, *, days: int = 7, limit: int = 5000) -> dict[str, Any]:
        """Return bounded, conversation-free rows for a development report."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(DISTINCT request_id) AS total
                FROM conversation_messages
                WHERE role = 'assistant' AND deleted_at IS NULL
                  AND created_at >= NOW() - (%s * INTERVAL '1 day')
                """,
                (max(1, days),),
            )
            assistant_turns = int(_value(cur.fetchone(), "total", 0) or 0)
            cur.execute(
                """
                SELECT run.evaluation_id, run.status, run.verdict, run.reason_codes,
                       run.review_required, run.error_code, run.evaluator_version,
                       run.created_at, subject.subject_id,
                       subject.payload->'routing'->'selected_skills' AS selected_skills
                FROM evaluation_runs AS run
                JOIN evaluation_subjects AS subject ON subject.subject_id = run.subject_id
                WHERE run.created_at >= NOW() - (%s * INTERVAL '1 day')
                ORDER BY run.created_at DESC
                LIMIT %s
                """,
                (max(1, days), max(1, min(limit, 20_000))),
            )
            rows = [dict(row) for row in (cur.fetchall() or [])]
        return {"assistant_turns": assistant_turns, "rows": rows, "days": max(1, days)}

    def missing_turn_subjects(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Find persisted assistant Turns not yet represented by an evaluation subject."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT assistant.request_id::text AS request_id,
                       assistant.session_id::text AS session_id,
                       assistant.turn_id::text AS turn_id,
                       assistant.content AS assistant_message,
                       assistant.answer_document,
                       assistant.presentation_document,
                       assistant.created_at AS occurred_at,
                       user_message.content AS user_message
                FROM conversation_messages AS assistant
                LEFT JOIN conversation_messages AS user_message
                  ON user_message.user_id = assistant.user_id
                 AND user_message.request_id = assistant.request_id
                 AND user_message.role = 'user'
                 AND user_message.sequence_no < assistant.sequence_no
                LEFT JOIN evaluation_subjects AS subject
                  ON subject.subject_type = 'turn'
                 AND subject.request_id = assistant.request_id::text
                WHERE assistant.role = 'assistant'
                  AND assistant.deleted_at IS NULL
                  AND subject.subject_id IS NULL
                ORDER BY assistant.created_at
                LIMIT %s
                """,
                (max(1, min(limit, 1000)),),
            )
            return [dict(row) for row in (cur.fetchall() or [])]

    def _finish_state(
        self,
        evaluation_id: str,
        *,
        worker_id: str,
        status: str,
        error_code: str,
    ) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evaluation_runs
                SET status = %s, error_code = %s, lease_owner = NULL,
                    lease_expires_at = NULL, completed_at = NOW()
                WHERE evaluation_id = %s AND status = 'running' AND lease_owner = %s
                """,
                (status, error_code, evaluation_id, worker_id),
            )
            return cur.rowcount == 1

    @contextmanager
    def _connection(self):
        if not self.configured:
            raise RuntimeError("Evaluation PostgreSQL is not configured")
        if self._pool is None:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            max_size = max(1, int(EVALUATION_CONFIG.get("database_pool_size", 2)))
            self._pool = ConnectionPool(
                conninfo=self.postgres_dsn,
                min_size=0,
                max_size=max_size,
                timeout=float(EVALUATION_CONFIG.get("database_timeout_sec", 5.0)),
                kwargs={"autocommit": False, "row_factory": dict_row},
                open=True,
            )
        with self._pool.connection() as conn:
            yield conn


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _sha256(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _value(row, key: str, index: int):
    if isinstance(row, dict):
        return row[key]
    return row[index]
