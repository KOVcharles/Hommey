"""RAG retrieval facades used by pipelines, agents, and compatibility tests."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import RAGPipelineConfig
from .schemas import DocumentChunk, RetrievalResult
from .vector_store import MilvusVectorStore, VectorStore


class Retriever(ABC):
    @abstractmethod
    def retrieve(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        raise NotImplementedError


class VectorStoreRetriever(Retriever):
    def __init__(self, vector_store: VectorStore):
        self.vector_store = vector_store

    def retrieve(self, query: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        if not query:
            return []
        return self.vector_store.search(expand_query(query), top_k=top_k)


class KnowledgeRetriever:
    """Backward-compatible retriever facade for existing agents."""

    def __init__(
        self,
        knowledge_base_path: Optional[str] = None,
        collection_name: Optional[str] = None,
        embedding_model: Optional[str] = None,
        embedding_backend: Optional[str] = None,
        top_k: int = 3,
        vector_store: Optional[VectorStore] = None,
    ):
        self.initialized = False
        self.top_k = max(1, int(top_k))
        self.store: Optional[VectorStore] = None
        self.retriever: Optional[VectorStoreRetriever] = None
        self.error: Optional[str] = None

        if vector_store is not None:
            self.store = vector_store
            self.retriever = VectorStoreRetriever(vector_store)
            self.initialized = True
            return

        from .milvus_store import DEPENDENCIES_AVAILABLE

        if not DEPENDENCIES_AVAILABLE:
            self.error = "RAG dependencies not installed"
            return

        try:
            config = RAGPipelineConfig.from_settings(
                {
                    "knowledge_base_path": knowledge_base_path,
                    "collection_name": collection_name,
                    "embedding_model": embedding_model,
                    "embedding_backend": embedding_backend,
                    "top_k": top_k,
                }
            )
            self.store = MilvusVectorStore(
                knowledge_base_path=config.knowledge_base_path,
                collection_name=config.collection_name,
                embedding_model=config.embedding_model,
                embedding_backend=config.embedding_backend,
                embedding_api_key=config.embedding_api_key,
                embedding_base_url=config.embedding_base_url,
                embedding_dimension=config.embedding_dimension,
                embedding_batch_size=config.embedding_batch_size,
                embedding_timeout_sec=config.embedding_timeout_sec,
                top_k=config.top_k,
                vector_top_k=config.vector_top_k,
                bm25_top_k=config.bm25_top_k,
            )
            self.retriever = VectorStoreRetriever(self.store)
            self.initialized = True
        except Exception as exc:
            self.error = str(exc)

    def add_documents(self, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.store:
            return {"status": "error", "message": self.error or "RAG retriever not initialized"}
        return self.store.add_chunks([_coerce_chunk(document, index) for index, document in enumerate(documents, start=1)])

    def search(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.retriever or not query:
            return []
        return [result.to_dict() for result in self.retriever.retrieve(query, top_k=top_k)]

    def stats(self) -> Dict[str, Any]:
        if not self.store:
            return {"status": "error", "message": self.error or "RAG retriever not initialized"}
        return self.store.stats()

    def rebuild(self) -> None:
        if not self.store:
            raise RuntimeError(self.error or "RAG retriever not initialized")
        self.store.rebuild()

    def close(self) -> None:
        if self.store:
            self.store.close()


def expand_query(query: str) -> str:
    """Add a few domain synonyms without introducing a query-rewrite LLM call."""
    text = query.strip()
    international_hints = (
        "国际",
        "境外",
        "国外",
        "港澳",
        "新加坡",
        "日本",
        "韩国",
        "美国",
        "加拿大",
        "英国",
        "法国",
        "德国",
        "澳大利亚",
        "阿联酋",
    )
    if any(word in text for word in international_hints):
        text = f"{text} 国际出差 境外"
    if any(word in text for word in ("餐补", "餐费", "用餐", "饭补", "吃饭")):
        text = f"{text} 餐费 用餐 早餐 午餐 晚餐 报销 个人零食 酒水"
    return text


def _coerce_chunk(document: Dict[str, Any], index: int) -> DocumentChunk:
    """Compatibility layer (audit §6.1.7 二): map legacy metadata aliases onto
    the V2 canonical fields so old collections and old manifests stay readable."""
    if isinstance(document, DocumentChunk):
        return document

    content = document.get("content", "")
    metadata = dict(document.get("metadata", {}))
    source_path = (
        metadata.get("source_path") or metadata.get("file_path") or metadata.get("source") or ""
    )
    filename = (
        metadata.get("filename")
        or metadata.get("file_name")
        or metadata.get("parent_doc")
        or metadata.get("name")
        or Path(source_path).name
    )
    file_type = metadata.get("file_type") or Path(filename).suffix.lstrip(".").lower() or "txt"
    page_number = metadata.get("page_number") or metadata.get("page")
    chunk_index = int(metadata.get("chunk_index") or index)

    heading_path = metadata.get("heading_path") or []
    if isinstance(heading_path, str):
        heading_path = [part for part in heading_path.split("/") if part]
    if not heading_path and metadata.get("section"):
        heading_path = [str(metadata["section"])]
    if not heading_path and metadata.get("title"):
        heading_path = [str(metadata["title"])]

    document_id = metadata.get("document_id") or Path(source_path).name
    legacy_chunk_id = str(metadata.get("chunk_id") or "")
    if not legacy_chunk_id:
        legacy_chunk_id = f"{document_id}::legacy::{page_number or 'p1'}::{chunk_index:02d}"

    return DocumentChunk(
        content=content,
        source_path=source_path,
        filename=filename,
        file_type=file_type,
        page_number=page_number,
        chunk_index=chunk_index,
        content_type=metadata.get("content_type", "text"),
        hash=metadata.get("hash") or metadata.get("chunk_hash") or f"legacy-{chunk_index}",
        title=metadata.get("title") or "",
        category=metadata.get("category") or "business_travel",
        metadata=metadata,
        chunk_id=legacy_chunk_id,
        chunk_hash=metadata.get("chunk_hash") or metadata.get("hash") or "",
        chunk_ordinal=int(metadata.get("chunk_ordinal") or chunk_index),
        page_start=metadata.get("page_start") or page_number,
        page_end=metadata.get("page_end") or page_number,
        block_ids=list(metadata.get("block_ids", []) or []),
        heading_path=list(heading_path),
        retrieval_text=metadata.get("retrieval_text") or content,
        display_text=metadata.get("display_text") or content,
        text_source=metadata.get("text_source") or "unknown",
        index_version=metadata.get("index_version") or "",
        document_id=document_id,
        document_version=metadata.get("document_version") or "",
        parser_name=metadata.get("parser_name") or "",
        parser_version=metadata.get("parser_version") or "",
        table=metadata.get("table"),
    )
