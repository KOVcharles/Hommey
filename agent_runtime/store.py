"""Durable checkpoints and fenced, atomic business writes in the existing database."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from context.memory_repository import stable_uuid
from context.preference_schema import PREFERENCE_LIST_COLUMNS, PREFERENCE_SCALAR_COLUMNS
from utils.io_executor import run_blocking
from utils.memory_safety import redact_sensitive_text
from .contracts import RuntimeStopped, ToolRejected


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def trip_version(trip: dict | None) -> int:
    data = {k: v for k, v in (trip or {}).items() if k != "updated_at"}
    return int(fingerprint(data)[:12], 16)


def safe_checkpoint(value):
    """Redact nested JSON messages without corrupting their replayable syntax."""
    if isinstance(value, dict):
        return {k: safe_checkpoint(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_checkpoint(v) for v in value]
    if isinstance(value, str):
        # Opaque protocol IDs can contain eleven consecutive digits. Redacting
        # them as phone numbers breaks tool-call pairs and evidence references.
        if re.fullmatch(r"(?:call_|src_|result_)[a-fA-F0-9]{8,64}", value):
            return value
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                pass
            else:
                return json.dumps(safe_checkpoint(parsed), ensure_ascii=False)
        return redact_sensitive_text(value)
    return value


class RunStore:
    def __init__(self, pool):
        self.pool = pool

    async def call(self, method: str, *args):
        return await run_blocking(getattr(self, "_" + method), *args)

    @staticmethod
    def _key(scope):
        return (scope.user_id, scope.request_id, stable_uuid(scope.session_id, namespace="session"))

    def _begin(self, scope, input_hash):
        owner = uuid4().hex
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("DELETE FROM supervisor_runs WHERE user_id=%s AND retention_until<NOW()", (scope.user_id,))
            cur.execute("""INSERT INTO supervisor_runs (user_id, request_id, session_id, input_hash, owner)
                SELECT %s, %s, session_id, %s, %s FROM conversation_sessions
                WHERE session_id = %s AND user_id = %s AND close_reason IS DISTINCT FROM 'deleted'
                AND close_reason IS DISTINCT FROM 'cleared'
                ON CONFLICT (user_id, request_id) DO NOTHING""",
                (scope.user_id, scope.request_id, input_hash, owner, self._key(scope)[2], scope.user_id))
            cur.execute("SELECT * FROM supervisor_runs WHERE user_id=%s AND request_id=%s FOR UPDATE", self._key(scope)[:2])
            row = cur.fetchone()
            if not row or str(row["session_id"]) != str(self._key(scope)[2]) or row["input_hash"] != input_hash:
                raise ToolRejected("请求 ID 已用于其他内容或会话，请重新发送")
            if row["status"] == "completed":
                return {"owner": owner, "response": row["response"], "checkpoint": row["checkpoint"]}
            cur.execute("""SELECT 1 FROM supervisor_runs WHERE user_id=%s AND session_id=%s
                AND created_at>%s LIMIT 1""", (scope.user_id, self._key(scope)[2], row["created_at"]))
            if cur.fetchone():
                raise ToolRejected("此请求已被后续消息替代，请发送新消息继续")
            cur.execute("""UPDATE supervisor_runs SET status='interrupted', cancel_requested=TRUE, updated_at=NOW()
                WHERE user_id=%s AND session_id=%s AND request_id<>%s AND status='running'""",
                (scope.user_id, self._key(scope)[2], scope.request_id))
            # Called only while the existing per-user distributed lease is held.
            # A new owner fences delayed writes from an abandoned worker.
            cur.execute("""UPDATE supervisor_runs SET owner=%s, status='running',
                cancel_requested=FALSE, updated_at=NOW() WHERE user_id=%s AND request_id=%s""",
                (owner, scope.user_id, scope.request_id))
            return {"owner": owner, "response": None, "checkpoint": row["checkpoint"]}

    def _check(self, scope, owner):
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT 1 FROM supervisor_runs WHERE user_id=%s AND request_id=%s AND session_id=%s
                AND owner=%s AND status='running' AND NOT cancel_requested""", (*self._key(scope), owner))
            if not cur.fetchone():
                raise RuntimeStopped("本次执行已停止或被新的执行替代")

    def _save(self, scope, owner, checkpoint, response=None, status="running"):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE supervisor_runs SET checkpoint=%s, response=%s, status=%s, updated_at=NOW()
                WHERE user_id=%s AND request_id=%s AND session_id=%s AND owner=%s
                AND status='running' AND NOT cancel_requested RETURNING request_id""",
                (Jsonb(safe_checkpoint(checkpoint)), Jsonb(safe_checkpoint(response)) if response is not None else None, status, *self._key(scope), owner))
            if not cur.fetchone():
                raise RuntimeStopped("本次执行已停止或被新的执行替代")

    def _stop(self, scope, owner, status, checkpoint=None):
        from psycopg.types.json import Jsonb
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE supervisor_runs SET status=%s, checkpoint=COALESCE(%s,checkpoint), updated_at=NOW()
                WHERE user_id=%s AND request_id=%s AND session_id=%s AND owner=%s AND status='running'""",
                (status, Jsonb(safe_checkpoint(checkpoint)) if checkpoint is not None else None, *self._key(scope), owner))

    def _cancel(self, scope):
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""UPDATE supervisor_runs SET cancel_requested=TRUE, updated_at=NOW()
                WHERE user_id=%s AND request_id=%s AND session_id=%s AND status='running' RETURNING request_id""", self._key(scope))
            return bool(cur.fetchone())

    def _previous(self, scope):
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT checkpoint, status FROM supervisor_runs
                WHERE user_id=%s AND session_id=%s AND request_id<>%s AND retention_until>NOW()
                ORDER BY created_at DESC LIMIT 1""", (scope.user_id, self._key(scope)[2], scope.request_id))
            return cur.fetchone()

    def _public_plans(self, user_id, session_id):
        # Read-only recovery never acquires the turn lease or returns raw context.
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""SELECT checkpoint->'public_plan' AS plan, status FROM supervisor_runs
                WHERE user_id=%s AND session_id=%s AND retention_until>NOW()
                AND checkpoint ? 'public_plan' ORDER BY created_at DESC LIMIT 50""",
                (user_id, stable_uuid(session_id, namespace="session")))
            plans = []
            for row in reversed(cur.fetchall()):
                plan = row["plan"]
                # A superseding worker may have fenced the old run before it
                # could persist final step transitions. Reflect that DB fact.
                if row["status"] in {"interrupted", "failed"} and plan["status"] == "running":
                    status = "cancelled" if row["status"] == "interrupted" else "failed"
                    plan.update(status=status, revision=plan["revision"] + 1, change_reason="本次执行已停止。")
                    for step in plan["steps"]:
                        if step["status"] in {"pending", "running"}:
                            step.update(status=status, summary="此步骤未完成。")
                plans.append(plan)
            return plans

    def _apply(self, scope, owner, operation, expected_version, trip, preferences, action="update"):
        """Receipt and business data commit together; models cannot supply identities or SQL."""
        from psycopg.types.json import Jsonb
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("""SELECT mutations, owner, cancel_requested, status FROM supervisor_runs
                WHERE user_id=%s AND request_id=%s AND session_id=%s FOR UPDATE""", self._key(scope))
            run = cur.fetchone()
            if not run or run["owner"] != owner or run["cancel_requested"] or run["status"] != "running":
                raise RuntimeStopped("执行已失效，变更未提交")
            receipts = dict(run["mutations"] or {})
            if operation in receipts:
                return receipts[operation]
            # Serialize creation too (SELECT FOR UPDATE cannot lock a missing row).
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("supervisor:" + scope.user_id,))
            cur.execute("""SELECT status, context_data FROM active_trip_contexts
                WHERE user_id=%s AND session_id=%s FOR UPDATE""", (scope.user_id, scope.session_id))
            row = cur.fetchone()
            current = dict(row["context_data"] or {}) if row and row["status"] not in {"completed", "cancelled"} else {}
            if current:
                current["status"] = row["status"]
            if expected_version != trip_version(current):
                raise ToolRejected("行程已更新，必须重新整理后再提交")
            if action == "new":
                current = {}
            if trip or action in {"new", "cancel"}:
                current = {**current, **trip, "status": "active"}
                current.setdefault("_trip_id", uuid4().hex)
                if action == "cancel":
                    current["status"] = "cancelled"
                cur.execute("""INSERT INTO active_trip_contexts (user_id, session_id, status, context_data)
                    VALUES (%s,%s,%s,%s) ON CONFLICT (user_id,session_id) DO UPDATE
                    SET status=EXCLUDED.status, context_data=EXCLUDED.context_data, updated_at=NOW(),
                    completed_at=CASE WHEN EXCLUDED.status='cancelled' THEN NOW() ELSE NULL END""",
                    (scope.user_id, scope.session_id, current["status"], Jsonb(current)))
                cur.execute("""INSERT INTO supervisor_trip_records (trip_id,user_id,session_id,record_state,context_data)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT (trip_id) DO UPDATE SET
                    record_state=EXCLUDED.record_state,context_data=EXCLUDED.context_data,updated_at=NOW()
                    WHERE supervisor_trip_records.user_id=EXCLUDED.user_id
                    AND supervisor_trip_records.session_id=EXCLUDED.session_id""",
                    (current["_trip_id"], scope.user_id, self._key(scope)[2], "cancelled" if action == "cancel" else "planned", Jsonb(current)))
                if cur.rowcount != 1:
                    raise ToolRejected("行程记录身份不匹配，变更未提交")
                if action == "cancel":
                    current = {}
            for key, value in preferences.items():
                # Only hard-coded schema columns can reach SQL interpolation.
                column = {**PREFERENCE_SCALAR_COLUMNS, **PREFERENCE_LIST_COLUMNS}[key]
                stored = Jsonb(value) if key in PREFERENCE_LIST_COLUMNS else value
                cur.execute(f"""INSERT INTO user_travel_preferences (user_id,{column},preference_updated_at)
                    VALUES (%s,%s,jsonb_build_object(%s::text,to_jsonb(NOW())))
                    ON CONFLICT (user_id) DO UPDATE SET {column}=EXCLUDED.{column},
                    preference_updated_at=user_travel_preferences.preference_updated_at || EXCLUDED.preference_updated_at,
                    updated_at=NOW()""", (scope.user_id, stored, key))
                cur.execute("""INSERT INTO user_preferences (user_id,pref_type,pref_value,updated_at)
                    VALUES (%s,%s,%s,NOW()) ON CONFLICT (user_id,pref_type) DO UPDATE
                    SET pref_value=EXCLUDED.pref_value,updated_at=NOW()""", (scope.user_id, key, Jsonb(value)))
            receipt = {"applied": True, "trip": current, "version": trip_version(current), "preferences_updated": bool(preferences)}
            receipts[operation] = receipt
            cur.execute("UPDATE supervisor_runs SET mutations=%s, updated_at=NOW() WHERE user_id=%s AND request_id=%s", (Jsonb(receipts), *self._key(scope)[:2]))
            return receipt
