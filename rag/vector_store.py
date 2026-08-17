"""Vector store interfaces and concrete adapters."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .milvus_store import _fresh_versions
from .ranking import filter_relevant_results, rerank_results
from .schemas import DocumentChunk, RetrievalResult


class VectorStore(ABC):
    @abstractmethod
    def add_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        raise NotImplementedError

    def search_dense(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        """Dense-only extension point used by HyDE.

        Backends without a separate dense implementation may retain the
        compatibility fallback; the production Milvus backend overrides it.
        """
        return self.search(query, top_k=top_k)

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        raise NotImplementedError

    def rebuild(self) -> None:
        raise NotImplementedError

    def replace_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        """Replace all rows while preserving the previous store on failure.

        Concrete production stores should override this with a transactional or
        blue/green implementation.  The default deliberately refuses to fake
        atomicity with ``rebuild()`` followed by ``add_chunks()``.
        """
        raise NotImplementedError("atomic replacement is not supported by this vector store")

    def close(self) -> None:
        return None


class MilvusVectorStore(VectorStore):
    def __init__(
        self,
        knowledge_base_path: str,
        collection_name: str,
        embedding_model: str,
        embedding_backend: str = "siliconflow",
        embedding_api_key: str | None = None,
        embedding_base_url: str = "https://api.siliconflow.cn/v1",
        embedding_dimension: int = 1024,
        embedding_batch_size: int = 32,
        embedding_timeout_sec: float = 30.0,
        embedding_max_retries: int = 2,
        embedding_retry_base_delay_sec: float = 1.0,
        embedding_retry_max_delay_sec: float = 30.0,
        embedding_cache_size: int = 1024,
        top_k: int = 3,
        vector_top_k: int = 10,
        bm25_top_k: int = 10,
        sparse_backend: str = "python",
    ):
        from .milvus_store import MilvusKnowledgeStore

        self.store = MilvusKnowledgeStore(
            knowledge_base_path=knowledge_base_path,
            collection_name=collection_name,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            embedding_api_key=embedding_api_key,
            embedding_base_url=embedding_base_url,
            embedding_dimension=embedding_dimension,
            embedding_batch_size=embedding_batch_size,
            embedding_timeout_sec=embedding_timeout_sec,
            embedding_max_retries=embedding_max_retries,
            embedding_retry_base_delay_sec=embedding_retry_base_delay_sec,
            embedding_retry_max_delay_sec=embedding_retry_max_delay_sec,
            embedding_cache_size=embedding_cache_size,
            top_k=top_k,
            vector_top_k=vector_top_k,
            bm25_top_k=bm25_top_k,
            sparse_backend=sparse_backend,
        )

    def add_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        return self.store.add_chunks(chunks)

    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return [_result_from_dict(item) for item in self.store.hybrid_search(query, top_k=top_k)]

    def search_dense(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return [
            _result_from_dict(item)
            for item in self.store.vector_search(query, top_k or self.store.vector_top_k)
        ]

    def stats(self) -> Dict[str, Any]:
        return self.store.stats()

    def rebuild(self) -> None:
        self.store.rebuild_collection()

    def replace_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        return self.store.replace_chunks_atomically(chunks)

    def close(self) -> None:
        self.store.close()


class InMemoryVectorStore(VectorStore):
    """Small deterministic store for tests and local dry-runs."""

    def __init__(self):
        self.rows: List[DocumentChunk] = []

    def add_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        # Mirror the Milvus backend's idempotent upsert (audit §4.3/§7 P6): a
        # chunk already present under the same (chunk_hash, document_id,
        # document_version) key is skipped, and a changed document's older rows
        # are retired so incremental refresh adds zero duplicates on both
        # backends (§6.1.6 two-backend consistency).
        existing = {chunk_key(chunk) for chunk in self.rows}
        fresh: List[DocumentChunk] = []
        for chunk in chunks:
            key = chunk_key(chunk)
            if key in existing:
                continue
            existing.add(key)
            fresh.append(chunk)
        if fresh:
            fresh_versions = _fresh_versions(fresh)
            keep = []
            for row in self.rows:
                metadata = row.to_metadata()
                document_id = str(metadata.get("document_id") or "")
                if document_id in fresh_versions and str(metadata.get("document_version") or "") not in fresh_versions[document_id]:
                    continue  # superseded by the just-written version
                keep.append(row)
            self.rows = keep + fresh
        return {"status": "success", "added_count": len(fresh), "total_count": len(self.rows)}

    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        tokens = _tokens(query)
        candidates: List[Dict[str, Any]] = []
        for index, chunk in enumerate(self.rows, start=1):
            content_tokens = _tokens(chunk.content)
            if not tokens:
                score = 0.0
            else:
                score = float(sum(content_tokens.count(token) for token in tokens))
            if score > 0 or query in chunk.content:
                candidates.append(
                    {
                        "id": index,
                        "content": chunk.content,
                        "metadata": chunk.to_metadata(),
                        "distance": None,
                        "vector_rank": None,
                        "bm25_rank": None,
                        "fusion_score": score,
                    }
                )
        if not candidates:
            return []
        # Mirror the production backend's scoring pipeline (audit §6.1.6) so both
        # stores sort by the same rerank_score and apply the same domain filter.
        reranked = rerank_results(candidates, query)
        filtered = filter_relevant_results(reranked, query)
        final = (filtered or reranked)[: (top_k or 3)]
        return [_result_from_dict(item) for item in final]

    def stats(self) -> Dict[str, Any]:
        return {"status": "success", "total_documents": len(self.rows)}

    def rebuild(self) -> None:
        self.rows.clear()

    def replace_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        replacement = list(chunks)
        self.rows = replacement
        return {
            "status": "success",
            "added_count": len(replacement),
            "total_count": len(replacement),
        }


def create_vector_store(config: Any) -> VectorStore:
    """Build the configured backend through one production construction seam."""
    backend = str(getattr(config, "vector_backend", "milvus_lite") or "milvus_lite").lower()
    if backend == "memory":
        return InMemoryVectorStore()

    common = {
        "collection_name": config.collection_name,
        "embedding_model": config.embedding_model,
        "embedding_backend": config.embedding_backend,
        "embedding_api_key": config.embedding_api_key,
        "embedding_base_url": config.embedding_base_url,
        "embedding_dimension": config.embedding_dimension,
        "embedding_batch_size": config.embedding_batch_size,
        "embedding_timeout_sec": config.embedding_timeout_sec,
        "embedding_max_retries": config.embedding_max_retries,
        "embedding_retry_base_delay_sec": config.embedding_retry_base_delay_sec,
        "embedding_retry_max_delay_sec": config.embedding_retry_max_delay_sec,
        "embedding_cache_size": config.embedding_cache_size,
        "top_k": config.top_k,
        "vector_top_k": config.vector_top_k,
        "bm25_top_k": config.bm25_top_k,
        "sparse_backend": config.bm25_backend,
    }
    if backend == "postgres":
        from .postgres_vector_store import PostgresVectorStore

        return PostgresVectorStore(postgres_dsn=config.postgres_dsn, **common)
    if backend in {"milvus", "milvus_lite"}:
        return MilvusVectorStore(
            knowledge_base_path=config.knowledge_base_path,
            **common,
        )
    raise ValueError(
        f"Unsupported RAG vector backend: {backend}. Use postgres, milvus_lite, or memory."
    )


def chunk_key(chunk: DocumentChunk) -> tuple[str, str, str]:
    """Idempotency key shared with the Milvus backend (audit P6)."""
    metadata = chunk.to_metadata()
    return (
        str(metadata.get("chunk_hash") or ""),
        str(metadata.get("document_id") or ""),
        str(metadata.get("document_version") or ""),
    )


def _result_from_dict(item: Dict[str, Any]) -> RetrievalResult:
    return RetrievalResult(
        id=item.get("id"),
        content=item.get("content", ""),
        metadata=item.get("metadata", {}),
        distance=item.get("distance"),
        vector_rank=item.get("vector_rank"),
        bm25_rank=item.get("bm25_rank"),
        bm25_score=item.get("bm25_score"),
        fusion_score=float(item.get("fusion_score", 0.0) or 0.0),
        rerank_score=item.get("rerank_score"),
        retrieval_trace_id=item.get("retrieval_trace_id"),
    )


def _tokens(text: str) -> List[str]:
    lowered = (text or "").lower()
    return re.findall(r"[a-z0-9_]+", lowered) + [char for char in lowered if "\u4e00" <= char <= "\u9fff"]
