# Error Codes

Hommey API errors use one public response contract:

```json
{
  "success": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "用户可见文案",
    "details": {},
    "request_id": "request-id"
  }
}
```

`message` is safe to show to users. Internal exception text, provider payloads,
stack traces, credentials, and debug hints must only be written to sanitized
structured logs as `debug_message`.

| Code | HTTP | Component | Retryable | User Message | Internal Meaning |
| --- | ---: | --- | --- | --- | --- |
| `BAD_REQUEST` | 400 | http | no | 请输入用户 ID | Login payload is syntactically valid but user_id is empty. |
| `VALIDATION_ERROR` | 422 | http | no | 请求参数格式不正确，请检查后重试 | FastAPI/Pydantic request validation failed. |
| `NOT_INITIALIZED` | 400 | http | no | 系统未初始化，请刷新页面 | User session has no initialized Hommey instance. |
| `EMPTY_MESSAGE` | 400 | http | no | 请输入消息 | Chat message is empty after trimming whitespace. |
| `INVALID_ONBOARDING_PREFERENCE` | 400 | http | no | 偏好项不支持，请刷新页面后重试 | Onboarding preference key is not in the allowlist. |
| `INIT_FAILED` | 500 | http | yes | 初始化失败，请稍后刷新页面重试 | Runtime initialization failed; inspect sanitized logs for debug_message. |
| `CHAT_FAILED` | 500 | http | yes | 处理失败，请稍后重试 | Unexpected non-AppError in the normal chat route. |
| `STREAM_FAILED` | 500 event | http | yes | 处理失败，请稍后重试 | Unexpected non-AppError while producing an NDJSON stream. |
| `STREAM_CANCELLED` | 499 log | http | no | n/a | Client disconnected while a streaming response generator was active. This is logged and counted, but no response body can be sent because the socket is already closed. |
| `ONBOARDING_SAVE_FAILED` | 500 | storage | yes | 保存初始化偏好失败，请稍后重试 | Preference storage write failed. |
| `CIRCUIT_OPEN` | 502 | llm | yes | 服务暂时不可用，请稍后再试。 | Circuit breaker blocked an upstream LLM call. |
| `INTENTION_FAILED` | 502 | llm | yes | 处理请求时出错，请稍后重试。 | Intention agent call failed after retry handling. |
| `INTENTION_PARSE_FAILED` | 502 | llm | no | 抱歉，我没能理解您的意思，请换一种说法试试？ | Intention agent returned non-JSON or incompatible JSON. |
| `ORCHESTRATION_FAILED` | 502 | llm | yes | 调度执行失败，请稍后重试。 | Orchestration agent call failed after retry handling. |
| `ORCHESTRATION_PARSE_FAILED` | 500 | http | yes | 解析结果失败，请稍后重试 | Orchestration output could not be parsed into the expected result object. |
| `AGENT_EXECUTION_FAILED` | 502 | llm | yes | 处理失败，请稍后重试。 | Orchestrator returned an internal agent error result such as `status=error` or a fatal `data.error`; the raw agent message is logged only as sanitized debug_message. |
| `AGENT_CALL_LIMIT_EXCEEDED` | 502 | llm | no | 本次请求的 Agent 调用次数已达到安全上限 | Per-request Agent execution budget was exhausted. |
| `EXTERNAL_CALL_LIMIT_EXCEEDED` | 502 | llm | no | 本次请求的外部调用总次数已达到安全上限 | Combined LLM, RAG, HTTP search/weather, and MCP budget was exhausted. |
| `EXTERNAL_CALL_TYPE_LIMIT_EXCEEDED` | 502 | llm | no | 本次请求的同类外部调用次数已达到安全上限 | One outbound operation type exhausted its per-type budget. |
| `REQUEST_EXECUTION_TIMEOUT` | 502 | llm | yes | 本次任务处理超时，请稍后重试。 | The complete request exceeded its configured execution deadline. |
| `INTERNAL_ERROR` | 500 | http | yes | 系统暂时不可用，请稍后再试 | Unhandled exception caught by middleware. |
| `HTTP_ERROR` | 500+ | http | yes | 请求处理失败，请稍后重试 | Starlette/FastAPI HTTPException with status >= 500. |

## Observability Fields

Every structured error log should include:

| Field | Meaning |
| --- | --- |
| `request_id` | Correlates API response, logs, stream error event, and user report. |
| `user_id` | Path user id when available. |
| `route` | Request path. |
| `method` | HTTP method. |
| `status_code` | HTTP status or equivalent stream failure status. |
| `error_code` | Public error code. |
| `component` | Logical owner such as `http`, `llm`, `redis`, `postgres`, `rag`, `mcp`, or `storage`. |
| `duration_ms` | Request duration at the point the error is logged. |
| `debug_message` | Sanitized internal detail for operators only. |

## Runtime Endpoints

| Endpoint | Purpose |
| --- | --- |
| `/healthz` | Liveness check. Returns `{"ok": true}` when the ASGI app can respond. |
| `/readyz` | Readiness check. Runs componentized preflight checks for API key, optional model service, Redis, Postgres, RAG model path, Milvus data directory, and MCP config. |
| `/metrics` | Dependency-free Prometheus-style text metrics. This can be scraped directly or replaced later by a Prometheus/OpenTelemetry sink. |

`/readyz` does not probe the model service by default. Set
`HOMMEY_PREFLIGHT_INCLUDE_NETWORK=true` when deployment readiness should include
an outbound LLM health check.

Set `HOMMEY_LOG_FORMAT=json` to emit one sanitized JSON log object per line.
The default remains text logs for local development.

## Alert Signals

The current implementation emits alert signals as both structured logs and
metrics. These are intentionally transport-neutral so they can later be wired to
Alertmanager, Datadog, cloud monitoring, or an internal notification service.

| Alert | Trigger | Component |
| --- | --- | --- |
| `http_5xx` | Any HTTP request records a status code >= 500. | `http` |
| `upstream_timeout` | An upstream error is classified as timeout. | `llm`, `mcp`, `redis`, `postgres`, or `rag` |
| `circuit_open` | An upstream error is classified as circuit open. | Usually `llm` |
| `db_connect_failure` | Postgres readiness or usage reports connection/storage failure. | `postgres` |
