"""Readiness checks for external dependencies and local runtime assets."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from settings import LLM_CONFIG, MCP_CONFIG, MEMORY_CONFIG, OCR_CONFIG, RAG_CONFIG, RESILIENCE_CONFIG
from utils.llm_resilience import run_health_check
from utils.io_executor import run_blocking
from utils.observability import (
    COMPONENT_LLM,
    COMPONENT_MCP,
    COMPONENT_POSTGRES,
    COMPONENT_RAG,
    COMPONENT_REDIS,
    record_upstream_error,
)


@dataclass
class CheckResult:
    name: str
    component: str
    ok: bool
    message: str
    duration_ms: int
    details: dict = field(default_factory=dict)


async def run_preflight(include_network: bool = False) -> dict:
    """Run readiness checks and return a stable API-friendly summary."""
    checks: list[Callable[[], Awaitable[CheckResult]]] = [
        check_api_key,
        check_rag_embedding_config,
        check_ocr_config,
        check_runtime_topology,
        check_mcp_config,
    ]
    vector_backend = str(RAG_CONFIG.get("vector_backend") or "postgres").lower()

    if include_network:
        checks.append(check_model_service)
    # Redis 协调层（锁/信号量/熔断）是聊天路径的硬依赖，与 short_term 后端无关，
    # 因此始终探测，保证 Redis 不可用时 readiness 失败（§3.3 fail closed）。
    checks.append(check_redis)
    if (
        MEMORY_CONFIG.get("long_term", {}).get("backend") == "postgres"
        or vector_backend == "postgres"
    ):
        checks.append(check_postgres)
    if vector_backend == "postgres":
        checks.append(check_pgvector)
        if str(RAG_CONFIG.get("refresh_backend") or "postgres").lower() == "postgres":
            checks.append(check_rag_refresh_control_plane)

    results = [await check() for check in checks]
    return {
        "ok": all(result.ok for result in results),
        "checks": [result.__dict__ for result in results],
    }


async def check_api_key() -> CheckResult:
    start = time.perf_counter()
    api_key = str(LLM_CONFIG.get("api_key") or "").strip()
    ok = bool(api_key)
    return _result("api_key", COMPONENT_LLM, ok, "api key configured" if ok else "HOMMEY_API_KEY is missing", start)


async def check_model_service() -> CheckResult:
    start = time.perf_counter()
    ok, message = await run_health_check(
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        model_name=LLM_CONFIG["model_name"],
        timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
    )
    if not ok:
        record_upstream_error(COMPONENT_LLM, message, retryable=True)
    return _result("model_service", COMPONENT_LLM, ok, message, start, {"model": LLM_CONFIG.get("model_name")})


async def check_ocr_config() -> CheckResult:
    """Validate local document-OCR settings without making a paid/network call."""
    start = time.perf_counter()
    if not OCR_CONFIG.get("enabled"):
        return _result(
            "document_ocr",
            COMPONENT_RAG,
            True,
            "disabled",
            start,
            {"enabled": False},
        )
    model = str(OCR_CONFIG.get("model") or "").strip()
    base_url = str(OCR_CONFIG.get("base_url") or "").strip()
    api_key = str(OCR_CONFIG.get("api_key") or "").strip()
    max_pages = int(OCR_CONFIG.get("query_pdf_max_pages") or 0)
    ok = bool(model and base_url and api_key and max_pages > 0)
    return _result(
        "document_ocr",
        COMPONENT_RAG,
        ok,
        "configured" if ok else "document OCR configuration is incomplete",
        start,
        {
            "enabled": True,
            "model": model,
            "query_pdf_max_pages": max_pages,
        },
    )


async def check_redis() -> CheckResult:
    start = time.perf_counter()
    conf = MEMORY_CONFIG.get("short_term", {})
    try:
        import redis

        def _ping() -> None:
            client = redis.Redis(
                host=conf.get("redis_host", "127.0.0.1"),
                port=conf.get("redis_port", 6379),
                db=conf.get("redis_db", 0),
                password=conf.get("redis_password"),
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            client.ping()

        await run_blocking(_ping)
        return _result("redis_ping", COMPONENT_REDIS, True, "ok", start)
    except Exception as exc:
        record_upstream_error(COMPONENT_REDIS, exc, retryable=True)
        return _result("redis_ping", COMPONENT_REDIS, False, "redis unavailable", start)


async def check_postgres() -> CheckResult:
    start = time.perf_counter()
    dsn = RAG_CONFIG.get("postgres_dsn") or MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    if not dsn:
        return _result("postgres_connect", COMPONENT_POSTGRES, False, "HOMMEY_POSTGRES_DSN is missing", start)
    try:
        import psycopg

        def _query() -> None:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()

        await run_blocking(_query)
        return _result("postgres_connect", COMPONENT_POSTGRES, True, "ok", start)
    except Exception as exc:
        record_upstream_error(COMPONENT_POSTGRES, exc, retryable=True)
        return _result("postgres_connect", COMPONENT_POSTGRES, False, "postgres unavailable", start)


async def check_pgvector() -> CheckResult:
    """Verify pgvector schema and one queryable active RAG version."""
    start = time.perf_counter()
    dsn = RAG_CONFIG.get("postgres_dsn") or MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    collection = str(RAG_CONFIG.get("collection_name") or "business_travel_knowledge")
    expected_model = str(RAG_CONFIG.get("embedding_model") or "")
    expected_dimension = int(RAG_CONFIG.get("embedding_dimension") or 0)
    if not dsn:
        return _result("pgvector", COMPONENT_RAG, False, "PostgreSQL RAG DSN is missing", start)
    try:
        import psycopg
        from psycopg.rows import dict_row

        def _query() -> dict:
            with psycopg.connect(dsn, connect_timeout=2, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
                    extension = cur.fetchone()
                    cur.execute(
                        """
                        SELECT active_version, embedding_model, embedding_dimension,
                               index_fingerprint, schema_version
                        FROM rag_collections WHERE collection_name=%s
                        """,
                        (collection,),
                    )
                    state = cur.fetchone()
                    return {"extension": extension, "state": state}

        result = await run_blocking(_query)
        extension = result["extension"]
        state = result["state"]
        ok = bool(
            extension
            and state
            and state.get("active_version")
            and state.get("index_fingerprint")
            and state.get("embedding_model") == expected_model
            and int(state.get("embedding_dimension") or 0) == expected_dimension
        )
        return _result(
            "pgvector",
            COMPONENT_RAG,
            ok,
            "pgvector active version ready" if ok else "pgvector schema, active version, or embedding config mismatch",
            start,
            {
                "collection": collection,
                "active_version": state.get("active_version") if state else None,
                "extension_version": extension.get("extversion") if extension else None,
            },
        )
    except Exception as exc:
        record_upstream_error(COMPONENT_RAG, exc, retryable=True)
        return _result("pgvector", COMPONENT_RAG, False, "pgvector unavailable or schema missing", start)


async def check_rag_refresh_control_plane() -> CheckResult:
    """Verify durable refresh tables and a recently alive independent worker."""
    start = time.perf_counter()
    dsn = RAG_CONFIG.get("postgres_dsn") or MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    collection = str(RAG_CONFIG.get("collection_name") or "business_travel_knowledge")
    stale_seconds = max(10, int(RAG_CONFIG.get("refresh_worker_stale_seconds") or 45))
    if not dsn:
        return _result("rag_refresh_worker", COMPONENT_RAG, False, "PostgreSQL RAG DSN is missing", start)
    try:
        import psycopg
        from psycopg.rows import dict_row

        def _query() -> dict:
            with psycopg.connect(dsn, connect_timeout=2, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT to_regclass('rag_refresh_jobs') AS jobs,
                               to_regclass('rag_source_generations') AS sources,
                               to_regclass('rag_worker_heartbeats') AS workers
                        """
                    )
                    tables = cur.fetchone()
                    if not all(tables.values()):
                        return {"tables": tables, "worker": None}
                    cur.execute(
                        """
                        SELECT worker_id, last_seen_at,
                               EXTRACT(EPOCH FROM (NOW() - last_seen_at)) AS age_seconds
                        FROM rag_worker_heartbeats
                        WHERE collection_name=%s
                        ORDER BY last_seen_at DESC LIMIT 1
                        """,
                        (collection,),
                    )
                    return {"tables": tables, "worker": cur.fetchone()}

        result = await run_blocking(_query)
        worker = result["worker"]
        age = float(worker["age_seconds"]) if worker else None
        ok = bool(all(result["tables"].values()) and worker and age is not None and age <= stale_seconds)
        return _result(
            "rag_refresh_worker",
            COMPONENT_RAG,
            ok,
            "persistent RAG refresh worker ready" if ok else "RAG refresh worker missing or stale",
            start,
            {
                "collection": collection,
                "worker_id": worker.get("worker_id") if worker else None,
                "heartbeat_age_seconds": round(age, 3) if age is not None else None,
                "stale_after_seconds": stale_seconds,
            },
        )
    except Exception as exc:
        record_upstream_error(COMPONENT_RAG, exc, retryable=True)
        return _result("rag_refresh_worker", COMPONENT_RAG, False, "RAG refresh control plane unavailable", start)


async def check_rag_embedding_config() -> CheckResult:
    start = time.perf_counter()
    backend = str(RAG_CONFIG.get("embedding_backend") or "siliconflow").lower()
    if backend == "local":
        path = Path(RAG_CONFIG.get("embedding_model", "")).expanduser()
        ok = path.exists() and os.access(path, os.R_OK)
        return _result(
            "rag_embedding",
            COMPONENT_RAG,
            ok,
            "local embedding model path readable" if ok else "local RAG embedding model path is missing or unreadable",
            start,
            {"backend": backend, "path": str(path)},
        )
    api_key = str(RAG_CONFIG.get("embedding_api_key") or "").strip()
    base_url = str(RAG_CONFIG.get("embedding_base_url") or "").strip()
    ok = bool(api_key and base_url)
    return _result(
        "rag_embedding",
        COMPONENT_RAG,
        ok,
        "cloud embedding configured" if ok else "HOMMEY_EMBEDDING_API_KEY or SILICONFLOW_API_KEY is missing",
        start,
        {"backend": backend, "base_url": base_url, "model": RAG_CONFIG.get("embedding_model")},
    )


async def check_runtime_topology() -> CheckResult:
    """Reject unsafe worker counts until every required shared backend exists."""
    start = time.perf_counter()
    try:
        workers = int(os.getenv("UVICORN_WORKERS", "1"))
    except ValueError:
        workers = 0
    vector_backend = str(RAG_CONFIG.get("vector_backend") or "postgres").lower()
    long_backend = str(MEMORY_CONFIG.get("long_term", {}).get("backend") or "file").lower()
    short_backend = str(MEMORY_CONFIG.get("short_term", {}).get("backend") or "memory").lower()
    refresh_backend = str(RAG_CONFIG.get("refresh_backend") or "postgres").lower()
    source_storage = str(RAG_CONFIG.get("source_storage") or "shared_filesystem").lower()
    shared_data = vector_backend == "postgres" and long_backend == "postgres" and short_backend == "redis"
    persistent_refresh = refresh_backend == "postgres"
    shared_files = source_storage in {"shared_filesystem", "object_storage"}
    ok = workers >= 1 and (workers == 1 or (shared_data and persistent_refresh and shared_files))
    if workers > 1 and ok:
        message = "multi-worker shared-state topology ready"
    elif workers > 1:
        message = "multi-worker requires Redis, PostgreSQL RAG/memory, persistent refresh jobs, and shared files"
    elif workers < 1:
        message = "UVICORN_WORKERS must be at least 1"
    else:
        message = "single-worker topology"
    return _result(
        "runtime_topology",
        COMPONENT_RAG,
        ok,
        message,
        start,
        {
            "uvicorn_workers": workers,
            "vector_backend": vector_backend,
            "long_term_backend": long_backend,
            "short_term_backend": short_backend,
            "refresh_backend": refresh_backend,
            "source_storage": source_storage,
        },
    )


async def check_mcp_config() -> CheckResult:
    start = time.perf_counter()
    enabled_servers = []
    invalid_servers = []
    for name, server in MCP_CONFIG.get("servers", {}).items():
        if not server.get("enabled"):
            continue
        enabled_servers.append(name)
        if server.get("transport") == "stdio" and not server.get("command"):
            invalid_servers.append(name)
        if server.get("transport") == "http" and not server.get("url"):
            invalid_servers.append(name)

    ok = not invalid_servers
    message = "mcp config ok" if ok else "mcp server config invalid"
    return _result("mcp_config", COMPONENT_MCP, ok, message, start, {"enabled": enabled_servers, "invalid": invalid_servers})


def run_preflight_sync(include_network: bool = False) -> dict:
    return asyncio.run(run_preflight(include_network=include_network))


def _result(name: str, component: str, ok: bool, message: str, start: float, details: dict | None = None) -> CheckResult:
    return CheckResult(
        name=name,
        component=component,
        ok=ok,
        message=message,
        duration_ms=int((time.perf_counter() - start) * 1000),
        details=details or {},
    )
