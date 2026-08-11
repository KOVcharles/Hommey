"""语音转写路由测试（Mode A）：Mock 转写器与配额，不调用外部 ASR。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multimodal.audio_processor import AudioTranscriptionError
from webui_new.auth.deps import get_current_user
from webui_new.auth.storage import User
from webui_new.core.errors import register_error_handlers
from webui_new.routes.asr import create_asr_router


class _FakeTranscriber:
    def __init__(self, text: str = "下周一去上海出差", error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls = []

    async def transcribe(self, audio_bytes: bytes, filename: str, language=None) -> str:
        self.calls.append((audio_bytes, filename, language))
        if self.error:
            raise self.error
        return self.text


class _QuotaStub:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls = []

    def consume(self, user_id: str) -> bool:
        self.calls.append(user_id)
        return self.allowed


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def asr_client(monkeypatch):
    transcriber = _FakeTranscriber()
    quota = _QuotaStub(allowed=True)
    monkeypatch.setattr("webui_new.routes.asr.create_transcriber", lambda: transcriber)
    monkeypatch.setattr("webui_new.routes.asr._get_asr_quota", lambda: quota)

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_asr_router())

    async def current_user():
        return User(
            id=7,
            email="user@example.com",
            password_hash="",
            created_at="2026-01-01T00:00:00+00:00",
        )

    app.dependency_overrides[get_current_user] = current_user
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client, transcriber, quota


@pytest.mark.anyio
async def test_asr_transcribe_returns_text_and_consumes_quota(asr_client):
    client, transcriber, quota = asr_client

    response = await client.post(
        "/api/7/asr/transcribe",
        files={"file": ("voice.wav", b"\x00" * 200, "audio/wav")},
        headers={"X-Request-ID": "request-1"},
    )

    assert response.status_code == 200
    assert response.json() == {"text": "下周一去上海出差"}
    assert quota.calls == ["7"]
    filename = transcriber.calls[0][1]
    assert filename == "voice.wav"


@pytest.mark.anyio
async def test_asr_disabled_returns_clear_error(asr_client, monkeypatch):
    client, _, _ = asr_client
    monkeypatch.setattr("webui_new.routes.asr.create_transcriber", lambda: None)

    response = await client.post(
        "/api/7/asr/transcribe",
        files={"file": ("voice.wav", b"\x00" * 200, "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ASR_DISABLED"


@pytest.mark.anyio
async def test_asr_daily_quota_exceeded(asr_client, monkeypatch):
    client, _, _ = asr_client
    monkeypatch.setattr("webui_new.routes.asr._get_asr_quota", lambda: _QuotaStub(allowed=False))

    response = await client.post(
        "/api/7/asr/transcribe",
        files={"file": ("voice.wav", b"\x00" * 200, "audio/wav")},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "ASR_QUOTA_EXCEEDED"


@pytest.mark.anyio
async def test_asr_upstream_failure_surfaces_error_code(asr_client, monkeypatch):
    client, _, _ = asr_client
    monkeypatch.setattr(
        "webui_new.routes.asr.create_transcriber",
        lambda: _FakeTranscriber(error=AudioTranscriptionError("ASR_TIMEOUT", "转写超时")),
    )

    response = await client.post(
        "/api/7/asr/transcribe",
        files={"file": ("voice.wav", b"\x00" * 200, "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ASR_TIMEOUT"


@pytest.mark.anyio
async def test_asr_cross_user_rejected(asr_client):
    client, transcriber, _ = asr_client

    response = await client.post(
        "/api/8/asr/transcribe",
        files={"file": ("voice.wav", b"\x00" * 200, "audio/wav")},
    )

    assert response.status_code == 403
    assert transcriber.calls == []
