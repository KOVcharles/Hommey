"""Composable RAG ingestion and query pipeline."""
from __future__ import annotations

import dataclasses
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .chunker import BlockChunker, TextChunker
from .config import RAGPipelineConfig
from .loader import DocumentLoader, FileSystemDocumentLoader
from .normalizer import DocumentNormalizer, TextNormalizer
from .ocr import PageOcrFallback
from .parser import ParserRegistry, UnsupportedFileTypeError
from .retriever import Retriever, VectorStoreRetriever
from .schemas import DocumentChunk, IngestionReport, ParsedDocument, RetrievalResult
from .vector_store import VectorStore, create_vector_store
from .versions import index_version_block

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RAGPipeline:
    def __init__(
        self,
        config: Optional[RAGPipelineConfig] = None,
        loader: Optional[DocumentLoader] = None,
        parser_registry: Optional[ParserRegistry] = None,
        normalizer: Optional[DocumentNormalizer] = None,
        chunker: Optional[Any] = None,
        vector_store: Optional[VectorStore] = None,
        retriever: Optional[Retriever] = None,
        ocr_fallback: Optional[PageOcrFallback] = None,
    ):
        self.config = config or RAGPipelineConfig.from_settings()
        self.loader = loader or FileSystemDocumentLoader(self.config.supported_file_types)
        self.parser_registry = parser_registry or ParserRegistry()
        self.normalizer = normalizer or TextNormalizer()
        # Phase 3: flag-gated OCR fallback for text-less PDF pages.  A caller
        # may inject a fake OCR client (tests); the default builds the shared
        # document OCR client lazily and only acts when config.ocr_enabled is true.
        self.ocr_fallback = ocr_fallback or PageOcrFallback(
            enabled=self.config.ocr_enabled,
            confidence_threshold=self.config.ocr_confidence_threshold,
        )
        # Phase-1 default is the structured block chunker; legacy callers may
        # still inject the character-window TextChunker.
        self.chunker = chunker or BlockChunker(
            min_tokens=self.config.chunk_min_tokens,
            max_tokens=self.config.chunk_max_tokens,
            overlap_tokens=self.config.chunk_overlap_tokens,
        )
        self.vector_store = vector_store or create_vector_store(self.config)
        self.retriever = retriever or VectorStoreRetriever(self.vector_store)

    def ingest(
        self,
        path: str | Path,
        rebuild: bool = False,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> IngestionReport:
        source_path = str(path)
        logger.info("Starting RAG ingestion: path=%s rebuild=%s", source_path, rebuild)
        if progress_callback:
            progress_callback("正在读取知识库源文件", 14)

        raw_documents = self.loader.load(path)
        if progress_callback:
            progress_callback("正在解析文档内容", 28)
        if Path(path).is_file() and raw_documents:
            file_type = raw_documents[0].file_type.lower()
            if file_type not in self.parser_registry.parsers:
                raise UnsupportedFileTypeError(file_type, raw_documents[0].source_path)

        # Freeze the index fingerprint once at pipeline entry (audit §6.1.5).
        version_block = index_version_block(self.config)
        index_version = version_block["index"]["version"]

        errors: List[Dict[str, Any]] = []
        parsed_documents: List[ParsedDocument] = []
        page_states: Dict[str, Counter] = {}
        doc_versions: Dict[str, Dict[str, Any]] = {}
        for raw_document in raw_documents:
            try:
                parsed = self.parser_registry.parse(raw_document)
            except UnsupportedFileTypeError:
                raise
            except Exception as exc:
                logger.exception("Failed to parse RAG document: %s", raw_document.source_path)
                errors.append({"source_path": raw_document.source_path, "error": str(exc)})
                continue

            # Phase 3: OCR fallback replaces text-less PDF pages before the
            # empty-page filter below, so a scan only survives if OCR recovers
            # it (or records why it could not — P8 terminal state).
            parsed = self.ocr_fallback.apply(parsed)

            for document in parsed:
                document_id = self._document_id_for(document.source_path)
                document = dataclasses.replace(
                    document, metadata={**document.metadata, "document_id": document_id}
                )
                states = page_states.setdefault(document_id, Counter())
                states[document.page_terminal_state] += 1
                doc_versions.setdefault(
                    document_id,
                    {
                        "document_version": document.document_version,
                        "parser_name": document.parser_name,
                        "parser_version": document.parser_version,
                    },
                )
                if document.metadata.get("parse_error"):
                    errors.append(
                        {
                            "source_path": document.source_path,
                            "page_number": document.page_number,
                            "error": document.metadata["parse_error"],
                        }
                    )
                    continue
                # Empty pages (intentionally_skipped / error) are recorded in
                # page_states but never become chunks (§7 P8).
                if document.text or document.blocks:
                    parsed_documents.append(document)

        if progress_callback:
            progress_callback("正在规范化文档结构", 46)
        normalized = self.normalizer.normalize(parsed_documents)

        grouped: Dict[str, List[ParsedDocument]] = {}
        for document in normalized:
            grouped.setdefault(document.document_id, []).append(document)

        chunks: List[DocumentChunk] = []
        chunks_by_doc: Counter = Counter()
        for document_id, pages in grouped.items():
            try:
                doc_chunks = self.chunker.chunk(pages)
            except Exception as exc:
                logger.exception("Failed to chunk RAG document: %s", document_id)
                errors.append({"source_path": document_id, "error": str(exc)})
                continue
            chunks.extend(doc_chunks)
            chunks_by_doc[document_id] += len(doc_chunks)

        # Stamp the frozen index fingerprint on every chunk before writing.
        chunks = [
            dataclasses.replace(
                chunk,
                index_version=index_version,
                schema_version=version_block["schema_version"],
            )
            for chunk in chunks
        ]

        if progress_callback:
            progress_callback("正在切分检索片段", 62)
        add_result: Dict[str, Any] = {
            "added_count": 0,
            "total_count": self.vector_store.stats().get("total_documents", 0),
        }
        if rebuild and not raw_documents:
            errors.append({"source_path": source_path, "error": "没有找到可入库的知识库文档"})
        elif rebuild and not chunks and not errors:
            errors.append({"source_path": source_path, "error": "文档没有生成任何可检索片段"})

        # A full refresh is all-or-nothing.  Parsing/chunking failures leave the
        # live collection untouched rather than publishing a partial policy set.
        should_write = bool(chunks) and (not rebuild or not errors)
        if should_write:
            try:
                if progress_callback:
                    progress_callback("正在生成向量并写入数据库", 72)
                add_result = (
                    self.vector_store.replace_chunks(chunks)
                    if rebuild else self.vector_store.add_chunks(chunks)
                )
            except Exception as exc:
                logger.exception("Failed to write RAG chunks to vector store")
                errors.append({"source_path": source_path, "error": str(exc)})
        if progress_callback:
            progress_callback("正在核对入库结果", 94)

        status = "success" if not errors else "partial_success"
        if rebuild and errors:
            status = "error"
        elif not chunks and errors:
            status = "error"

        documents_report = {}
        for document_id, version_info in doc_versions.items():
            documents_report[document_id] = {
                **version_info,
                "pages": dict(page_states.get(document_id, {})),
                "chunk_count": chunks_by_doc.get(document_id, 0),
            }
        report = IngestionReport(
            status=status,
            source_path=source_path,
            documents_loaded=len(raw_documents),
            pages_parsed=sum(sum(states.values()) for states in page_states.values()),
            chunks_loaded=len(chunks),
            added_count=int(add_result.get("added_count", 0) or 0),
            total_count=int(add_result.get("total_count", 0) or 0),
            errors=errors,
            metadata={
                "knowledge_base_path": self.config.knowledge_base_path,
                "collection_name": self.config.collection_name,
                "schema_version": version_block["schema_version"],
                "index": {
                    **version_block["index"],
                    "built_at": _utc_now_iso(),
                    "trigger": "full_rebuild" if rebuild else "incremental",
                    "collection_name": self.config.collection_name,
                },
                "documents": documents_report,
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

    def _document_id_for(self, source_path: str) -> str:
        """Document identity is the posix path relative to documents_dir, aligned
        with the ingestion_manifest documents keys (audit §6.1.2).  Files outside
        documents_dir fall back to their filename."""
        anchor = Path(self.config.documents_dir).resolve()
        try:
            return Path(source_path).resolve().relative_to(anchor).as_posix()
        except ValueError:
            return Path(source_path).name

    def query(self, question: str, top_k: Optional[int] = None) -> List[RetrievalResult]:
        return self.retriever.retrieve(question, top_k=top_k or self.config.top_k)

    def close(self) -> None:
        self.vector_store.close()
