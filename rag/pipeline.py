"""Composable RAG ingestion and query pipeline."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .chunker import TextChunker
from .config import RAGPipelineConfig
from .loader import DocumentLoader, FileSystemDocumentLoader
from .normalizer import DocumentNormalizer, TextNormalizer
from .parser import ParserRegistry, UnsupportedFileTypeError
from .retriever import Retriever, VectorStoreRetriever
from .schemas import DocumentChunk, IngestionReport, ParsedDocument, RetrievalResult
from .vector_store import MilvusVectorStore, VectorStore

logger = logging.getLogger(__name__)


class RAGPipeline:
    def __init__(
        self,
        config: Optional[RAGPipelineConfig] = None,
        loader: Optional[DocumentLoader] = None,
        parser_registry: Optional[ParserRegistry] = None,
        normalizer: Optional[DocumentNormalizer] = None,
        chunker: Optional[TextChunker] = None,
        vector_store: Optional[VectorStore] = None,
        retriever: Optional[Retriever] = None,
    ):
        self.config = config or RAGPipelineConfig.from_settings()
        self.loader = loader or FileSystemDocumentLoader(self.config.supported_file_types)
        self.parser_registry = parser_registry or ParserRegistry()
        self.normalizer = normalizer or TextNormalizer()
        self.chunker = chunker or TextChunker(self.config.chunk_size, self.config.chunk_overlap)
        self.vector_store = vector_store or MilvusVectorStore(
            knowledge_base_path=self.config.knowledge_base_path,
            collection_name=self.config.collection_name,
            embedding_model=self.config.embedding_model,
            embedding_backend=self.config.embedding_backend,
            embedding_api_key=self.config.embedding_api_key,
            embedding_base_url=self.config.embedding_base_url,
            embedding_dimension=self.config.embedding_dimension,
            embedding_batch_size=self.config.embedding_batch_size,
            embedding_timeout_sec=self.config.embedding_timeout_sec,
            top_k=self.config.top_k,
            vector_top_k=self.config.vector_top_k,
            bm25_top_k=self.config.bm25_top_k,
        )
        self.retriever = retriever or VectorStoreRetriever(self.vector_store)

    def ingest(
        self,
        path: str | Path,
        rebuild: bool = False,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> IngestionReport:
        source_path = str(path)
        logger.info("Starting RAG ingestion: path=%s rebuild=%s", source_path, rebuild)
        if rebuild:
            self.vector_store.rebuild()
        if progress_callback:
            progress_callback("正在读取知识库源文件", 14)

        raw_documents = self.loader.load(path)
        if progress_callback:
            progress_callback("正在解析文档内容", 28)
        if Path(path).is_file() and raw_documents:
            file_type = raw_documents[0].file_type.lower()
            if file_type not in self.parser_registry.parsers:
                raise UnsupportedFileTypeError(file_type, raw_documents[0].source_path)

        errors: List[Dict[str, Any]] = []
        parsed_documents: List[ParsedDocument] = []
        for raw_document in raw_documents:
            try:
                parsed = self.parser_registry.parse(raw_document)
            except UnsupportedFileTypeError:
                raise
            except Exception as exc:
                logger.exception("Failed to parse RAG document: %s", raw_document.source_path)
                errors.append({"source_path": raw_document.source_path, "error": str(exc)})
                continue

            for document in parsed:
                if document.metadata.get("parse_error"):
                    errors.append(
                        {
                            "source_path": document.source_path,
                            "page_number": document.page_number,
                            "error": document.metadata["parse_error"],
                        }
                    )
                    continue
                parsed_documents.append(document)

        if progress_callback:
            progress_callback("正在规范化文档结构", 46)
        normalized = self.normalizer.normalize(parsed_documents)
        chunks: List[DocumentChunk] = []
        for document in normalized:
            try:
                chunks.extend(self.chunker.chunk([document]))
            except Exception as exc:
                logger.exception("Failed to chunk RAG document page: %s", document.source_path)
                errors.append(
                    {
                        "source_path": document.source_path,
                        "page_number": document.page_number,
                        "error": str(exc),
                    }
                )

        if progress_callback:
            progress_callback("正在切分检索片段", 62)
        add_result: Dict[str, Any] = {"added_count": 0, "total_count": self.vector_store.stats().get("total_documents", 0)}
        if chunks:
            try:
                if progress_callback:
                    progress_callback("正在生成向量并写入数据库", 72)
                add_result = self.vector_store.add_chunks(chunks)
            except Exception as exc:
                logger.exception("Failed to write RAG chunks to vector store")
                errors.append({"source_path": source_path, "error": str(exc)})
        if progress_callback:
            progress_callback("正在核对入库结果", 94)

        status = "success" if not errors else "partial_success"
        if not chunks and errors:
            status = "error"
        report = IngestionReport(
            status=status,
            source_path=source_path,
            documents_loaded=len(raw_documents),
            pages_parsed=len(normalized),
            chunks_loaded=len(chunks),
            added_count=int(add_result.get("added_count", 0) or 0),
            total_count=int(add_result.get("total_count", 0) or 0),
            errors=errors,
            metadata={
                "knowledge_base_path": self.config.knowledge_base_path,
                "collection_name": self.config.collection_name,
            },
        )
        logger.info(
            "Finished RAG ingestion: status=%s documents=%d pages=%d chunks=%d errors=%d",
            report.status,
            report.documents_loaded,
            report.pages_parsed,
            report.chunks_loaded,
            len(report.errors),
        )
        return report

    def query(self, question: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return self.retriever.retrieve(question, top_k=top_k or self.config.top_k)

    def close(self) -> None:
        self.vector_store.close()
