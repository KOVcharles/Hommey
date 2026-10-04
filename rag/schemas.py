"""Data structures shared by the RAG ingestion and retrieval pipeline.

The RAG V2 audit (docs/plans/2026-08-09-rag-v2-audit-and-roadmap.md §6.1)
introduces a layered document model: document → page → block → chunk →
operational. ``ParsedDocument`` now carries a list of ``Block`` objects in
addition to the legacy ``text`` string, and ``DocumentChunk`` carries stable
lineage identity (chunk_id/chunk_hash/ordinal) so that incremental writes can
be deduplicated and chunk ids stay stable across rebuilds under the same
document/parser/chunker versions.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---- Version constants -----------------------------------------------------
# The serialization contract version stamped onto every chunk's metadata and
# recorded in the manifest.  Bump this whenever the metadata shape changes in a
# way that old readers cannot interpret.
SCHEMA_VERSION = "rag.v2.metadata.1"
# v2: PDF pages now recognize the Chinese heading registry and the chunker
# carries a section's heading stack across PDF page breaks (audit §6.1.2).
CHUNKER_VERSION = "block-chunker-v3"
# Parser contract versions; bump when a parser's output shape changes so that
# the index fingerprint changes and a rebuild is forced.
PARSER_VERSIONS: Dict[str, str] = {
    "txt": "txt-block-v1",
    "md": "txt-block-v1",
    "pdf": "pdf-text-block-v3",
    # Phase 2 (audit §11 Phase 2): DOCX keeps paragraph/table original order;
    # CSV/XLSX form sheet/table/cell structure.
    "docx": "docx-block-v1",
    "csv": "csv-block-v1",
    "xlsx": "xlsx-block-v1",
}
PARSER_NAMES: Dict[str, str] = {
    "txt": "txt_block",
    "md": "txt_block",
    "pdf": "pdf_text",
    "docx": "docx_structured",
    "csv": "csv_table",
    "xlsx": "xlsx_table",
}

# Page terminal states (audit §7 原则 P8): every original page must have a
# terminal state so that no page silently disappears.
PAGE_STATE_NATIVE_TEXT = "native_text"
PAGE_STATE_OCR_TEXT = "ocr_text"
PAGE_STATE_INTENTIONALLY_SKIPPED = "intentionally_skipped"
PAGE_STATE_ERROR = "error"
PAGE_STATES = (
    PAGE_STATE_NATIVE_TEXT,
    PAGE_STATE_OCR_TEXT,
    PAGE_STATE_INTENTIONALLY_SKIPPED,
    PAGE_STATE_ERROR,
)

# Block types (audit §6.1.2 block layer).  "heading" blocks are paths, never
# leaves; "faq" records are complete self-contained Q&A pairs.
BLOCK_TYPE_HEADING = "heading"
BLOCK_TYPE_PARAGRAPH = "paragraph"
BLOCK_TYPE_LIST = "list"
BLOCK_TYPE_TABLE = "table"
BLOCK_TYPE_CODE = "code"
BLOCK_TYPE_FAQ = "faq"
BLOCK_TYPE_IMAGE = "image"
BLOCK_TYPE_OTHER = "other"
BLOCK_TYPES = (
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_PARAGRAPH,
    BLOCK_TYPE_LIST,
    BLOCK_TYPE_TABLE,
    BLOCK_TYPE_CODE,
    BLOCK_TYPE_FAQ,
    BLOCK_TYPE_IMAGE,
    BLOCK_TYPE_OTHER,
)

# Block types that are complete self-contained records: never merged with
# neighbors and never split mid-block (audit §14: 代码块不被中间切断).
ATOMIC_BLOCK_TYPES = frozenset({BLOCK_TYPE_CODE, BLOCK_TYPE_TABLE, BLOCK_TYPE_FAQ})


def _block_id(
    page_number: Optional[int],
    seq: int,
    prefix: Optional[str] = None,
) -> str:
    """Deterministic block identity (audit §6.1.2).

    Paginated documents carry a ``p{page}-b{seq}`` prefix; non-paginated
    documents (TXT/MD) have no page anchor and use ``b{seq}`` so their location
    segment in ``chunk_id`` stays section-based instead of faking a page.
    Spreadsheet sheets (Phase 2) pass an explicit ``prefix`` (e.g. ``s1``) so
    their block identity and ``chunk_id`` location segment stay sheet-based.
    """
    if prefix:
        return f"{prefix}-b{seq}"
    if page_number is None:
        return f"b{seq}"
    return f"p{page_number}-b{seq}"


def _document_version_from_bytes(content: bytes, length: int = 12) -> str:
    return hashlib.sha256(content).hexdigest()[:length]


@dataclass(frozen=True)
class RawDocument:
    content: bytes
    source_path: str
    filename: str
    file_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def document_version(self) -> str:
        return self.metadata.get("document_version") or _document_version_from_bytes(self.content)


@dataclass(frozen=True)
class Block:
    """A structural unit produced by a parser.

    ``block_type`` is one of :data:`BLOCK_TYPES`.  Heading blocks carry a
    ``heading_path`` that includes themselves and is inherited by every
    following content block on the same page (audit §7 原则 P2).

    ``table_data`` (Phase 2, audit §6.1.2 block layer) holds the structured
    grid of a ``table`` block: ``{sheet, table_id, cells, row_span, col_span}``.
    The chunker converges it into the chunk-level ``table`` citation field.
    """

    block_id: str
    block_type: str
    text: str
    level: int = 0
    heading_path: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    table_data: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    source_path: str
    filename: str
    file_type: str
    page_number: Optional[int] = None
    content_type: str = "text"
    title: str = ""
    category: str = "business_travel"
    metadata: Dict[str, Any] = field(default_factory=dict)
    blocks: List[Block] = field(default_factory=list)
    page_terminal_state: str = PAGE_STATE_NATIVE_TEXT
    parser_name: str = ""
    parser_version: str = ""
    # Phase 2 generalized location anchor (audit §6.1.2/§6.1.3): PDF pages use
    # "p3", XLSX sheets "s1", TXT/MD sections derive from headings ("c{n}").
    # Empty means the chunker derives the default location segment.
    location: str = ""

    @property
    def document_id(self) -> str:
        return self.metadata.get("document_id") or Path(self.source_path).name

    @property
    def document_version(self) -> str:
        return self.metadata.get("document_version") or ""


@dataclass(frozen=True)
class DocumentChunk:
    content: str
    source_path: str
    filename: str
    file_type: str
    page_number: Optional[int]
    chunk_index: int
    content_type: str
    hash: str
    title: str = ""
    category: str = "business_travel"
    metadata: Dict[str, Any] = field(default_factory=dict)
    # --- V2 lineage fields (audit §6.1.2 / §6.1.3) ---
    chunk_id: str = ""
    chunk_hash: str = ""
    chunk_ordinal: int = 0
    chunk_index_within_page: int = 0
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    block_ids: List[str] = field(default_factory=list)
    heading_path: List[str] = field(default_factory=list)
    retrieval_text: str = ""
    display_text: str = ""
    text_source: str = "native"
    index_version: str = ""
    document_id: str = ""
    document_version: str = ""
    parser_name: str = ""
    parser_version: str = ""
    chunker_version: str = CHUNKER_VERSION
    schema_version: str = SCHEMA_VERSION
    # Phase 2 table citation (audit §6.1.2 chunk layer): converged from the
    # block's table_data so answers can locate a cell via sheet/table_id/row/col.
    table: Optional[Dict[str, Any]] = None

    def to_metadata(self) -> Dict[str, Any]:
        """The unique metadata assembly point (audit §6.1.1).

        Every canonical key is emitted together with the compatibility aliases
        that legacy readers actually consume.  No parser or chunker writes
        metadata keys directly anymore.
        """
        data = dict(self.metadata)
        heading_path = list(self.heading_path or [])
        data.update(
            {
                "schema_version": self.schema_version,
                "document_id": self.document_id,
                "document_version": self.document_version,
                "chunk_id": self.chunk_id,
                "chunk_hash": self.chunk_hash or self.hash,
                "chunk_ordinal": self.chunk_ordinal,
                # `chunk_index` was previously reset per page (audit §4.7).  It
                # is now a legacy alias of the document-level ordinal so old
                # readers keep seeing a monotonic counter, while the true
                # page-level counter lives in `chunk_index_within_page`.
                "chunk_index": self.chunk_ordinal or self.chunk_index,
                "chunk_index_within_page": self.chunk_index_within_page,
                "page_number": self.page_number,
                "page_start": self.page_start if self.page_start is not None else self.page_number,
                "page_end": self.page_end if self.page_end is not None else self.page_number,
                "block_ids": list(self.block_ids or []),
                "heading_path": heading_path,
                "content_type": self.content_type,
                "retrieval_text": self.retrieval_text or self.content,
                "display_text": self.display_text or self.content,
                "text_source": self.text_source,
                "index_version": self.index_version,
                "source_path": self.source_path,
                "filename": self.filename,
                "file_type": self.file_type,
                "title": self.title,
                "category": self.category,
                "hash": self.hash,
                "parser_name": self.parser_name,
                "parser_version": self.parser_version,
                "chunker_version": self.chunker_version,
            }
        )
        if self.table is not None:
            data["table"] = self.table
        # Compatibility aliases for legacy readers.  `source` now points at the
        # real source path (never the old "business_travel_documents" constant).
        data.setdefault("file_path", self.source_path)
        data.setdefault("source", self.source_path)
        data.setdefault("file_name", self.filename)
        data.setdefault("parent_doc", self.filename)
        data.setdefault("page", self.page_number)
        data.setdefault("section", "/".join(heading_path) or self.title or None)
        return data


@dataclass(frozen=True)
class RetrievalResult:
    id: Any
    content: str
    metadata: Dict[str, Any]
    distance: Optional[float] = None
    vector_rank: Optional[int] = None
    bm25_rank: Optional[int] = None
    bm25_score: Optional[float] = None
    fusion_score: float = 0.0
    rerank_score: Optional[float] = None
    retrieval_trace_id: Optional[str] = None

    @property
    def chunk_id(self) -> str:
        if isinstance(self.metadata, dict):
            return str(self.metadata.get("chunk_id") or "")
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "metadata": self.metadata,
            "distance": self.distance,
            "vector_rank": self.vector_rank,
            "bm25_rank": self.bm25_rank,
            "bm25_score": self.bm25_score,
            "fusion_score": self.fusion_score,
            "rerank_score": self.rerank_score,
            "retrieval_trace_id": self.retrieval_trace_id,
            "chunk_id": self.chunk_id,
        }


@dataclass(frozen=True)
class IngestionReport:
    status: str
    source_path: str
    documents_loaded: int = 0
    pages_parsed: int = 0
    chunks_loaded: int = 0
    added_count: int = 0
    total_count: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "source_path": self.source_path,
            "documents_loaded": self.documents_loaded,
            "pages_parsed": self.pages_parsed,
            "chunks_loaded": self.chunks_loaded,
            "added_count": self.added_count,
            "total_count": self.total_count,
            "errors": self.errors,
            **self.metadata,
        }
