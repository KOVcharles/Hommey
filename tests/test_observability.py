import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from utils.observability import InMemoryMetricsSink, record_alert, record_http_request, record_upstream_error
from utils.preflight import run_preflight
from utils.structured_logging import JsonFormatter
from webui_new.auth.deps import require_path_user
from webui_new.auth.storage import User
from webui_new.server import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_json_formatter_outputs_sanitized_extra_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="failed api_key=secret",
        args=(),
        exc_info=None,
    )
    record.request_id = "rid-1"
    record.debug_message = "password=super-secret"

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "ERROR"
    assert payload["request_id"] == "rid-1"
    assert "secret" not in payload["message"]
    assert "super-secret" not in payload["debug_message"]


def test_in_memory_metrics_renders_prometheus_style_text():
    sink = InMemoryMetricsSink()
    sink.increment("hommey_errors_total", {"component": "llm", "error_code": "CIRCUIT_OPEN"})
    sink.observe("hommey_http_request_duration_ms", 12.5, {"route": "/chat", "status_code": "500"})

    text = sink.render_text()

    assert 'hommey_errors_total{component="llm",error_code="CIRCUIT_OPEN"} 1' in text
    assert 'hommey_http_request_duration_ms_count{route="/chat",status_code="500"} 1' in text
    assert 'hommey_http_request_duration_ms_sum{route="/chat",status_code="500"} 12.500000' in text


def test_alert_signals_are_rendered_as_metrics():
    sink = InMemoryMetricsSink()
    import utils.observability as observability

    original = observability.metrics
    observability.metrics = sink
    try:
        record_http_request("/api/u/chat", "POST", 500, 10)
        record_upstream_error("llm", TimeoutError("timed out"), retryable=True)
        record_alert("manual", "rag", "warning", "manual alert")
    finally:
        observability.metrics = original

    text = sink.render_text()
    assert 'hommey_alerts_total{alert="http_5xx",component="http",severity="warning"} 1' in text
    assert 'hommey_alerts_total{alert="upstream_timeout",component="llm",severity="warning"} 1' in text
    assert 'hommey_alerts_total{alert="manual",component="rag",severity="warning"} 1' in text


@pytest.mark.anyio
async def test_preflight_is_componentized_and_does_not_require_network(monkeypatch, tmp_path):
    import utils.preflight as preflight

    monkeypatch.setitem(preflight.LLM_CONFIG, "api_key", "")
    monkeypatch.setitem(preflight.RAG_CONFIG, "embedding_backend", "siliconflow")
    monkeypatch.setitem(preflight.RAG_CONFIG, "embedding_api_key", "")
    monkeypatch.setitem(preflight.RAG_CONFIG, "embedding_base_url", "https://api.siliconflow.cn/v1")
    monkeypatch.setitem(preflight.RAG_CONFIG, "knowledge_base_path", str(tmp_path / "rag-store"))
    monkeypatch.setitem(preflight.RAG_CONFIG, "vector_backend", "milvus_lite")
    monkeypatch.setitem(preflight.MEMORY_CONFIG["short_term"], "backend", "memory")
    monkeypatch.setitem(preflight.MEMORY_CONFIG["long_term"], "backend", "file")
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    result = await run_preflight(include_network=False)
    checks = {item["name"]: item for item in result["checks"]}

    assert result["ok"] is False
    assert checks["api_key"]["ok"] is False
    assert checks["rag_embedding"]["ok"] is False
    assert checks["milvus_data_dir"]["ok"] is True
    assert checks["runtime_topology"]["ok"] is True
    assert all("duration_ms" in item for item in result["checks"])


@pytest.mark.anyio
async def test_observability_endpoints(monkeypatch):
    async def fake_preflight(include_network=False):
        return {"ok": True, "checks": [{"name": "api_key", "component": "llm", "ok": True, "message": "ok", "duration_ms": 1, "details": {}}]}

    monkeypatch.setattr("webui_new.server.run_preflight", fake_preflight)
    record_http_request("/unit", "GET", 200, 3)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
        metrics = await client.get("/metrics")

    assert health.json()["ok"] is True
    assert isinstance(health.json()["worker_pid"], int)
    assert ready.json()["ok"] is True
    assert "hommey_http_requests_total" in metrics.text


@pytest.mark.anyio
async def test_readyz_returns_503_when_a_required_check_fails(monkeypatch):
    async def fake_preflight(include_network=False):
        return {"ok": False, "checks": [{"name": "redis_ping", "ok": False}]}

    monkeypatch.setattr("webui_new.server.run_preflight", fake_preflight)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["ok"] is False


@pytest.mark.anyio
async def test_error_log_contains_required_context_fields(caplog):
    caplog.set_level(logging.WARNING)

    # 该测试聚焦 validation_error 日志字段；用 dependency override 绕过鉴权，
    # 使空请求体能走到 body 校验阶段产出 422（而非先被 require_path_user 拦为 401）。
    async def _bypass_auth():
        return User(id=0, email="test@example.com", password_hash="", created_at="2026-01-01T00:00:00+00:00")

    app.dependency_overrides[require_path_user] = _bypass_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            await client.post("/api/u1/chat", json={"message": 123}, headers={"X-Request-ID": "rid-log"})
    finally:
        app.dependency_overrides.clear()

    matching = [record for record in caplog.records if record.getMessage() == "validation_error"]
    assert matching
    record = matching[-1]
    assert record.request_id == "rid-log"
    assert record.user_id == "u1"
    assert record.route == "/api/u1/chat"
    assert record.method == "POST"
    assert record.status_code == 422
    assert record.error_code == "VALIDATION_ERROR"
    assert record.component == "http"
    assert isinstance(record.duration_ms, int)
