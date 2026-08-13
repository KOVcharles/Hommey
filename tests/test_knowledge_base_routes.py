"""Authenticated RAG knowledge-library and admin management contracts."""
from __future__ import annotations

import time
from pathlib import Path

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
            role="admin",
        )

    app.dependency_overrides[get_current_user] = current_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_lists_supported_rag_sources_with_display_metadata(knowledge_client):
    response = await knowledge_client.get("/api/knowledge/documents")

    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
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
async def test_admin_can_upload_knowledge_document(knowledge_client):
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
async def test_regular_user_can_read_but_cannot_manage_knowledge_base(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "policy.txt").write_text("差旅餐补标准", encoding="utf-8")
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_knowledge_base_router(documents))

    async def current_user():
        return User(
            id=8,
            email="employee@example.com",
            password_hash="",
            created_at="2026-01-01T00:00:00+00:00",
            role="user",
        )

    app.dependency_overrides[get_current_user] = current_user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get("/api/knowledge/documents")).status_code == 200
        upload = await client.post(
            "/api/knowledge/documents",
            files=[("files", ("new.md", b"# policy", "text/markdown"))],
        )
        refresh = await client.post("/api/knowledge/refresh")
        status = await client.get("/api/knowledge/refresh/status")

    assert upload.status_code == 403
    assert refresh.status_code == 403
    assert status.status_code == 403
    assert not (documents / "new.md").exists()


@pytest.mark.anyio
async def test_upload_never_overwrites_existing_policy(knowledge_client):
    response = await knowledge_client.post(
        "/api/knowledge/documents",
        files=[("files", ("notes.md", b"# malicious replacement", "text/markdown"))],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "KNOWLEDGE_DOCUMENT_EXISTS"
    existing = await knowledge_client.get("/api/knowledge/documents/notes.md")
    assert "保留票据" in existing.json()["content"]


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


def test_manifest_v2_accepts_flattened_ingestion_report(tmp_path):
    documents = tmp_path / "documents"
    knowledge = tmp_path / "knowledge"
    documents.mkdir()
    policy = documents / "policy.txt"
    policy.write_text("差旅制度", encoding="utf-8")
    service = KnowledgeBaseManagementService(documents, knowledge)

    service._write_manifest(
        {
            "status": "success",
            "schema_version": "rag.v2.metadata.1",
            "index": {
                "version": "index-v2",
                "built_at": "2026-08-12T00:00:00+00:00",
                "collection_name": "business_travel_knowledge",
            },
            "documents": {
                "policy.txt": {
                    "document_version": "abc123",
                    "parser_name": "txt_block",
                    "parser_version": "txt-block-v1",
                    "pages": {"native_text": 1},
                    "chunk_count": 1,
                }
            },
            "errors": [],
        }
    )

    manifest = service._read_manifest()
    assert manifest["schema_version"] == "rag.v2.metadata.1"
    assert manifest["index"]["version"] == "index-v2"
    assert manifest["documents"]["policy.txt"]["parser"]["version"] == "txt-block-v1"
    assert manifest["documents"]["policy.txt"]["pages"] == {"native_text": 1}
    assert manifest["documents"]["policy.txt"]["chunk_count"] == 1


def test_failed_refresh_preserves_previous_manifest(tmp_path):
    documents = tmp_path / "documents"
    knowledge = tmp_path / "knowledge"
    documents.mkdir()
    policy = documents / "policy.txt"
    policy.write_text("差旅制度\n二等座", encoding="utf-8")

    successful = KnowledgeBaseManagementService(
        documents,
        knowledge,
        ingestion_runner=lambda _progress: {
            "status": "success",
            "documents_loaded": 1,
            "chunks_loaded": 1,
            "errors": [],
        },
    )
    successful.start_refresh("7")
    deadline = time.monotonic() + 2
    while successful.status()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    manifest_before = successful.manifest_path.read_bytes()

    failed = KnowledgeBaseManagementService(
        documents,
        knowledge,
        ingestion_runner=lambda _progress: {
            "status": "error",
            "documents_loaded": 1,
            "chunks_loaded": 0,
            "errors": [{"source_path": str(policy), "error": "parse failed"}],
        },
    )
    failed.start_refresh("7")
    deadline = time.monotonic() + 2
    while failed.status()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert failed.status()["status"] == "error"
    assert failed.manifest_path.read_bytes() == manifest_before
    assert failed.document_index_status("policy.txt", policy) == "indexed"
