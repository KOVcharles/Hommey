"""Vector store interfaces and concrete adapters."""
from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .schemas import DocumentChunk, RetrievalResult


class VectorStore(ABC):
    @abstractmethod
    def add_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        raise NotImplementedError

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
        top_k: int = 3,
        vector_top_k: int = 10,
        bm25_top_k: int = 10,
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
            top_k=top_k,
            vector_top_k=vector_top_k,
            bm25_top_k=bm25_top_k,
        )

    def add_chunks(self, chunks: List[DocumentChunk]) -> Dict[str, Any]:
        return self.store.add_chunks(chunks)

    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return [_result_from_dict(item) for item in self.store.hybrid_search(query, top_k=top_k)]

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
        self.rows.extend(chunks)
        return {"status": "success", "added_count": len(chunks), "total_count": len(self.rows)}

    def search(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        tokens = _tokens(query)
        scored: List[tuple[float, int, DocumentChunk]] = []
        for index, chunk in enumerate(self.rows, start=1):
            content_tokens = _tokens(chunk.content)
            if not tokens:
                score = 0.0
            else:
                score = sum(content_tokens.count(token) for token in tokens)
            if score > 0 or query in chunk.content:
                scored.append((float(score), index, chunk))
        scored.sort(key=lambda item: (-item[0], item[1]))
        limit = top_k or 3
        return [
            RetrievalResult(
                id=index,
                content=chunk.content,
                metadata=chunk.to_metadata(),
                fusion_score=score if math.isfinite(score) else 0.0,
            )
            for score, index, chunk in scored[:limit]
        ]

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
    )


def _tokens(text: str) -> List[str]:
    lowered = (text or "").lower()
    return re.findall(r"[a-z0-9_]+", lowered) + [char for char in lowered if "\u4e00" <= char <= "\u9fff"]
