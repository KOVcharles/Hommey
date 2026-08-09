"""Read-only RAG knowledge-library API contracts."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from webui_new.auth import User, get_current_user
from webui_new.core.errors import BusinessError, register_error_handlers
from webui_new.routes.knowledge_base import KnowledgeBaseLibrary, create_knowledge_base_router
from webui_new.knowledge_base_service import KnowledgeBaseManagementService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def knowledge_client(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "01_travel_standards.txt").write_text(
        "差旅标准\n\n一、交通标准\n\n- 高铁可乘坐二等座",
        encoding="utf-8",
    )
    (documents / "notes.md").write_text("# 补充说明\n\n保留票据。", encoding="utf-8")
    (documents / "ignored.json").write_text("{}", encoding="utf-8")

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_knowledge_base_router(documents))

    async def current_user():
        return User(
            id=7,
            email="user@example.com",
            password_hash="",
            created_at="2026-01-01T00:00:00+00:00",
        )

    app.dependency_overrides[get_current_user] = current_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_lists_supported_rag_sources_with_display_metadata(knowledge_client):
    response = await knowledge_client.get("/api/knowledge/documents")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["documents"]] == ["01_travel_standards.txt", "notes.md"]
    first = body["documents"][0]
    assert first["title"] == "差旅标准"
    assert first["category"] == "travel_policy"
    assert first["category_label"] == "差旅标准"
    assert first["read_minutes"] == 1


@pytest.mark.anyio
async def test_returns_full_document_content(knowledge_client):
    response = await knowledge_client.get("/api/knowledge/documents/01_travel_standards.txt")

    assert response.status_code == 200
    assert "高铁可乘坐二等座" in response.json()["content"]


@pytest.mark.anyio
async def test_every_authenticated_user_can_upload_during_test_phase(knowledge_client):
    response = await knowledge_client.post(
        "/api/knowledge/documents",
        files=[("files", ("new_policy.md", "# 新制度\n\n保留发票。".encode(), "text/markdown"))],
    )

    assert response.status_code == 200
    assert response.json()["refresh_required"] is True
    assert response.json()["uploaded"][0]["index_status"] == "pending"
    listing = (await knowledge_client.get("/api/knowledge/documents")).json()
    uploaded = next(item for item in listing["documents"] if item["id"] == "new_policy.md")
    assert uploaded["index_status"] == "pending"


@pytest.mark.anyio
async def test_upload_rejects_unsupported_knowledge_file(knowledge_client):
    response = await knowledge_client.post(
        "/api/knowledge/documents",
        files=[("files", ("policy.exe", b"nope", "application/octet-stream"))],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "KNOWLEDGE_FILE_TYPE_UNSUPPORTED"


def test_library_rejects_paths_outside_configured_root(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    (tmp_path / "secret.txt").write_text("not public", encoding="utf-8")
    library = KnowledgeBaseLibrary(documents)

    with pytest.raises(BusinessError) as exc_info:
        library.get_document("../secret.txt")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "KNOWLEDGE_DOCUMENT_NOT_FOUND"


def test_library_does_not_list_symlinks_that_escape_root(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("not public", encoding="utf-8")
    (documents / "linked.txt").symlink_to(secret)

    assert KnowledgeBaseLibrary(documents).list_documents() == []


def test_refresh_job_rebuilds_manifest_and_marks_document_indexed(tmp_path):
    documents = tmp_path / "documents"
    knowledge = tmp_path / "knowledge"
    documents.mkdir()
    policy = documents / "policy.txt"
    policy.write_text("差旅制度\n二等座", encoding="utf-8")

    def fake_ingestion(progress):
        progress("正在解析文档内容", 40)
        progress("正在生成向量并写入数据库", 80)
        return {
            "status": "success",
            "documents_loaded": 1,
            "pages_parsed": 1,
            "chunks_loaded": 2,
            "added_count": 2,
            "total_count": 2,
            "errors": [],
        }

    service = KnowledgeBaseManagementService(
        documents,
        knowledge,
        ingestion_runner=fake_ingestion,
    )
    assert service.document_index_status("policy.txt", policy) == "pending"
    started = service.start_refresh("7")
    assert started["status"] == "running"

    deadline = time.monotonic() + 2
    while service.status()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    finished = service.status()
    assert finished["status"] == "success"
    assert finished["report"]["chunks_loaded"] == 2
    assert service.document_index_status("policy.txt", policy) == "indexed"
