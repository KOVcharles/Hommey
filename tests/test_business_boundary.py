"""Contracts for the Spring-owned business boundary, without live model calls."""

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from ai_service.business import BusinessClient, RemoteRunStore, execution_credential


@pytest.fixture
def keys(tmp_path, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    path = tmp_path / "public.pem"
    path.write_bytes(public)
    monkeypatch.setenv("HOMMEY_JWT_PUBLIC_KEY_PATH", str(path))
    monkeypatch.setenv("HOMMEY_BUSINESS_API_URL", "http://backend")
    return private


def token(keys, **updates):
    claims = dict(
        sub="1",
        iss="hommey-backend",
        aud=["hommey-agent", "hommey-business"],
        type="agent",
        iat=int(time.time()),
        exp=int(time.time()) + 120,
        session_id="session-1",
        request_id="request-1",
        role="user",
    )
    claims.update(updates)
    return jwt.encode(claims, keys, algorithm="RS256")


@pytest.fixture
def client(keys):
    from ai_service.server import create_app

    router = APIRouter()

    @router.post("/api/{user_id}/chat/stream")
    async def stream(user_id: str, request: Request):
        async def events():
            assert execution_credential.get()
            yield json.dumps(
                {"type": "chunk", "text": request.state.execution_claims["sub"]}
            ) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    return TestClient(create_app(router.routes))


def test_internal_service_rejects_missing_and_public_tokens(client, keys):
    body = {"session_id": "session-1", "client_request_id": "request-1"}
    assert (
        client.post("/internal/capabilities/api/1/chat/stream", json=body).status_code
        == 401
    )
    for changes in [
        dict(type="access"),
        dict(aud="hommey-api"),
        dict(iss="other"),
        dict(exp=int(time.time()) - 60),
    ]:
        assert (
            client.post(
                "/internal/capabilities/api/1/chat/stream",
                json=body,
                headers={"Authorization": "Bearer " + token(keys, **changes)},
            ).status_code
            == 401
        )


def test_token_is_bound_to_user_session_and_request(client, keys):
    headers = {"Authorization": "Bearer " + token(keys)}
    for user, session, request in [
        ("2", "session-1", "request-1"),
        ("1", "other", "request-1"),
        ("1", "session-1", "other"),
    ]:
        assert (
            client.post(
                f"/internal/capabilities/api/{user}/chat/stream",
                json={"session_id": session, "client_request_id": request},
                headers=headers,
            ).status_code
            == 401
        )
    response = client.post(
        "/internal/capabilities/api/1/chat/stream",
        json={"session_id": "session-1", "client_request_id": "request-1"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json() == {"type": "chunk", "text": "1"}
    assert execution_credential.get() is None


def test_concurrent_streams_keep_credentials_separate(client, keys):
    from concurrent.futures import ThreadPoolExecutor

    def read(user):
        response = client.post(
            f"/internal/capabilities/api/{user}/chat/stream",
            json={"session_id": "session-1", "client_request_id": "request-1"},
            headers={"Authorization": "Bearer " + token(keys, sub=user)},
        )
        assert response.status_code == 200
        return response.json()["text"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(read, ["1", "2"])) == ["1", "2"]
    assert execution_credential.get() is None


def test_mutations_go_to_business_api_without_sql():
    calls = []

    class Client:
        def call(self, method, suffix, body):
            calls.append((method, suffix, body))
            return {
                "applied": True,
                "trip": {},
                "version": 0,
                "preferences_updated": True,
            }

    class ForbiddenPool:
        def connection(self):
            raise AssertionError("Business mutations must not write from Python")

    scope = SimpleNamespace(user_id="1", session_id="s", request_id="r")
    store = RemoteRunStore(ForbiddenPool(), Client())
    result = store._apply(scope, "owner", "op", 42, {}, {"home_location": "重庆"})
    assert result["applied"]
    assert calls[0][0:2] == ("POST", "/mutations")
    assert calls[0][2]["expected_version"] == 42


def test_client_requires_scoped_credential(monkeypatch):
    monkeypatch.setenv("HOMMEY_BUSINESS_API_URL", "http://backend")
    with pytest.raises(RuntimeError, match="scoped"):
        BusinessClient("1").call("GET", "/preferences")
