"""
WebUI 路由拆分后的最小契约测试。

使用 httpx.ASGITransport 直接请求 ASGI app，避开当前环境里
fastapi.testclient.TestClient 的线程 portal 兼容问题。
"""
import json
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from webui_new.auth.deps import require_path_user
from webui_new.auth.storage import User
from webui_new.manager import HommeyWebInstance
from webui_new.server import app, jinja_env, manager


def _error(body):
    return body["error"]


@pytest.fixture
def anyio_backend():
    """只跑 asyncio 后端；项目测试环境没有安装 trio。"""
    return "asyncio"


@pytest.fixture
async def client():
    # 本文件聚焦业务错误响应契约；用 dependency override 绕过鉴权直达业务逻辑。
    async def _bypass_auth():
        return User(id=0, email="test@example.com", password_hash="", created_at="2026-01-01T00:00:00+00:00")

    app.dependency_overrides[require_path_user] = _bypass_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _all_route_paths():
    """收集所有路由路径，包括 APIRouter 内部的子路由。"""
    paths = set()
    for route in app.routes:
        # APIRouter 通过 app.include_router() 注册后会变成 _IncludedRouter
        if type(route).__name__ == "_IncludedRouter":
            for sub in route.original_router.routes:
                if hasattr(sub, "path"):
                    paths.add(sub.path)
        elif hasattr(route, "path"):
            paths.add(route.path)
    return paths


def test_webui_routes_are_registered():
    """防止拆分 router 后漏注册原有 API path。"""
    paths = _all_route_paths()

    assert "/" in paths
    assert "/login" in paths
    assert "/chat/{user_id}" in paths
    assert "/api/{user_id}/init" in paths
    assert "/api/{user_id}/status" in paths
    assert "/api/{user_id}/is-new" in paths
    assert "/api/{user_id}/summary" in paths
    assert "/api/{user_id}/onboarding" in paths
    assert "/api/{user_id}/onboarding/preference" in paths
    assert "/api/{user_id}/chat" in paths
    assert "/api/{user_id}/chat/stream" in paths
    assert "/api/{user_id}/attachments" in paths
    assert "/api/{user_id}/attachments/{attachment_id}" in paths
    assert "/api/{user_id}/sessions" in paths
    assert "/api/{user_id}/sessions/{session_id}" in paths
    assert "/api/{user_id}/sessions/{session_id}/activate" in paths
    assert "/api/{user_id}/history" in paths
    assert "/api/{user_id}/trip/active" in paths
    assert "/api/knowledge/documents" in paths
    assert "/api/knowledge/documents/{document_id:path}" in paths
    assert "/api/knowledge/refresh" in paths
    assert "/api/knowledge/refresh/status" in paths
    assert "/admin/skills" in paths
    assert "/api/admin/skills" in paths
    assert "/api/admin/skills/{skill_name}" in paths
    assert "/api/admin/skills/{skill_name}/enabled" in paths


def test_html_templates_escape_untrusted_values():
    rendered = jinja_env.from_string("{{ value }}").render(
        value='\"><img src=x onerror=alert(1)>'
    )

    assert "<img" not in rendered
    assert "&lt;img" in rendered


@pytest.mark.anyio
async def test_chat_page_rejects_non_numeric_user_id(client):
    response = await client.get("/chat/not-a-user-id")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")


@pytest.mark.anyio
async def test_login_error_has_code_and_request_id(client):
    response = await client.post("/login", json={"user_id": "  "}, headers={"X-Request-ID": "rid-login"})

    assert response.status_code == 400
    assert response.headers["X-Request-ID"] == "rid-login"
    assert response.json() == {
        "success": False,
        "error": {
            "code": "BAD_REQUEST",
            "message": "请输入用户 ID",
            "details": {},
            "request_id": "rid-login",
        },
    }


@pytest.mark.anyio
async def test_init_error_hides_raw_exception(client, monkeypatch):
    async def failing_initialize_user(_user_id):
        raise RuntimeError("secret-token leaked upstream detail")

    monkeypatch.setattr(manager, "initialize_user", failing_initialize_user)

    response = await client.post("/api/u1/init", headers={"X-Request-ID": "rid-init"})

    assert response.status_code == 500
    body = response.json()
    assert _error(body)["code"] == "INIT_FAILED"
    assert _error(body)["request_id"] == "rid-init"
    assert _error(body)["message"] == "初始化失败，请稍后刷新页面重试"
    assert "secret-token" not in str(body)


@pytest.mark.anyio
async def test_chat_not_initialized_route_defers_to_manager_lazy_init(client, monkeypatch):
    """路由层不再预检查 NOT_INITIALIZED：未初始化/跨 worker（本 worker 无实例）时，
    请求直达 manager.process_message，由其内部懒初始化。"""
    calls = []

    async def fake_process_message(user_id, message, *, request_id=None, attachment_ids=None):
        calls.append((user_id, message, request_id, attachment_ids))
        return {"response": "ok", "agents": [], "preferences_updated": False}

    monkeypatch.setattr(manager, "get", lambda _user_id: None)  # 模拟本 worker 无实例
    monkeypatch.setattr(manager, "process_message", fake_process_message)

    response = await client.post(
        "/api/u1/chat",
        json={"message": "hello"},
        headers={"X-Request-ID": "rid-chat"},
    )

    assert response.status_code == 200
    assert calls == [("u1", "hello", "rid-chat", [])]  # ChatRequest.attachment_ids 默认 []


@pytest.mark.anyio
async def test_chat_route_forwards_requested_session(client, monkeypatch):
    calls = []

    async def fake_process_message(
        user_id, message, *, request_id=None, attachment_ids=None, session_id=None
    ):
        calls.append((user_id, message, request_id, attachment_ids, session_id))
        return {"response": "ok", "agents": [], "preferences_updated": False}

    monkeypatch.setattr(manager, "process_message", fake_process_message)

    response = await client.post(
        "/api/u1/chat",
        json={"message": "hello", "session_id": "session-a"},
        headers={"X-Request-ID": "rid-chat-session"},
    )

    assert response.status_code == 200
    assert calls == [("u1", "hello", "rid-chat-session", [], "session-a")]


@pytest.mark.anyio
async def test_stream_route_forwards_requested_session(client, monkeypatch):
    calls = []

    async def fake_stream_message(
        user_id, message, *, request_id=None, attachment_ids=None, session_id=None
    ):
        calls.append((user_id, message, request_id, attachment_ids, session_id))
        yield {"type": "done", "preferences_updated": False, "timings": {}}

    monkeypatch.setattr(manager, "stream_message", fake_stream_message)

    response = await client.post(
        "/api/u1/chat/stream",
        json={"message": "hello", "session_id": "session-a"},
        headers={"X-Request-ID": "rid-stream-session"},
    )

    assert response.status_code == 200
    assert calls == [("u1", "hello", "rid-stream-session", [], "session-a")]


@pytest.mark.anyio
async def test_chat_route_forwards_explicit_enhanced_retrieval_mode(client, monkeypatch):
    calls = []

    async def fake_process_message(
        user_id, message, *, request_id=None, attachment_ids=None, retrieval_mode="standard"
    ):
        calls.append((user_id, message, request_id, attachment_ids, retrieval_mode))
        return {"response": "ok", "agents": [], "preferences_updated": False}

    monkeypatch.setattr(manager, "get", lambda _user_id: None)
    monkeypatch.setattr(manager, "process_message", fake_process_message)

    response = await client.post(
        "/api/u1/chat",
        json={"message": "发票丢了怎么报销", "retrieval_mode": "enhanced"},
        headers={"X-Request-ID": "rid-hyde"},
    )

    assert response.status_code == 200
    assert calls == [("u1", "发票丢了怎么报销", "rid-hyde", [], "enhanced")]


@pytest.mark.anyio
async def test_chat_lazy_init_failure_still_returns_not_initialized(client, monkeypatch):
    """manager 懒初始化后仍无实例（初始化失败）时，NOT_INITIALIZED 仍以 400 透传给客户端。"""
    async def fake_process_message(user_id, message, *, request_id=None, attachment_ids=None):
        from webui_new.core.errors import BusinessError
        raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")

    monkeypatch.setattr(manager, "get", lambda _user_id: None)
    monkeypatch.setattr(manager, "process_message", fake_process_message)

    response = await client.post(
        "/api/u1/chat",
        json={"message": "hello"},
        headers={"X-Request-ID": "rid-chat"},
    )

    assert response.status_code == 400
    assert _error(response.json())["code"] == "NOT_INITIALIZED"
    assert _error(response.json())["request_id"] == "rid-chat"


@pytest.mark.anyio
async def test_validation_error_contract(client):
    response = await client.post("/api/u1/chat", json={"message": 123}, headers={"X-Request-ID": "rid-validation"})

    assert response.status_code == 422
    assert response.json() == {
        "success": False,
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "请求参数格式不正确，请检查后重试",
            "details": {},
            "request_id": "rid-validation",
        },
    }


@pytest.mark.anyio
async def test_empty_message_error_contract(client, monkeypatch):
    class FakeInstance:
        initialized = True

    monkeypatch.setattr(manager, "get", lambda _user_id: FakeInstance())

    response = await client.post(
        "/api/u1/chat",
        json={"message": "  "},
        headers={"X-Request-ID": "rid-empty"},
    )

    assert response.status_code == 400
    assert _error(response.json())["code"] == "EMPTY_MESSAGE"
    assert _error(response.json())["message"] == "请输入消息或添加附件"
    assert _error(response.json())["request_id"] == "rid-empty"


@pytest.mark.anyio
async def test_empty_text_with_attachment_is_valid_chat_input(client, monkeypatch):
    calls = []

    class FakeInstance:
        initialized = True

    async def fake_process_message(user_id, message, *, request_id=None, attachment_ids=None):
        calls.append((user_id, message, request_id, attachment_ids))
        return {"response": "ok", "agents": [], "preferences_updated": False}

    monkeypatch.setattr(manager, "get", lambda _user_id: FakeInstance())
    monkeypatch.setattr(manager, "process_message", fake_process_message)
    response = await client.post(
        "/api/u1/chat",
        json={"message": "", "attachment_ids": ["att_1"]},
        headers={"X-Request-ID": "rid-attachment-chat"},
    )

    assert response.status_code == 200
    assert calls == [("u1", "", "rid-attachment-chat", ["att_1"])]


@pytest.mark.anyio
async def test_session_history_endpoints_contract(client, monkeypatch):
    calls = []

    class FakeInstance:
        initialized = True
        session_id = "s1"

        def list_chat_sessions(self):
            return [{"session_id": "s1", "title": "上海安排"}]

        def start_new_chat_session(self):
            calls.append(("new",))
            return "s2"

        def activate_chat_session(self, session_id):
            calls.append(("activate", session_id))
            return {
                "session_id": session_id,
                "messages": [{"role": "user", "content": "上海出差"}],
            }

        def rename_chat_session(self, session_id, title):
            calls.append(("rename", session_id, title))

        def delete_chat_session(self, session_id):
            calls.append(("delete", session_id))
            return "s2"

        def clear_chat_history(self):
            calls.append(("clear",))
            return "s3"

    instance = FakeInstance()

    async def fake_state_operation(_user_id, operation):
        return operation(instance)

    monkeypatch.setattr(manager, "run_user_state_operation", fake_state_operation)

    listed = await client.get("/api/u1/sessions")
    created = await client.post("/api/u1/sessions")
    activated = await client.post("/api/u1/sessions/s1/activate")
    renamed = await client.patch("/api/u1/sessions/s1", json={"title": " 新名字 "})
    deleted = await client.delete("/api/u1/sessions/s1")
    cleared = await client.delete("/api/u1/history")

    assert listed.json()["sessions"][0]["title"] == "上海安排"
    assert created.json() == {"session_id": "s2"}
    assert activated.json()["messages"][0]["content"] == "上海出差"
    assert renamed.json() == {"session_id": "s1", "title": "新名字"}
    assert deleted.json() == {"active_session_id": "s2"}
    assert cleared.json() == {"active_session_id": "s3"}
    assert calls == [
        ("new",),
        ("activate", "s1"),
        ("rename", "s1", "新名字"),
        ("delete", "s1"),
        ("clear",),
    ]


@pytest.mark.anyio
async def test_empty_session_title_error_contract(client, monkeypatch):
    class FakeInstance:
        initialized = True

    monkeypatch.setattr(manager, "get", lambda _user_id: FakeInstance())

    response = await client.patch(
        "/api/u1/sessions/s1",
        json={"title": "   "},
        headers={"X-Request-ID": "rid-session-title"},
    )

    assert response.status_code == 400
    assert _error(response.json())["code"] == "EMPTY_SESSION_TITLE"
    assert _error(response.json())["request_id"] == "rid-session-title"


@pytest.mark.anyio
async def test_onboarding_invalid_preference_contract(client, monkeypatch):
    class FakeInstance:
        initialized = True

        async def save_onboarding_preference(self, _key, _value):
            raise ValueError("secret-token unsupported key")

    @asynccontextmanager
    async def fake_state_scope(_user_id):
        yield FakeInstance()

    monkeypatch.setattr(manager, "user_state_scope", fake_state_scope)

    response = await client.post(
        "/api/u1/onboarding/preference",
        json={"key": "bad", "value": "x"},
        headers={"X-Request-ID": "rid-onboarding"},
    )

    assert response.status_code == 400
    body = response.json()
    assert _error(body)["code"] == "INVALID_ONBOARDING_PREFERENCE"
    assert _error(body)["message"] == "偏好项不支持，请刷新页面后重试"
    assert _error(body)["request_id"] == "rid-onboarding"
    assert "secret-token" not in str(body)


@pytest.mark.anyio
async def test_onboarding_save_failed_contract(client, monkeypatch):
    class FakeInstance:
        initialized = True

        async def save_onboarding_preference(self, _key, _value):
            raise RuntimeError("password=super-secret")

    @asynccontextmanager
    async def fake_state_scope(_user_id):
        yield FakeInstance()

    monkeypatch.setattr(manager, "user_state_scope", fake_state_scope)

    response = await client.post(
        "/api/u1/onboarding/preference",
        json={"key": "home_location", "value": "上海"},
        headers={"X-Request-ID": "rid-onboarding-fail"},
    )

    assert response.status_code == 500
    body = response.json()
    assert _error(body)["code"] == "ONBOARDING_SAVE_FAILED"
    assert _error(body)["message"] == "保存初始化偏好失败，请稍后重试"
    assert _error(body)["request_id"] == "rid-onboarding-fail"
    assert "super-secret" not in str(body)


@pytest.mark.anyio
async def test_stream_error_event_contract(client, monkeypatch):
    class FakeInstance:
        initialized = True

        async def stream_message(self, _message, request_id=None):
            yield {"type": "status", "message": "processing"}
            raise RuntimeError("api_key=secret-stream")

    instance = FakeInstance()
    monkeypatch.setattr(manager, "get", lambda _user_id: instance)

    async def fake_stream_message(user_id, message, *, request_id=None, attachment_ids=None):
        async for event in instance.stream_message(message, request_id=request_id):
            yield event

    monkeypatch.setattr(manager, "stream_message", fake_stream_message)

    response = await client.post(
        "/api/u1/chat/stream",
        json={"message": "hello"},
        headers={"X-Request-ID": "rid-stream"},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1] == {
        "type": "error",
        "code": "STREAM_FAILED",
        "message": "处理失败，请稍后重试",
        "request_id": "rid-stream",
        "retryable": True,
    }
    assert "secret-stream" not in response.text


@pytest.mark.anyio
async def test_stream_optional_agent_error_returns_partial_success(client, monkeypatch):
    # 阶段 4 后走 DAG 管线：continue 步骤失败不中止，composer 降级出卡片。
    from core.orchestration.fallback_composer import FallbackComposer
    from core.orchestration.models import ExecutionTask, PipelineOutput, TaskResult

    class FastRoute:
        def to_intention_data(self, _message):
            return {
                "routing": {"should_call_skill": True},
                "intents": [{
                    "type": "information_query",
                    "confidence": 0.95,
                    "should_call_skill": True,
                }],
            }

    class Memory:
        def add_message(self, *_args):
            pass

    class Orchestrator:
        def prepare_context(self, _intention_data, *, request_context=None):
            return {"rewritten_query": "查南京天气"}

        def record_task_results(self, _intention_data, _results):
            pass

    class FakePipeline:
        async def run(self, **kwargs):
            task = ExecutionTask(
                task_id="information_query-information_query",
                intent="information_query",
                query="查询南京天气",
                entities={"destination": "南京"},
                agent_name="information_query",
                priority=1,
                failure_policy="continue",
                display_order=0,
            )
            result = TaskResult(
                task_id="information_query-information_query",
                intent="information_query",
                agent_name="information_query",
                status="error",
                data={"results": {"error": "Error in input stream"}},
                error_code="INFORMATION_QUERY_UNAVAILABLE",
                error_message="天气服务暂时不可用",
                display_order=0,
            )
            return PipelineOutput(
                tasks=[task],
                execution_tasks=[task],
                results=[result],
                answer_document=FallbackComposer().compose([task], [result]),
            )

    instance = HommeyWebInstance("u1")
    instance.initialized = True
    instance.memory_manager = Memory()
    instance.orchestrator = Orchestrator()
    instance.multi_intent_pipeline = FakePipeline()
    monkeypatch.setattr(instance, "_route_without_context", lambda _message: FastRoute())
    monkeypatch.setattr(manager, "get", lambda _user_id: instance)

    async def fake_stream_message(user_id, message, *, request_id=None, attachment_ids=None):
        async for event in instance.stream_message(
            message, request_id=request_id, attachment_ids=attachment_ids
        ):
            yield event

    monkeypatch.setattr(manager, "stream_message", fake_stream_message)

    response = await client.post(
        "/api/u1/chat/stream",
        json={"message": "查询南京天气"},
        headers={"X-Request-ID": "rid-agent-stream"},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["type"] == "done"
    documents = [event["document"] for event in events if event.get("type") == "answer_document"]
    assert len(documents) == 1
    assert "天气服务暂时不可用" in documents[0]["plain_text"]
    assert not any(event.get("type") == "chunk" for event in events)
    assert "Error in input stream" not in response.text


@pytest.mark.anyio
async def test_stream_required_agent_error_is_normalized(client, monkeypatch):
    # abort 步骤失败由 _raise_on_pipeline_errors 上抛为公共错误流。
    from core.orchestration.models import ExecutionTask, PipelineOutput, TaskResult

    class FastRoute:
        def to_intention_data(self, _message):
            return {
                "routing": {"should_call_skill": True},
                "intents": [{
                    "type": "itinerary_planning",
                    "confidence": 0.95,
                    "should_call_skill": True,
                }],
            }

    class Memory:
        def add_message(self, *_args):
            pass

    class Orchestrator:
        def prepare_context(self, _intention_data, *, request_context=None):
            return {"rewritten_query": "收集出差信息"}

        def record_task_results(self, _intention_data, _results):
            pass

    class FakePipeline:
        async def run(self, **kwargs):
            task = ExecutionTask(
                task_id="event_collection-event_collection",
                intent="event_collection",
                query="收集出差信息",
                entities={"destination": "南京"},
                agent_name="event_collection",
                priority=1,
                failure_policy="abort",
                display_order=0,
            )
            result = TaskResult(
                task_id="event_collection-event_collection",
                intent="event_collection",
                agent_name="event_collection",
                status="error",
                data={"error": "Error in input stream"},
                error_code="AGENT_EXECUTION_FAILED",
                error_message="internal failure",
                display_order=0,
            )
            return PipelineOutput(tasks=[task], execution_tasks=[task], results=[result])

    instance = HommeyWebInstance("u1")
    instance.initialized = True
    instance.memory_manager = Memory()
    instance.orchestrator = Orchestrator()
    instance.multi_intent_pipeline = FakePipeline()
    monkeypatch.setattr(instance, "_route_without_context", lambda _message: FastRoute())
    monkeypatch.setattr(manager, "get", lambda _user_id: instance)

    async def fake_stream_message(user_id, message, *, request_id=None, attachment_ids=None):
        async for event in instance.stream_message(
            message, request_id=request_id, attachment_ids=attachment_ids
        ):
            yield event

    monkeypatch.setattr(manager, "stream_message", fake_stream_message)

    response = await client.post(
        "/api/u1/chat/stream",
        json={"message": "我要去出差"},
        headers={"X-Request-ID": "rid-agent-stream-fatal"},
    )

    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1] == {
        "type": "error",
        "code": "AGENT_EXECUTION_FAILED",
        "message": "处理失败，请稍后重试。",
        "request_id": "rid-agent-stream-fatal",
        "retryable": True,
    }
    assert "internal failure" not in response.text
    assert "Error in input stream" not in response.text


@pytest.mark.anyio
async def test_middleware_catch_all_error_contract(client):
    path = "/__test_unhandled_error"

    async def failing_route():
        raise RuntimeError("token=secret-route")

    if path not in _all_route_paths():
        app.add_api_route(path, failing_route, methods=["GET"])

    response = await client.get(path, headers={"X-Request-ID": "rid-catch-all"})

    assert response.status_code == 500
    body = response.json()
    assert _error(body)["code"] == "INTERNAL_ERROR"
    assert _error(body)["message"] == "系统暂时不可用，请稍后再试"
    assert _error(body)["request_id"] == "rid-catch-all"
    assert "secret-route" not in str(body)


@pytest.mark.anyio
async def test_manager_intention_error_does_not_return_raw_exception(monkeypatch):
    async def failing_reply(_messages):
        raise RuntimeError("secret-token from intent")

    async def fake_build_context(_message):
        return []

    instance = HommeyWebInstance("u1")
    instance.initialized = True
    instance.circuit_breaker = None
    instance.intention_agent = type("Agent", (), {"reply": failing_reply})()
    monkeypatch.setattr(instance, "_build_context", fake_build_context)

    with pytest.raises(Exception) as exc_info:
        await instance.process_message("帮我规划下周出差")

    assert getattr(exc_info.value, "code") == "INTENTION_FAILED"
    assert getattr(exc_info.value, "message") == "处理请求时出错，请稍后重试。"
    assert "secret-token" not in getattr(exc_info.value, "message")


@pytest.mark.anyio
async def test_manager_orchestration_error_does_not_return_raw_exception(monkeypatch):
    class FastRoute:
        def to_intention_data(self, _message):
            return {
                "routing": {"should_call_skill": True},
                "intents": [{
                    "type": "itinerary_planning",
                    "confidence": 0.95,
                    "should_call_skill": True,
                }],
            }

    class Memory:
        def add_message(self, *_args):
            pass

    class Orchestrator:
        def prepare_context(self, _intention_data, *, request_context=None):
            return {"rewritten_query": "收集出差信息"}

    class FakePipeline:
        async def run(self, **_kwargs):
            raise RuntimeError("password=secret-orchestration")

    instance = HommeyWebInstance("u1")
    instance.initialized = True
    instance.circuit_breaker = None
    instance.memory_manager = Memory()
    instance.orchestrator = Orchestrator()
    instance.multi_intent_pipeline = FakePipeline()
    monkeypatch.setattr(instance, "_route_without_context", lambda _message: FastRoute())

    with pytest.raises(Exception) as exc_info:
        await instance.process_message("帮我规划下周出差")

    assert getattr(exc_info.value, "code") == "ORCHESTRATION_FAILED"
    assert getattr(exc_info.value, "message") == "调度执行失败，请稍后重试。"
    assert "secret-orchestration" not in getattr(exc_info.value, "message")
