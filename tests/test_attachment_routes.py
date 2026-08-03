"""API contract tests for authenticated P0 attachment upload, status, and deletion."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from multimodal.schemas import AttachmentDetailResponse, AttachmentUploadResponse
from settings import ATTACHMENT_CONFIG
from webui_new.auth.deps import get_current_user
from webui_new.auth.storage import User
from webui_new.core.errors import register_error_handlers
from webui_new.routes.attachments import create_attachments_router


class _AttachmentService:
    def __init__(self):
        self.calls = []

    def upload(self, **kwargs):
        self.calls.append(("upload", kwargs))
        return AttachmentUploadResponse(
            id="att_1",
            filename=kwargs["filename"],
            kind="document",
            status="ready",
            mime_type="text/plain",
            size_bytes=len(kwargs["content"]),
        )

    def get(self, attachment_id, user_id):
        self.calls.append(("get", attachment_id, user_id))
        return AttachmentDetailResponse(
            id=attachment_id,
            filename="policy.txt",
            kind="document",
            status="ready",
            mime_type="text/plain",
            size_bytes=5,
        )

    def delete(self, attachment_id, user_id):
        self.calls.append(("delete", attachment_id, user_id))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def attachment_client():
    service = _AttachmentService()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_attachments_router(service))

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
        yield client, service


@pytest.mark.anyio
async def test_upload_uses_authenticated_user_and_request_id(attachment_client):
    client, service = attachment_client
    response = await client.post(
        "/api/7/attachments",
        files={"file": ("policy.txt", b"hello", "text/plain")},
        headers={"X-Request-ID": "request-1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    call = service.calls[0]
    assert call[0] == "upload"
    assert call[1]["user_id"] == "7"
    assert call[1]["request_id"] == "request-1"
    assert call[1]["content"] == b"hello"


@pytest.mark.anyio
async def test_status_and_delete_keep_attachment_ownership_in_service(attachment_client):
    client, service = attachment_client

    status = await client.get("/api/7/attachments/att_1")
    deleted = await client.delete("/api/7/attachments/att_1")

    assert status.status_code == 200
    assert deleted.json() == {"id": "att_1", "deleted": True}
    assert ("get", "att_1", "7") in service.calls
    assert ("delete", "att_1", "7") in service.calls


@pytest.mark.anyio
async def test_cross_user_attachment_route_is_rejected(attachment_client):
    client, service = attachment_client
    response = await client.get("/api/8/attachments/att_1")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert service.calls == []


@pytest.mark.anyio
async def test_upload_stops_reading_above_configured_limit(attachment_client, monkeypatch):
    client, service = attachment_client
    monkeypatch.setitem(ATTACHMENT_CONFIG, "max_size_bytes", 4)

    response = await client.post(
        "/api/7/attachments",
        files={"file": ("policy.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ATTACHMENT_TOO_LARGE"
    assert service.calls == []
