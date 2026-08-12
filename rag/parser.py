"""Document parser interfaces and txt/pdf implementations.

Phase-1 parsers produce the audit's minimal block model (§6): every page gets
a list of structured ``Block`` objects and a page terminal state (§7 原则 P8).
The legacy ``ParsedDocument.text`` field is kept as the rendered result of
``blocks`` so older callers keep working.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from .block_parser import parse_text_blocks
from .document_loader import infer_category
from .encodings import decode_text_bytes
from .heading_rules import match_heading
from .schemas import (
    PARSER_NAMES,
    PARSER_VERSIONS,
    PAGE_STATE_ERROR,
    PAGE_STATE_INTENTIONALLY_SKIPPED,
    PAGE_STATE_NATIVE_TEXT,
    ParsedDocument,
    RawDocument,
)

_ATX_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")


class UnsupportedFileTypeError(ValueError):
    def __init__(self, file_type: str, source_path: str):
        super().__init__(f"Unsupported RAG document type '{file_type}' for file: {source_path}")
        self.file_type = file_type
        self.source_path = source_path


class DocumentParser(ABC):
    supported_file_types: tuple[str, ...] = ()
    parser_name: str = ""
    parser_version: str = ""

    @abstractmethod
    def parse(self, document: RawDocument) -> List[ParsedDocument]:
        raise NotImplementedError


def _derive_title(text: str, filename: str, file_type: str) -> str:
    """Document-level title: first H1, or first short title-like line for text.

    PDF pages never derive their title from the page's first line (audit §6.1.2
    “不再取页首行”); PDFs use the filename stem.
    """
    if file_type == "pdf":
        return Path(filename).stem
    for line in (text.splitlines() or []):
        stripped = line.strip()
        if not stripped:
            continue
        if file_type == "md":
            atx = _ATX_H1_RE.match(stripped)
            if atx:
                return atx.group(1).strip()
        heading = match_heading(stripped, file_type)
        if heading is not None:
            return heading[1]
        # A bare title line: short, no sentence-ending punctuation.
        if len(stripped) <= 60 and not re.search(r"[。！？;；!?]", stripped):
            return stripped
        break
    return Path(filename).stem


class TxtParser(DocumentParser):
    supported_file_types = ("txt", "md")
    parser_name = PARSER_NAMES["txt"]
    parser_version = PARSER_VERSIONS["txt"]

    def parse(self, document: RawDocument) -> List[ParsedDocument]:
        text = decode_text_bytes(document.content)
        if not text:
            return []
        file_type = document.file_type.lower()
        # Non-paginated TXT/MD blocks carry bare `b{seq}` ids (audit §6.1.2):
        # no fake page anchor, since the chunk location is section-based.
        blocks = parse_text_blocks(text, page_number=None, file_type=file_type)
        return [
            ParsedDocument(
                text=_render_blocks(blocks, text),
                source_path=document.source_path,
                filename=document.filename,
                file_type=document.file_type,
                page_number=None,
                title=_derive_title(text, document.filename, file_type),
                category=infer_category(Path(document.source_path)),
                metadata=dict(document.metadata),
                blocks=blocks,
                page_terminal_state=PAGE_STATE_NATIVE_TEXT,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
            )
        ]


class PdfTextParser(DocumentParser):
    supported_file_types = ("pdf",)
    parser_name = PARSER_NAMES["pdf"]
    parser_version = PARSER_VERSIONS["pdf"]

    def parse(self, document: RawDocument) -> List[ParsedDocument]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF ingestion requires the optional 'pypdf' package.") from exc

        parsed: List[ParsedDocument] = []
        reader = PdfReader(document.source_path)
        doc_title = _derive_title("", document.filename, "pdf")
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as exc:
                parsed.append(
                    ParsedDocument(
                        text="",
                        source_path=document.source_path,
                        filename=document.filename,
                        file_type=document.file_type,
                        page_number=index,
                        title=doc_title,
                        category=infer_category(Path(document.source_path)),
                        metadata={**document.metadata, "parse_error": str(exc)},
                        blocks=[],
                        page_terminal_state=PAGE_STATE_ERROR,
                        parser_name=self.parser_name,
                        parser_version=self.parser_version,
                    )
                )
                continue
            if not text:
                # No text layer and no OCR in this phase: the page gets an
                # explicit terminal state instead of silently vanishing (§4.1).
                parsed.append(
                    ParsedDocument(
                        text="",
                        source_path=document.source_path,
                        filename=document.filename,
                        file_type=document.file_type,
                        page_number=index,
                        title=doc_title,
                        category=infer_category(Path(document.source_path)),
                        metadata=dict(document.metadata),
                        blocks=[],
                        page_terminal_state=PAGE_STATE_INTENTIONALLY_SKIPPED,
                        parser_name=self.parser_name,
                        parser_version=self.parser_version,
                    )
                )
                continue
            blocks = parse_text_blocks(text, page_number=index, file_type="pdf")
            parsed.append(
                ParsedDocument(
                    text=_render_blocks(blocks, text),
                    source_path=document.source_path,
                    filename=document.filename,
                    file_type=document.file_type,
                    page_number=index,
                    title=doc_title,
                    category=infer_category(Path(document.source_path)),
                    metadata=dict(document.metadata),
                    blocks=blocks,
                    page_terminal_state=PAGE_STATE_NATIVE_TEXT,
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                )
            )
        return parsed


def _render_blocks(blocks: List[object], fallback: str) -> str:
    """Render blocks back to text for the legacy ``ParsedDocument.text`` field."""
    if not blocks:
        return fallback
    texts = [getattr(block, "text", "") for block in blocks if getattr(block, "text", "")]
    return "\n\n".join(texts).strip()


class ParserRegistry:
    def __init__(self, parsers: List[DocumentParser] | None = None):
        self.parsers: Dict[str, DocumentParser] = {}
        for parser in parsers or self._default_parsers():
            self.register(parser)

    @staticmethod
    def _default_parsers() -> List[DocumentParser]:
        """Phase 2: register the DOCX/CSV/XLSX structured parsers (audit §11).

        Kept lazy so importing ``rag.parser`` never pulls python-docx/openpyxl
        (those are imported inside the parse methods, on first use only).
        """
        from .structured_parser import CsvParser, DocxParser, XlsxParser

        return [TxtParser(), PdfTextParser(), DocxParser(), CsvParser(), XlsxParser()]

    def register(self, parser: DocumentParser) -> None:
        for file_type in parser.supported_file_types:
            self.parsers[file_type.lower()] = parser

    def parse(self, document: RawDocument) -> List[ParsedDocument]:
        parser = self.parsers.get(document.file_type.lower())
        if not parser:
            raise UnsupportedFileTypeError(document.file_type, document.source_path)
        return parser.parse(document)

    def parser_version_for(self, file_type: str) -> str:
        parser = self.parsers.get(file_type.lower())
        return getattr(parser, "parser_version", "") if parser else ""


# TODO: Add OCR, table extraction, image caption, and multimodal parsers behind
# this same DocumentParser interface when those dependencies are intentionally added.
