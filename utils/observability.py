"""Small observability primitives with replaceable sinks."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Protocol

from utils.logging_safety import sanitize_for_log

logger = logging.getLogger(__name__)


COMPONENT_LLM = "llm"
COMPONENT_MCP = "mcp"
COMPONENT_REDIS = "redis"
COMPONENT_POSTGRES = "postgres"
COMPONENT_RAG = "rag"
COMPONENT_HTTP = "http"

ERROR_TIMEOUT = "timeout"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_AUTH_FAILED = "auth_failed"
ERROR_CONNECTION_FAILED = "connection_failed"
ERROR_BAD_RESPONSE = "bad_response"
ERROR_CIRCUIT_OPEN = "circuit_open"
ERROR_DEPENDENCY_MISSING = "dependency_missing"
ERROR_STORAGE_UNAVAILABLE = "storage_unavailable"
ERROR_UNKNOWN = "unknown"


class MetricsSink(Protocol):
    def increment(self, name: str, labels: Mapping[str, str] | None = None, amount: int = 1) -> None:
        ...

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        ...

    def render_text(self) -> str:
        ...


@dataclass
class InMemoryMetricsSink:
    """Dependency-free metrics sink suitable for tests and simple deployments."""

    counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = field(default_factory=lambda: defaultdict(int))
    observations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = field(default_factory=lambda: defaultdict(list))

    def increment(self, name: str, labels: Mapping[str, str] | None = None, amount: int = 1) -> None:
        self.counters[(name, _label_items(labels))] += amount

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        self.observations[(name, _label_items(labels))].append(float(value))

    def render_text(self) -> str:
        lines: list[str] = []
        for (name, labels), value in sorted(self.counters.items()):
            lines.append(f"{name}{_format_labels(labels)} {value}")
        for (name, labels), values in sorted(self.observations.items()):
            count = len(values)
            total = sum(values)
            lines.append(f"{name}_count{_format_labels(labels)} {count}")
            lines.append(f"{name}_sum{_format_labels(labels)} {total:.6f}")
        return "\n".join(lines) + ("\n" if lines else "")


metrics: MetricsSink = InMemoryMetricsSink()


def classify_upstream_error(exc: BaseException) -> str:
    """Classify external dependency failures without importing provider SDKs."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ERROR_TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return ERROR_CONNECTION_FAILED

    text = str(exc).lower()
    if "circuit" in text and "open" in text:
        return ERROR_CIRCUIT_OPEN
    if "timeout" in text or "timed out" in text:
        return ERROR_TIMEOUT
    if "rate limit" in text or "too many requests" in text or "429" in text:
        return ERROR_RATE_LIMITED
    if "unauthorized" in text or "forbidden" in text or "401" in text or "403" in text or "api key" in text:
        return ERROR_AUTH_FAILED
    if "connection" in text or "connect" in text or "refused" in text:
        return ERROR_CONNECTION_FAILED
    if "not installed" in text or "no module named" in text:
        return ERROR_DEPENDENCY_MISSING
    if "postgres" in text or "redis" in text or "storage" in text:
        return ERROR_STORAGE_UNAVAILABLE
    if "500" in text or "502" in text or "503" in text or "504" in text or "bad response" in text:
        return ERROR_BAD_RESPONSE
    return ERROR_UNKNOWN


def record_http_request(route: str, method: str, status_code: int, duration_ms: int) -> None:
    labels = {
        "route": _safe_label(route),
        "method": method.upper(),
        "status_code": str(status_code),
    }
    metrics.increment("hommey_http_requests_total", labels)
    metrics.observe("hommey_http_request_duration_ms", duration_ms, labels)
    if status_code >= 500:
        record_alert("http_5xx", COMPONENT_HTTP, "warning", f"{method.upper()} {route} returned {status_code}")


def record_app_error(component: str, error_code: str, status_code: int) -> None:
    metrics.increment(
        "hommey_errors_total",
        {
            "component": _safe_label(component),
            "error_code": _safe_label(error_code),
            "status_code": str(status_code),
        },
    )


def record_upstream_error(component: str, exc: BaseException | str, retryable: bool = False) -> str:
    category = classify_upstream_error(exc if isinstance(exc, BaseException) else RuntimeError(exc))
    metrics.increment(
        "hommey_upstream_errors_total",
        {
            "component": _safe_label(component),
            "category": category,
            "retryable": str(bool(retryable)).lower(),
        },
    )
    logger.warning(
        "upstream_error",
        extra={
            "component": component,
            "error_category": category,
            "retryable": retryable,
            "error": sanitize_for_log(exc),
        },
    )
    _record_alert_for_upstream_error(component, category)
    return category


def record_alert(alert_name: str, component: str, severity: str, message: str) -> None:
    """Emit alert signals as metrics and structured logs."""
    labels = {
        "alert": _safe_label(alert_name),
        "component": _safe_label(component),
        "severity": _safe_label(severity),
    }
    metrics.increment("hommey_alerts_total", labels)
    logger.error(
        "alert_event",
        extra={
            "alert": alert_name,
            "component": component,
            "severity": severity,
            "alert_message": sanitize_for_log(message),
        },
    )


def render_metrics() -> str:
    return metrics.render_text()


def _label_items(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(key), _safe_label(value)) for key, value in labels.items()))


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{value}"' for key, value in labels)
    return "{" + inner + "}"


def _safe_label(value: object) -> str:
    return str(value or "unknown").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _record_alert_for_upstream_error(component: str, category: str) -> None:
    if category == ERROR_TIMEOUT:
        record_alert("upstream_timeout", component, "warning", f"{component} upstream timeout")
    elif category == ERROR_CIRCUIT_OPEN:
        record_alert("circuit_open", component, "critical", f"{component} circuit breaker is open")
    elif component == COMPONENT_POSTGRES and category in {ERROR_CONNECTION_FAILED, ERROR_STORAGE_UNAVAILABLE}:
        record_alert("db_connect_failure", component, "critical", "postgres connection failed")
