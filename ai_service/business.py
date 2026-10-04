"""Typed business adapters. Credentials are request-scoped and never model inputs."""

from __future__ import annotations

import contextvars
import os
from copy import copy
from types import SimpleNamespace
from urllib.parse import quote

import httpx

from agent_runtime.contracts import RuntimeStopped, ToolRejected
from agent_runtime.services import BusinessServices
from agent_runtime.store import RunStore
from context.memory_manager import MemoryManager
from context.memory_repository import stable_uuid
from context.postgres_pool import get_postgres_pool
from settings import MEMORY_CONFIG
from utils.io_executor import run_blocking
from utils.memory_safety import sanitize_memory_value
from webui_new.core.errors import BusinessError, UpstreamError

execution_credential = contextvars.ContextVar(
    "business_execution_credential", default=None
)


class BusinessClient:
    def __init__(self, user_id):
        self.user_id = str(user_id)
        self.base_url = os.environ["HOMMEY_BUSINESS_API_URL"].rstrip("/")
        self.path = "/internal/business/users/" + quote(self.user_id, safe="")

    def call(self, method, suffix, *, params=None, body=None):
        credential = execution_credential.get()
        if not credential:
            raise RuntimeError(
                "Business operations require a scoped execution credential"
            )
        # Retry only these idempotent business endpoints, never a model or upload.
        attempts = 2 if method == "GET" or suffix in {"/mutations", "/messages"} else 1
        for attempt in range(attempts):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(8, connect=3), follow_redirects=False
                ) as client:
                    response = client.request(
                        method,
                        self.base_url + self.path + suffix,
                        headers={"Authorization": "Bearer " + credential},
                        params=params,
                        json=body,
                    )
                if response.status_code >= 400:
                    try:
                        error = response.json().get("error", {})
                    except ValueError:
                        error = {}
                    code = error.get("code", "BUSINESS_UNAVAILABLE")
                    message = error.get("message", "业务服务暂不可用")
                    if code == "RUN_STOPPED":
                        raise RuntimeStopped(message)
                    if code in {
                        "TRIP_CONFLICT",
                        "OPERATION_CONFLICT",
                        "INVALID_TRIP",
                        "INVALID_PREFERENCE",
                    }:
                        raise ToolRejected(message, code=code)
                    raise BusinessError(code, message, status_code=response.status_code)
                return response.json()
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt + 1 == attempts:
                    raise UpstreamError(
                        "BUSINESS_UNAVAILABLE",
                        "业务服务暂不可用，请重试",
                        retryable=True,
                    )


class RemoteLongTerm:
    def __init__(self, client, pool):
        self.client, self.pool = client, pool

    def get_preference(self, pref_type=None):
        values = self.client.call("GET", "/preferences")
        return values.get(pref_type) if pref_type else values

    def get_chat_history(self, limit=None, session_id=None, **kwargs):
        if not session_id:
            raise ValueError("History reads must specify a session")
        return self.client.call(
            "GET",
            "/messages",
            params={"session_id": session_id, "limit": limit or 1000},
        )

    def get_active_trip(self, session_id):
        return (
            self.client.call("GET", "/context", params={"session_id": session_id}).get(
                "trip"
            )
            or None
        )


class RemoteMemoryService:
    def __init__(self, client, long_term, session_id=None):
        self.client, self.long_term, self.session_id = client, long_term, session_id
        self.short_term = SimpleNamespace(backend="business_api")

    def for_session(self, session_id):
        self.client.call("GET", "/sessions/" + quote(str(session_id), safe=""))
        bound = copy(self)
        bound.session_id = str(session_id)
        return bound

    def append_message(self, role, content, metadata):
        return self.client.call(
            "POST",
            "/messages",
            body={
                "role": role,
                "content": content,
                "content_type": metadata.get("content_type", "text"),
                "attachment_ids": (
                    metadata.get("attachment_ids", []) if role == "user" else []
                ),
                "answer_document": metadata.get("answer_document"),
                "presentation_document": metadata.get("presentation_document"),
            },
        )


class RemoteMemoryManager(MemoryManager):
    def __init__(self, user_id, session_id=None):
        self.user_id = str(user_id)
        self.client = BusinessClient(self.user_id)
        pool = get_postgres_pool(MEMORY_CONFIG["long_term"]["postgres_dsn"])
        self.long_term = RemoteLongTerm(self.client, pool)
        self.memory_service = RemoteMemoryService(
            self.client, self.long_term, session_id
        )
        self.short_term = self.memory_service.short_term
        self.session_id = session_id
        self.current_request_id = self._current_turn_id = None


class RemoteBusinessServices(BusinessServices):
    def read_personal_profile(self, scope):
        record = self.memory.client.call("GET", "/profile")
        return (
            record["profile"]
            if record["onboarding_status"] == "completed"
            else {"basic_info": {"institution": None}}
        )

    def _memory_search(self, scope, request):
        result = self.memory.client.call(
            "GET",
            "/memory",
            params={
                "kind": request.kind,
                "query": request.query,
                "limit": request.limit,
            },
        )
        return sanitize_memory_value(result)

    async def context(self, scope):
        from agent_runtime.context_window import history_from_rows

        def read():
            data = self.memory.client.call(
                "GET", "/context", params={"session_id": scope.session_id}
            )
            rows = data.pop("messages")
            # Native model history is AI state, retained in the Python checkpoint store.
            with self.pool.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT request_id,checkpoint->'main' AS main FROM supervisor_runs
                    WHERE user_id=%s AND session_id=%s AND request_id<>%s AND retention_until>NOW()
                    ORDER BY created_at DESC LIMIT 100""",
                    (
                        scope.user_id,
                        stable_uuid(scope.session_id, namespace="session"),
                        scope.request_id,
                    ),
                )
                native = {
                    str(
                        stable_uuid(
                            row["request_id"], namespace=f"request:{scope.user_id}"
                        )
                    ): row["main"]
                    for row in cur.fetchall()
                }
            visible = {str(row["request_id"]) for row in rows if row["role"] == "user"}
            for row in rows:
                key = str(row["request_id"])
                if row.get("native_allowed") and key in visible:
                    row["native_main"] = native.get(key)
            data["recent"] = history_from_rows(rows)
            return data

        return await run_blocking(read)


class RemoteRunStore(RunStore):
    def __init__(self, pool, client):
        super().__init__(pool)
        self.client = client

    def _apply(
        self,
        scope,
        owner,
        operation,
        expected_version,
        trip,
        preferences,
        action="update",
    ):
        # Spring rechecks the run fence and owns the business transaction + receipt.
        return self.client.call(
            "POST",
            "/mutations",
            body={
                "owner": owner,
                "operation_id": operation,
                "expected_version": expected_version,
                "trip": trip,
                "preferences": preferences,
                "action": action,
            },
        )
