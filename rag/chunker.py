"""Text chunking utilities for RAG ingestion.

Two chunkers coexist:

- ``TextChunker`` — the legacy character-window chunker over raw text.  Kept for
  old callers (``chunk_document``, ``split_text``) that are outside the audit's
  Phase-1 scope.
- ``BlockChunker`` — the Phase-1 structured chunker (audit §7 P1-P8).  It only
  consumes ``Block`` sequences, attaches headings as paths (P2), merges short
  adjacent segments under the same parent (P3), sizes chunks in tokens (P7),
  and derives stable identity from the document lineage (P6).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .normalizer import is_atomic_block
from .schemas import (
    ATOMIC_BLOCK_TYPES,
    BLOCK_TYPE_CODE,
    BLOCK_TYPE_FAQ,
    BLOCK_TYPE_HEADING,
    BLOCK_TYPE_LIST,
    BLOCK_TYPE_TABLE,
    CHUNKER_VERSION,
    PAGE_STATE_OCR_TEXT,
    Block,
    DocumentChunk,
    ParsedDocument,
    SourceDocument,
)
from .structured_parser import render_cells

_QUESTION_HEADING_RE = re.compile(r"^Q\d+\s*[:：]", re.IGNORECASE)
_SECTION_HEADING_RE = re.compile(r"^[一二三四五六七八九十]+[、.．]\s*\S+")
_NUMBERED_HEADING_RE = re.compile(r"^\d+[.．]\s+\S+")

_CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])")


def token_count(text: str) -> int:
    """Token estimator shared by the chunker and tests.

    CJK characters count one token each; Latin/ASCII words count one token
    each.  This is a deterministic, dependency-free approximation the audit's
    P7 sizing can be tuned from.
    """
    if not text:
        return 0
    return len(_CJK_RE.findall(text)) + len(_LATIN_WORD_RE.findall(text))


class TextChunker:
    def __init__(self, max_chars: int = 600, overlap: int = 100):
        self.max_chars = max_chars
        self.overlap = overlap

    def chunk(self, documents: List[ParsedDocument]) -> List[DocumentChunk]:
        chunks: List[DocumentChunk] = []
        chunk_index = 1
        for document in documents:
            for content in split_text(document.text, max_chars=self.max_chars, overlap=self.overlap):
                chunk_hash = _hash_chunk(document.source_path, document.page_number, chunk_index, content)
                metadata = dict(document.metadata)
                metadata.setdefault("parent_doc", document.filename)
                chunks.append(
                    DocumentChunk(
                        content=content,
                        source_path=document.source_path,
                        filename=document.filename,
                        file_type=document.file_type,
                        page_number=document.page_number,
                        chunk_index=chunk_index,
                        content_type=document.content_type,
                        hash=chunk_hash,
                        title=document.title,
                        category=document.category,
                        metadata=metadata,
                    )
                )
                chunk_index += 1
        return chunks


def split_text(text: str, max_chars: int = 600, overlap: int = 100) -> List[str]:
    """Split text into topic-aware chunks with an overlap fallback for long blocks."""
    if not text.strip():
        return []

    paragraphs = _paragraphs(text)
    chunks: List[str] = []
    current_chunk = ""
    for paragraph in paragraphs:
        if not paragraph:
            continue

        starts_topic = _starts_new_topic(paragraph)
        if current_chunk and starts_topic:
            chunks.extend(_split_long_text(current_chunk, max_chars, overlap))
            current_chunk = ""

        if current_chunk and len(current_chunk) + len(paragraph) + 2 > max_chars:
            chunks.extend(_split_long_text(current_chunk, max_chars, overlap))
            current_chunk = ""

        if len(paragraph) > max_chars:
            if current_chunk:
                chunks.extend(_split_long_text(current_chunk, max_chars, overlap))
                current_chunk = ""
            chunks.extend(_split_long_text(paragraph, max_chars, overlap))
            continue

        current_chunk = f"{current_chunk}\n\n{paragraph}".strip()

    if current_chunk:
        chunks.extend(_split_long_text(current_chunk, max_chars, overlap))
    return chunks


def chunk_document(document: SourceDocument, max_chars: int = 600, overlap: int = 100) -> List[DocumentChunk]:
    parsed = ParsedDocument(
        text=document.content,
        source_path=document.source_path,
        filename=document.filename,
        file_type=document.file_type,
        page_number=document.metadata.get("page_number"),
        title=document.title,
        category=document.category,
        metadata=document.metadata,
    )
    return TextChunker(max_chars=max_chars, overlap=overlap).chunk([parsed])


# ---- Phase-1 structured chunker (audit §7) ----------------------------------

@dataclass
class _Unit:
    """A logical merge unit: a heading chain plus its following content blocks,
    or a single atomic block (faq/code/table)."""

    blocks: List[Block] = field(default_factory=list)
    atomic: bool = False

    def parent_heading(self) -> Optional[str]:
        """Top-level heading path element shared with sibling units, or None."""
        for block in self.blocks:
            if block.block_type != BLOCK_TYPE_HEADING and block.heading_path:
                return block.heading_path[0]
        return None

    def token_count(self) -> int:
        return sum(token_count(block.text) for block in self.blocks)

    def dominant_type(self) -> str:
        """The chunk-level content type for rerank weighting (audit §6.1.2)."""
        types = [block.block_type for block in self.blocks if block.block_type != BLOCK_TYPE_HEADING]
        for preferred in (BLOCK_TYPE_FAQ, BLOCK_TYPE_TABLE, BLOCK_TYPE_CODE, BLOCK_TYPE_LIST):
            if preferred in types:
                return preferred
        return "paragraph"


class BlockChunker:
    """Merge-then-split chunker over block sequences (audit §7 P1-P8)."""

    def __init__(self, min_tokens: int = 150, max_tokens: int = 400, overlap_tokens: int = 60):
        self.min_tokens = int(min_tokens)
        self.max_tokens = int(max_tokens)
        self.overlap_tokens = int(overlap_tokens)

    def chunk(self, documents: List[ParsedDocument]) -> List[DocumentChunk]:
        """Chunk pages grouped by document so ``chunk_ordinal`` is document-level
        and never resets per page (audit §4.7)."""
        chunks: List[DocumentChunk] = []
        by_document: dict = {}
        for document in documents:
            by_document.setdefault(document.document_id, []).append(document)
        for pages in by_document.values():
            chunks.extend(self._chunk_document_group(pages))
        return chunks

    def _chunk_document_group(self, pages: List[ParsedDocument]) -> List[DocumentChunk]:
        if not pages:
            return []
        first = pages[0]
        document_id = first.document_id
        document_version = first.document_version
        chunks: List[DocumentChunk] = []
        ordinal = 0
        seen_l1 = 0
        # A PDF section can span pages: the heading stack open at the end of a
        # page is carried into the next page so continuation paragraphs keep
        # their section attribution (audit §6.1.2).  TXT/MD are single-page.
        carried: List[str] = []
        for page in pages:
            page_index = 0
            units = self._build_units(page.blocks)
            for unit in units:
                seen_l1 += sum(1 for block in unit.blocks if block.block_type == BLOCK_TYPE_HEADING and block.level == 1)
                if page.location:
                    # Phase 2 (audit §6.1.3): parsers may pin an explicit location
                    # anchor (XLSX sheets -> "s1").  It wins over the defaults so
                    # chunk_id's location segment stays sheet-based.
                    location = page.location
                elif page.file_type == "pdf":
                    location = f"p{page.page_number}"
                else:
                    location = f"c{seen_l1}"
                for part in self._split_unit(unit):
                    ordinal += 1
                    page_index += 1
                    chunks.append(
                        self._make_chunk(
                            page=page,
                            blocks=part,
                            location=location,
                            ordinal=ordinal,
                            page_index=page_index,
                            document_id=document_id,
                            document_version=document_version,
                            heading_path=self._effective_heading_path(part, carried),
                        )
                    )
            carried = self._page_open_stack(page.blocks, carried)
        return chunks

    def _build_units(self, blocks: List[Block]) -> List[_Unit]:
        """Group blocks into merge units (P2/P3).  A heading chain followed by
        its content forms one unit; a heading-only chain attaches to the unit
        that follows it so no unit is ever heading-only."""
        units: List[_Unit] = []
        current: List[Block] = []
        has_content = False
        for block in blocks:
            if block.block_type == BLOCK_TYPE_HEADING:
                if has_content:
                    units.append(_Unit(blocks=current))
                    current = [block]
                    has_content = False
                else:
                    current.append(block)
                continue
            if is_atomic_block(block.block_type):
                if current:
                    units.append(_Unit(blocks=current))
                    current = []
                    has_content = False
                units.append(_Unit(blocks=[block], atomic=True))
                continue
            current.append(block)
            has_content = True
        if current:
            units.append(_Unit(blocks=current))
        units = self._attach_heading_only_units(units)
        return self._merge_short_units(units)

    @staticmethod
    def _is_heading_only(unit: _Unit) -> bool:
        return bool(unit.blocks) and all(block.block_type == BLOCK_TYPE_HEADING for block in unit.blocks)

    def _attach_heading_only_units(self, units: List[_Unit]) -> List[_Unit]:
        """P2 invariant: no leaf chunk is heading-only.  Heading-only chains
        (a heading with no content before a boundary) merge into the previous
        unit; a leading chain prepends to the first content unit."""
        result: List[_Unit] = []
        for unit in units:
            if self._is_heading_only(unit) and result:
                result[-1].blocks.extend(unit.blocks)
                continue
            result.append(unit)
        if result and self._is_heading_only(result[0]):
            leading = result.pop(0)
            if result:
                result[0].blocks = leading.blocks + result[0].blocks
            # else: the whole document is headings-only — there is no content
            # leaf to attach to, so emit nothing (P2: no heading-only chunks).
        return result

    def _merge_short_units(self, units: List[_Unit]) -> List[_Unit]:
        """P3: repeatedly fuse adjacent same-parent units while their combined
        size fits one chunk, so short segments stop fragmenting.  Merging the
        right unit into the left preserves document order."""
        result = list(units)
        changed = True
        while changed:
            changed = False
            for index in range(len(result) - 1):
                left, right = result[index], result[index + 1]
                if left.atomic or right.atomic:
                    continue
                if left.parent_heading() != right.parent_heading():
                    continue
                if left.token_count() + right.token_count() > self.max_tokens:
                    continue
                left.blocks.extend(right.blocks)
                del result[index + 1]
                changed = True
                break
        return result

    def _split_unit(self, unit: _Unit) -> List[List[Block]]:
        """Split an overlong unit at block boundaries; a single overlong prose
        block splits at sentence boundaries instead (audit §14: 超长段按句子/
        列表边界拆分，代码块不被中间切断)."""
        if unit.token_count() <= self.max_tokens:
            return [list(unit.blocks)]

        parts: List[List[Block]] = []
        current: List[Block] = []
        current_tokens = 0
        for block in unit.blocks:
            tokens = token_count(block.text)
            # Never flush a heading-only run: a heading stays glued to the block
            # that follows it so splitting can never produce a heading leaf (P2).
            if current and current_tokens + tokens > self.max_tokens and self._has_content(current):
                parts.append(current)
                current = []
                current_tokens = 0
            current.append(block)
            current_tokens += tokens
        if current:
            parts.append(current)

        final_parts: List[List[Block]] = []
        for part in parts:
            final_parts.extend(self._split_part(part))
        return final_parts

    def _split_part(self, part: List[Block]) -> List[List[Block]]:
        """Split an overlong part at sentence boundaries.

        The block-boundary loop above refuses to flush a heading-only run (P2),
        so an overlong paragraph under a heading chain reaches here as
        ``[heading..., big_paragraph]``.  That single content block is
        sentence-split (audit §14: 超长段按句子/列表边界拆分) with the heading
        chain kept glued to the first piece only — later pieces are pure prose,
        and a code/table block is never cut mid-way.
        """
        non_heading = [block for block in part if block.block_type != BLOCK_TYPE_HEADING]
        if len(non_heading) != 1:
            return [part]
        content = non_heading[0]
        if token_count(content.text) <= self.max_tokens:
            return [part]
        leading_headings = [block for block in part if block.block_type == BLOCK_TYPE_HEADING]
        # An overlong table is atomic: never cut mid-row, but band it at row
        # boundaries (audit §14: 表格按行分带).  Code/faq stay unsplit.
        if content.block_type == BLOCK_TYPE_TABLE:
            bands = self._band_table_block(content)
            return [
                list(leading_headings) + [band] if index == 0 else [band]
                for index, band in enumerate(bands)
            ]
        if content.block_type in ATOMIC_BLOCK_TYPES:
            return [part]
        pieces = self._split_block_text(content)
        return [
            list(leading_headings) + piece if index == 0 else piece
            for index, piece in enumerate(pieces)
        ]

    def _band_table_block(self, block: Block) -> List[Block]:
        """Split an overlong table into consecutive row bands (audit §14).

        Atomic tables are never cut mid-row; an overlong table is instead split
        at row boundaries into bands, each carrying its own ``table_data`` with
        ``band``/``band_row_start``/``band_row_end`` so the chunk-level citation
        can still name a sheet/table_id/row range.  A band whose single row is
        itself over budget stays whole rather than breaking a row apart.
        """
        table_data = block.table_data or {}
        cells = table_data.get("cells") or []
        if not cells:
            return [block]

        bands: List[List[List[object]]] = []
        current: List[List[object]] = []
        current_tokens = 0
        for row in cells:
            row_tokens = token_count(render_cells([row]))
            if current and current_tokens + row_tokens > self.max_tokens:
                bands.append(current)
                current = []
                current_tokens = 0
            current.append(row)
            current_tokens += row_tokens
        if current:
            bands.append(current)
        if len(bands) <= 1:
            return [block]

        result: List[Block] = []
        offset = 0
        for index, band_rows in enumerate(bands):
            band_data = dict(table_data)
            band_data["cells"] = band_rows
            band_data["band"] = index
            band_data["band_total"] = len(bands)
            band_data["band_row_start"] = offset
            band_data["band_row_end"] = offset + len(band_rows) - 1
            result.append(
                Block(
                    block_id=f"{block.block_id}-band{index}",
                    block_type=BLOCK_TYPE_TABLE,
                    text=render_cells(band_rows),
                    level=block.level,
                    heading_path=list(block.heading_path),
                    metadata=dict(block.metadata),
                    table_data=band_data,
                )
            )
            offset += len(band_rows)
        return result

    @staticmethod
    def _table_citation(blocks: List[Block]) -> Optional[Dict[str, object]]:
        """Chunk-level table citation (audit §6.1.2 chunk layer).

        Names the sheet/table_id and the exact row/column window the chunk
        covers; cell values live in the rendered ``content``, so the citation
        stays small in metadata.  Banded chunks carry their band's row window
        instead of restarting at 0.
        """
        tables = [
            block for block in blocks
            if block.block_type == BLOCK_TYPE_TABLE and block.table_data
        ]
        if not tables:
            return None
        sheet = None
        table_id = ""
        row_start: Optional[int] = None
        row_end: Optional[int] = None
        col_end = 0
        for table_block in tables:
            data = table_block.table_data or {}
            sheet = sheet if sheet is not None else data.get("sheet")
            table_id = table_id or data.get("table_id") or ""
            cells = data.get("cells") or []
            if not cells:
                continue
            if "band_row_start" in data:
                start = int(data["band_row_start"])
                end = int(data["band_row_end"])
            else:
                start = 0
                end = len(cells) - 1
            row_start = start if row_start is None else min(row_start, start)
            row_end = end if row_end is None else max(row_end, end)
            col_end = max(col_end, max((len(row) for row in cells), default=0) - 1)
        if row_start is None or row_end is None:
            return None
        return {
            "sheet": sheet,
            "table_id": table_id,
            "row_start": row_start,
            "row_end": row_end,
            "col_start": 0,
            "col_end": col_end,
        }

    def _split_block_text(self, block: Block) -> List[List[Block]]:
        """Split one overlong paragraph at sentence boundaries with token overlap."""
        sentences = [part for part in _SENTENCE_BOUNDARY_RE.split(block.text) if part.strip()]
        if not sentences:
            return [[block]]

        parts: List[str] = []
        current = ""
        current_tokens = 0
        for sentence in sentences:
            tokens = token_count(sentence)
            if current and current_tokens + tokens > self.max_tokens:
                parts.append(current)
                current = _tail_overlap(current, self.overlap_tokens)
                current_tokens = token_count(current)
            current += sentence
            current_tokens += tokens
        if current.strip():
            parts.append(current)
        return [[_block_with_text(block, text=part.strip())] for part in parts if part.strip()]

    def _make_chunk(
        self,
        *,
        page: ParsedDocument,
        blocks: List[Block],
        location: str,
        ordinal: int,
        page_index: int,
        document_id: str,
        document_version: str,
        heading_path: Optional[List[str]] = None,
    ) -> DocumentChunk:
        display_text = "\n\n".join(block.text for block in blocks if block.text).strip()
        heading_path = self._heading_path(blocks) if heading_path is None else heading_path
        first_block_id = blocks[0].block_id if blocks else ""
        chunk_id = f"{document_id}::{document_version}::{location}::{first_block_id}::{ordinal:02d}"
        content_type = self._dominant_type(blocks)
        chunk_hash = hashlib.sha256(display_text.encode("utf-8")).hexdigest()[:16]
        unit = _Unit(blocks=blocks)
        return DocumentChunk(
            content=display_text,
            source_path=page.source_path,
            filename=page.filename,
            file_type=page.file_type,
            page_number=page.page_number,
            chunk_index=page_index,
            content_type=content_type,
            hash=chunk_hash,
            title=page.title,
            category=page.category,
            metadata=dict(page.metadata),
            chunk_id=chunk_id,
            chunk_hash=chunk_hash,
            chunk_ordinal=ordinal,
            page_start=page.page_number,
            page_end=page.page_number,
            block_ids=[block.block_id for block in blocks if block.block_id],
            heading_path=heading_path,
            retrieval_text=display_text,
            display_text=display_text,
            text_source=("ocr" if page.page_terminal_state == PAGE_STATE_OCR_TEXT else "native"),
            index_version="",
            document_id=document_id,
            document_version=document_version,
            parser_name=page.parser_name,
            parser_version=page.parser_version,
            chunker_version=CHUNKER_VERSION,
            table=self._table_citation(blocks),
        )

    @staticmethod
    def _heading_path(blocks: List[Block]) -> List[str]:
        for block in blocks:
            if block.block_type != BLOCK_TYPE_HEADING and block.heading_path:
                return list(block.heading_path)
        return []

    @staticmethod
    def _effective_heading_path(blocks: List[Block], carried: List[str]) -> List[str]:
        """The heading path a part inherits: its own content blocks' path when
        present, otherwise the stack carried over from the previous page."""
        for block in blocks:
            if block.block_type != BLOCK_TYPE_HEADING and block.heading_path:
                return list(block.heading_path)
        return list(carried)

    @staticmethod
    def _page_open_stack(blocks: List[Block], fallback: List[str]) -> List[str]:
        """The heading stack still open when a page ends; ``fallback`` when the
        page introduced no heading state of its own."""
        if not blocks:
            return list(fallback)
        last = blocks[-1]
        if last.block_type == BLOCK_TYPE_HEADING:
            return list(last.heading_path) + [last.text]
        return list(last.heading_path) if last.heading_path else list(fallback)

    @staticmethod
    def _has_content(blocks: List[Block]) -> bool:
        return any(block.block_type != BLOCK_TYPE_HEADING for block in blocks)

    @staticmethod
    def _dominant_type(blocks: List[Block]) -> str:
        return _Unit(blocks=blocks).dominant_type()


def _tail_overlap(text: str, overlap_tokens: int) -> str:
    """A rough tail window for overlap: the last ~2× the token budget, cut back
    to a sentence boundary so the next chunk does not start mid-sentence."""
    window = text[-max(1, overlap_tokens * 2):]
    for index in range(len(window) - 1, -1, -1):
        if window[index] in "。！？!?；;":
            return window[index:].lstrip()
    return window.lstrip()


def _block_with_text(block: Block, *, text: str) -> Block:
    return Block(
        block_id=block.block_id,
        block_type=block.block_type,
        text=text,
        level=block.level,
        heading_path=list(block.heading_path),
        metadata=dict(block.metadata),
    )


def _paragraphs(text: str) -> List[str]:
    paragraphs: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append("\n".join(current).strip())
            current = []
    if current:
        paragraphs.append("\n".join(current).strip())
    return paragraphs


def _starts_new_topic(paragraph: str) -> bool:
    first_line = paragraph.strip().splitlines()[0].strip()
    return bool(
        _QUESTION_HEADING_RE.match(first_line)
        or _SECTION_HEADING_RE.match(first_line)
        or _NUMBERED_HEADING_RE.match(first_line)
    )


def _split_long_text(text: str, max_chars: int, overlap: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    start = 0
    step_back = min(max(overlap, 0), max_chars - 1)
    while start < len(text):
        end = start + max_chars
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - step_back)
    return chunks


def _hash_chunk(source_path: str, page_number: int | None, chunk_index: int, content: str) -> str:
    payload = f"{source_path}:{page_number}:{chunk_index}:{content}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
