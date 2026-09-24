"""Reimbursement uses ordinary conversation and canonical document paths."""
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from core.intent_guard import guard_user_input
from rag.config import RAGPipelineConfig
from rag.pipeline import RAGPipeline
from rag.vector_store import InMemoryVectorStore
from tests.test_postgres_vector_store import FakeEmbedder, _chunk


@pytest.mark.parametrize("query", [
    "重庆大学版面费报销需要什么材料？",
    "国家社会科学基金项目", "改为非科研经费",
    "预订机票需要用公务卡吗？", "购票后发票丢失怎么办？",
    "办公用品采购报销需要合同吗？",
])
def test_consultations_reach_the_model_without_a_trip(query):
    assert guard_user_input(query) is None


def scoped_chunks():
    return [replace(_chunk(i, text="报销材料：" + scope),
                    document_id=scope + "/policy.pdf", chunk_id=scope + "::1")
            for i, scope in enumerate(("demo/travel", "cqu/finance-old", "cqu/finance", "cqu/research"), 1)]


def test_scope_filters_before_top_k_and_can_expand_without_reingestion():
    store = InMemoryVectorStore(search_scopes=("cqu/finance",))
    store.add_chunks(scoped_chunks())
    assert {r.metadata["document_id"] for r in store.search("报销材料", 1)} == {"cqu/finance/policy.pdf"}
    store.search_scopes = ("missing",)
    assert store.search("报销材料", 10) == []
    store.search_scopes = ("cqu/finance", "cqu/research")
    assert {r.metadata["document_id"] for r in store.search("报销材料", 10)} == {
        "cqu/finance/policy.pdf", "cqu/research/policy.pdf"}


def test_subdirectory_ingest_has_same_identity_as_full_ingest(tmp_path):
    root = tmp_path / "documents"
    branch = root / "cqu" / "finance"
    branch.mkdir(parents=True)
    (branch / "policy.txt").write_text("报销需要发票。", encoding="utf-8")
    store = InMemoryVectorStore()
    pipeline = RAGPipeline(config=RAGPipelineConfig(documents_dir=str(root)), vector_store=store)
    assert pipeline.ingest(branch).status == "success"
    ids = [c.chunk_id for c in store.rows]
    assert pipeline.ingest(root).added_count == 0
    assert [c.chunk_id for c in store.rows] == ids
    assert store.rows[0].document_id == "cqu/finance/policy.txt"


def test_real_guide_keeps_funding_conditions_and_original_numbers(tmp_path):
    root = Path("data/documents")
    store = InMemoryVectorStore()
    pipeline = RAGPipeline(config=RAGPipelineConfig(
        documents_dir=str(root), knowledge_base_path=str(tmp_path)), vector_store=store)
    report = pipeline.ingest(root / "cqu/finance")
    assert report.status == "success" and not report.errors
    hotels = [c for c in store.rows if c.page_number == 11 and "550" in c.content]
    assert hotels and all("使用科研经费报销" in c.heading_path for c in hotels)
    assert all("使用科研经费以外其他经费报销" not in c.heading_path for c in hotels)
    nonresearch = [c for c in store.rows if c.page_number == 12 and "80" in c.content]
    assert nonresearch and all("使用科研经费以外其他经费报销" in c.heading_path for c in nonresearch)
    assert any("26、27" in c.content.replace(" ", "") for c in store.rows if c.page_number == 10)
    assert any("国家社会科学基金" in c.content and "不得支出" in c.content for c in store.rows)


def test_scoped_upload_uses_the_same_relative_document_identity(tmp_path):
    from webui_new.knowledge_base_service import KnowledgeBaseManagementService
    from webui_new.routes.knowledge_base import KnowledgeBaseLibrary
    root = tmp_path / "documents"
    service = KnowledgeBaseManagementService(root, tmp_path / "index", config=RAGPipelineConfig(
        documents_dir=str(root), search_scopes=("cqu/finance",)))
    service.upload("policy.md", "# 报销\n需要发票".encode())
    documents = KnowledgeBaseLibrary(root).list_documents()
    assert documents[0]["id"] == "cqu/finance/policy.md"
    from webui_new.core.errors import BusinessError
    with pytest.raises(BusinessError, match="同名文档"):
        service.upload("policy.md", "# 不得覆盖".encode())


def test_merged_clauses_cite_common_ancestors_only():
    from rag.chunker import BlockChunker
    from rag.schemas import Block
    blocks = [Block(block_id="p1-b1", block_type="paragraph", text="甲", heading_path=["报销答疑", "图书"]),
              Block(block_id="p1-b2", block_type="paragraph", text="乙", heading_path=["报销答疑", "版面费"])]
    assert BlockChunker._effective_heading_path(blocks, []) == ["报销答疑"]


def test_historical_policy_outside_current_scope_is_expired(monkeypatch):
    from types import SimpleNamespace
    from agent_runtime.engine import Turn
    from settings import RAG_CONFIG
    monkeypatch.setitem(RAG_CONFIG, "search_scopes", "cqu/finance")
    result = SimpleNamespace(sources=[{"kind": "policy", "data": {
        "metadata": {"document_id": "demo/travel/policy.pdf"}}}])
    assert not Turn.source_fresh(result)
    result.sources[0]["data"]["metadata"]["document_id"] = "cqu/finance/policy.pdf"
    assert Turn.source_fresh(result)


@pytest.mark.parametrize("scope", ["/cqu", "../cqu", "cqu/../finance", "cqu//finance", "C:/docs", "cqu\\finance"])
def test_scope_configuration_rejects_noncanonical_paths(scope):
    with pytest.raises(ValueError, match="relative document directories"):
        RAGPipelineConfig.from_settings({"search_scopes": scope})


@pytest.mark.skipif(not os.getenv("HOMMEY_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
def test_postgres_dense_bm25_and_hybrid_share_scope():
    from context.postgres_pool import get_postgres_pool
    from rag.postgres_vector_store import PostgresVectorStore
    from rag.hybrid import bm25_search
    from webui_new.auth.migrations import apply_all_migrations
    dsn = os.environ["HOMMEY_TEST_POSTGRES_DSN"]
    apply_all_migrations(dsn)
    collection = "test-scope-" + uuid4().hex
    store = PostgresVectorStore(postgres_dsn=dsn, collection_name=collection,
        embedding_model="fake", embedding_dimension=3, embedder=FakeEmbedder(),
        search_scopes=("cqu/finance",))
    try:
        store.replace_chunks(scoped_chunks())
        for results in (store.search_dense("报销材料", 1), store.search("报销材料", 1)):
            assert [r.metadata["document_id"] for r in results] == ["cqu/finance/policy.pdf"]
        assert [r["metadata"]["document_id"] for r in bm25_search(store, "报销材料")] == ["cqu/finance/policy.pdf"]
        store.search_scopes = ("missing",)
        assert store.search("报销材料", 10) == []
        store.search_scopes = ("cqu/finance", "cqu/research")
        assert {r.metadata["document_id"] for r in store.search("报销材料", 10)} == {
            "cqu/finance/policy.pdf", "cqu/research/policy.pdf"}
    finally:
        with get_postgres_pool(dsn).connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM rag_collections WHERE collection_name=%s", (collection,))
