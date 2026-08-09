"""Readiness checks for external dependencies and local runtime assets."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from settings import LLM_CONFIG, MCP_CONFIG, MEMORY_CONFIG, RAG_CONFIG, RESILIENCE_CONFIG
from utils.llm_resilience import run_health_check
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
        check_milvus_data_dir,
        check_runtime_topology,
        check_mcp_config,
    ]

    if include_network:
        checks.append(check_model_service)
    if MEMORY_CONFIG.get("short_term", {}).get("backend") == "redis":
        checks.append(check_redis)
    if MEMORY_CONFIG.get("long_term", {}).get("backend") == "postgres":
        checks.append(check_postgres)

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


async def check_redis() -> CheckResult:
    start = time.perf_counter()
    conf = MEMORY_CONFIG.get("short_term", {})
    try:
        import redis

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
        return _result("redis_ping", COMPONENT_REDIS, True, "ok", start)
    except Exception as exc:
        record_upstream_error(COMPONENT_REDIS, exc, retryable=True)
        return _result("redis_ping", COMPONENT_REDIS, False, "redis unavailable", start)


async def check_postgres() -> CheckResult:
    start = time.perf_counter()
    dsn = MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    if not dsn:
        return _result("postgres_connect", COMPONENT_POSTGRES, False, "HOMMEY_POSTGRES_DSN is missing", start)
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return _result("postgres_connect", COMPONENT_POSTGRES, True, "ok", start)
    except Exception as exc:
        record_upstream_error(COMPONENT_POSTGRES, exc, retryable=True)
        return _result("postgres_connect", COMPONENT_POSTGRES, False, "postgres unavailable", start)


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


async def check_milvus_data_dir() -> CheckResult:
    start = time.perf_counter()
    path = Path(RAG_CONFIG.get("knowledge_base_path", "")).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".preflight-", delete=True):
            pass
        ok = os.access(path, os.R_OK | os.W_OK)
        return _result(
            "milvus_data_dir",
            COMPONENT_RAG,
            ok,
            "milvus data directory writable" if ok else "milvus data directory not writable",
            start,
            {"path": str(path)},
        )
    except Exception:
        return _result("milvus_data_dir", COMPONENT_RAG, False, "milvus data directory unavailable", start, {"path": str(path)})


async def check_runtime_topology() -> CheckResult:
    """Milvus Lite and process-local Web instances require one worker."""
    start = time.perf_counter()
    try:
        workers = int(os.getenv("UVICORN_WORKERS", "1"))
    except ValueError:
        workers = 0
    ok = workers == 1
    return _result(
        "runtime_topology",
        COMPONENT_RAG,
        ok,
        "single-worker Milvus Lite topology"
        if ok else "Milvus Lite and process-local sessions require UVICORN_WORKERS=1",
        start,
        {"uvicorn_workers": workers},
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
