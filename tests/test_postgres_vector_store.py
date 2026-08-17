import os
import uuid
from pathlib import Path

import pytest

from rag.config import RAGPipelineConfig
from rag.postgres_vector_store import PostgresVectorStore
from rag.schemas import DocumentChunk
from rag.vector_store import create_vector_store


class FakeEmbedder:
    def __init__(self, dimension=3):
        self._dimension = dimension

    def dimension(self):
        return self._dimension

    def embed_texts(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        seed = float((sum(ord(char) for char in text) % 7) + 1)
        return [seed, 1.0, 0.5][: self._dimension]


class IncompleteEmbedder(FakeEmbedder):
    def embed_texts(self, texts):
        return []


def _chunk(index, *, fingerprint="fingerprint-v1", text=None):
    content = text or f"差旅政策片段 {index}"
    return DocumentChunk(
        content=content,
        source_path="policy.md",
        filename="policy.md",
        file_type="md",
        page_number=None,
        chunk_index=index,
        content_type="paragraph",
        hash=f"hash-{index}",
        chunk_id=f"policy::c{index}",
        chunk_hash=f"hash-{index}",
        chunk_ordinal=index,
        document_id="policy.md",
        document_version="doc-v1",
        index_version=fingerprint,
    )


def test_postgres_store_validates_and_stamps_versioned_rows():
    store = PostgresVectorStore(
        postgres_dsn="",
        collection_name="knowledge",
        embedding_model="fake",
        embedding_dimension=3,
        pool=object(),
        embedder=FakeEmbedder(),
    )

    rows = store._prepare_rows([_chunk(1)], version="build-1", fingerprint="fingerprint-v1")

    assert rows[0]["chunk_id"] == "policy::c1"
    assert rows[0]["embedding"].startswith("[")
    assert '"index_version": "build-1"' in rows[0]["metadata"]
    assert '"index_fingerprint": "fingerprint-v1"' in rows[0]["metadata"]


def test_factory_selects_postgres_backend(monkeypatch):
    import rag.postgres_vector_store as module

    captured = {}

    class FakeStore:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(module, "PostgresVectorStore", FakeStore)
    config = RAGPipelineConfig(
        vector_backend="postgres",
        postgres_dsn="postgresql://example/test",
        collection_name="knowledge",
    )

    store = create_vector_store(config)

    assert isinstance(store, FakeStore)
    assert captured["postgres_dsn"] == "postgresql://example/test"
    assert captured["collection_name"] == "knowledge"


def test_pgvector_migration_defines_versioned_atomic_schema():
    sql = Path("webui_new/auth/migrations/0020_rag_pgvector.sql").read_text(encoding="utf-8")

    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "rag_collections" in sql
    assert "rag_index_versions" in sql
    assert "rag_documents" in sql
    assert "rag_chunks" in sql
    assert "uq_rag_index_versions_one_active" in sql
    assert "embedding VECTOR NOT NULL" in sql


@pytest.mark.skipif(
    not os.getenv("HOMMEY_TEST_POSTGRES_DSN"),
    reason="HOMMEY_TEST_POSTGRES_DSN is required for pgvector integration",
)
def test_pgvector_replace_publishes_one_active_version_and_retires_old_build():
    from context.postgres_pool import get_postgres_pool
    from webui_new.auth.migrations import apply_all_migrations

    dsn = os.environ["HOMMEY_TEST_POSTGRES_DSN"]
    apply_all_migrations(dsn)
    collection = f"test-rag-{uuid.uuid4().hex}"
    pool = get_postgres_pool(dsn)
    store = PostgresVectorStore(
        postgres_dsn=dsn,
        collection_name=collection,
        embedding_model="fake",
        embedding_dimension=3,
        pool=pool,
        embedder=FakeEmbedder(),
    )
    try:
        first = store.replace_chunks([_chunk(1), _chunk(2)])
        second = store.replace_chunks([_chunk(1, text="新版住宿政策")])

        assert first["index_version"] != second["index_version"]
        assert store.stats()["index_version"] == second["index_version"]
        assert store.count() == 1
        assert store.search_dense("新版住宿政策", top_k=1)[0].content == "新版住宿政策"
        store.embedding_model = IncompleteEmbedder()
        with pytest.raises(RuntimeError, match="incomplete vector batch"):
            store.replace_chunks([_chunk(2, text="不完整构建")])
        assert store.stats()["index_version"] == second["index_version"]
        assert store.count() == 1
        with pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, COUNT(*) AS count FROM rag_index_versions WHERE collection_name=%s GROUP BY status",
                (collection,),
            )
            counts = {row["status"]: row["count"] for row in cursor.fetchall()}
        assert counts == {"active": 1, "retired": 1}
    finally:
        with pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM rag_collections WHERE collection_name=%s", (collection,))
