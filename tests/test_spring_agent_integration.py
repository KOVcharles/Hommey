"""Opt-in integration with the NEW engineering stack, without external model calls.

Start the engineering Compose stack and set HOMMEY_SPLIT_INTEGRATION=1.
The fixture creates a temporary user in that stack and removes its own rows.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4
import time

import bcrypt
import httpx
import jwt
import psycopg
import pytest
from dotenv import dotenv_values


@pytest.fixture
def split_stack(monkeypatch):
    if os.getenv("HOMMEY_SPLIT_INTEGRATION") != "1":
        pytest.skip("Requires the isolated Spring engineering stack")
    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env.engineering")
    port = int(config.get("HOMMEY_DB_PORT", "15432"))
    business_dsn = f"postgresql://hommey:{quote(config['PG_PASSWORD'], safe='')}@127.0.0.1:{port}/hommey?connect_timeout=5"
    ai_dsn = f"postgresql://hommey_ai:{quote(config['HOMMEY_AI_DB_PASSWORD'], safe='')}@127.0.0.1:{port}/hommey?connect_timeout=5"
    email = f"integration-{uuid4().hex}@example.com"
    password = "integration-test-password"
    with psycopg.connect(business_dsn) as conn:
        user = str(
            conn.execute(
                "INSERT INTO users(email,password_hash) VALUES(%s,%s) RETURNING id",
                (email, bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()),
            ).fetchone()[0]
        )
    client = httpx.Client(base_url="http://localhost:8080", timeout=15)
    try:
        login = client.post("/auth/login", json={"email": email, "password": password})
        login.raise_for_status()
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        session = client.post(f"/api/{user}/sessions").json()["session_id"]
        request = "integration-" + uuid4().hex
        token = jwt.encode(
            dict(
                sub=user,
                role="user",
                type="agent",
                iss="hommey-backend",
                aud=["hommey-agent", "hommey-business"],
                session_id=session,
                request_id=request,
                iat=int(time.time()),
                exp=int(time.time()) + 300,
            ),
            (root / ".secrets/jwt-private.pem").read_text(),
            algorithm="RS256",
        )
        monkeypatch.setenv("HOMMEY_BUSINESS_API_URL", "http://localhost:8080")
        from settings import MEMORY_CONFIG

        monkeypatch.setitem(MEMORY_CONFIG["long_term"], "postgres_dsn", ai_dsn)
        yield SimpleNamespace(
            user=user,
            session=session,
            request=request,
            token=token,
            client=client,
            business_dsn=business_dsn,
        )
    finally:
        client.close()
        with psycopg.connect(business_dsn) as conn:
            for table in (
                "business_operation_receipts",
                "supervisor_trip_records",
                "supervisor_runs",
                "conversation_messages",
                "conversation_sessions",
                "chat_session_titles",
                "active_trip_contexts",
                "user_travel_preferences",
                "user_preferences",
                "memory_versions",
                "user_statistics",
                "user_personal_profiles",
            ):
                # Fixed test-owned table allowlist; user remains a bound parameter.
                conn.execute(f"DELETE FROM {table} WHERE user_id=%s", (user,))
            conn.execute("DELETE FROM users WHERE id=%s", (int(user),))


@pytest.mark.asyncio
async def test_real_python_supervisor_commits_only_through_spring(
    split_stack, monkeypatch
):
    from ai_service.business import execution_credential
    from datetime import date, timedelta
    import runtime
    from agent_runtime.contracts import Scope
    from agent_runtime.engine import Supervisor
    from tests.test_supervisor_runtime import outputs, reply
    from tests.test_supervisor_control import role_of

    stack = split_stack
    start = (date.today() + timedelta(days=7)).isoformat()
    text = f"我从北京到上海出差，{start}出发，2天，拜访客户。"
    marker = execution_credential.set(stack.token)
    try:
        scope = Scope(
            user_id=stack.user, session_id=stack.session, request_id=stack.request
        )

        async def model(messages, **kwargs):
            results = outputs(messages)
            if role_of(messages) is None:
                if not results:
                    return reply(
                        ("delegate", {"role": "trip_context", "task": "整理当前行程"})
                    )
                return reply(("finish", {"result_ids": [results[0]["result_id"]]}))
            trip = dict(
                origin="北京",
                destination="上海",
                start_date=start,
                duration_days=2,
                trip_purpose="拜访客户",
            )
            return reply(
                (
                    "report",
                    {
                        "summary": "北京到上海出差两天",
                        "data": {
                            "trip": trip,
                            "field_sources": {key: text for key in trip},
                        },
                    },
                )
            )

        config = dict(
            main_rounds=8,
            child_rounds=4,
            max_children=4,
            parallel_children=2,
            child_timeout_sec=15,
            tool_timeout_sec=10,
        )
        monkeypatch.setattr(runtime, "create_tool_model", lambda *args: model)
        instance = runtime.create_agent_runtime(stack.user)
        memory = instance.memory_manager.for_session(stack.session)
        memory.add_message("user", text, {"request_id": stack.request})
        supervisor = Supervisor(
            instance.model,
            instance.supervisor.services,
            instance.supervisor.store,
            config,
        )
        result = await supervisor.run(scope, text)
        assert result["agents"][0]["status"] == "success"
        memory.add_message(
            "assistant", result["response"], {"request_id": stack.request}
        )
        repeated = await supervisor.run(scope, text)
        assert repeated["response"] == result["response"]
        response = stack.client.get(
            f"/api/{stack.user}/trip/active", params={"session_id": stack.session}
        )
        response.raise_for_status()
        assert response.json()["active_trip"]["destination"] == "上海"
        with psycopg.connect(stack.business_dsn) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM business_operation_receipts WHERE user_id=%s",
                    (stack.user,),
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM conversation_messages WHERE user_id=%s",
                    (stack.user,),
                ).fetchone()[0]
                == 2
            )
    finally:
        execution_credential.reset(marker)


def test_spring_proxies_ai_assets_and_keeps_business_bindings(split_stack):
    from ai_service.business import RemoteMemoryManager, execution_credential

    stack = split_stack
    assert stack.client.get("/api/intents").status_code == 200
    assert stack.client.get("/api/knowledge/documents").status_code == 200
    assert stack.client.post("/api/knowledge/refresh").status_code == 403
    upload = stack.client.post(
        f"/api/{stack.user}/attachments",
        files={"file": ("integration.txt", "差旅材料测试".encode(), "text/plain")},
    )
    upload.raise_for_status()
    attachment = upload.json()
    assert attachment["status"] == "ready"
    try:
        marker = execution_credential.set(stack.token)
        try:
            memory = RemoteMemoryManager(stack.user).for_session(stack.session)
            memory.add_message(
                "user",
                "请看附件",
                {"request_id": stack.request, "attachment_ids": [attachment["id"]]},
            )
        finally:
            execution_credential.reset(marker)
        history = stack.client.get(f"/api/{stack.user}/sessions/{stack.session}")
        history.raise_for_status()
        assert history.json()["messages"][0]["attachments"][0]["id"] == attachment["id"]
        download = stack.client.get(
            f"/api/{stack.user}/attachments/{attachment['id']}/content"
        )
        download.raise_for_status()
        assert download.content == "差旅材料测试".encode()
        missing = stack.client.get(f"/api/{stack.user}/attachments/att_missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"]
    finally:
        stack.client.delete(
            f"/api/{stack.user}/attachments/{attachment['id']}"
        ).raise_for_status()
