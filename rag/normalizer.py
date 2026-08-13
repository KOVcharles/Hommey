"""Content normalization for parsed RAG documents.

Audit §7 原则 P5: normalization dispatches on block type.  Paragraph/heading/
faq/list blocks get their whitespace collapsed; code/table blocks keep their
raw spacing so column alignment and indentation survive.  The legacy
``ParsedDocument.text`` field is rebuilt from the normalized blocks so older
callers keep seeing the same content they used to.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Callable, Dict, List

from .schemas import (
    ATOMIC_BLOCK_TYPES,
    BLOCK_TYPE_CODE,
    BLOCK_TYPE_FAQ,
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_LIST,
    BLOCK_TYPE_PARAGRAPH,
    BLOCK_TYPE_TABLE,
    Block,
    ParsedDocument,
)

# Block types whose whitespace is significant.  Everything else is prose and
# gets line-level whitespace folding.
_PRESERVE_WHITESPACE = frozenset({BLOCK_TYPE_CODE, BLOCK_TYPE_TABLE})


def is_atomic_block(block_type: str) -> bool:
    return block_type in ATOMIC_BLOCK_TYPES


class DocumentNormalizer(ABC):
    @abstractmethod
    def normalize(self, documents: List[ParsedDocument]) -> List[ParsedDocument]:
        raise NotImplementedError


class TextNormalizer(DocumentNormalizer):
    def normalize(self, documents: List[ParsedDocument]) -> List[ParsedDocument]:
        normalized: List[ParsedDocument] = []
        for document in documents:
            blocks = [_normalize_block(block) for block in document.blocks]
            normalized.append(
                ParsedDocument(
                    text=_render_text(blocks, document.text),
                    source_path=document.source_path,
                    filename=document.filename,
                    file_type=document.file_type,
                    page_number=document.page_number,
                    content_type=document.content_type,
                    title=document.title,
                    category=document.category,
                    metadata=document.metadata,
                    blocks=blocks,
                    page_terminal_state=document.page_terminal_state,
                    parser_name=document.parser_name,
                    parser_version=document.parser_version,
                    # Phase 2: the sheet-based location anchor (e.g. "s1") is part
                    # of document identity and must survive normalization.
                    location=document.location,
                )
            )
        return normalized


def _normalize_block(block: Block) -> Block:
    if block.block_type in _PRESERVE_WHITESPACE:
        text = block.text
    else:
        text = _fold_whitespace(block.text)
    return Block(
        block_id=block.block_id,
        block_type=block.block_type,
        text=text,
        level=block.level,
        heading_path=list(block.heading_path),
        metadata=dict(block.metadata),
        # Phase 2: the structured grid of a table block must survive
        # normalization so the chunker can converge it into the chunk-level
        # table citation.
        table_data=block.table_data,
    )


def _fold_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs per line and normalize line endings."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def _render_text(blocks: List[Block], fallback: str) -> str:
    """Rebuild the legacy ``text`` field from normalized blocks."""
    if not blocks:
        return fallback
    return "\n\n".join(block.text for block in blocks if block.text).strip()
