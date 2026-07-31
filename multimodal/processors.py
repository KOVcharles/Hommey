"""Processor registry: dispatch by file extension (P0)."""
from __future__ import annotations

from .document_processor import (
    DocxProcessor,
    DocumentProcessor,
    ParseResult,
    PdfProcessor,
    TxtProcessor,
)


class ProcessorRegistry:
    def __init__(self, processors: list[DocumentProcessor] | None = None):
        self._by_ext: dict[str, DocumentProcessor] = {}
        for processor in processors or [TxtProcessor(), DocxProcessor(), PdfProcessor()]:
            self.register(processor)

    def register(self, processor: DocumentProcessor) -> None:
        for ext in processor.supported_extensions:
            self._by_ext[ext.lower()] = processor

    def supported_extensions(self) -> set[str]:
        return set(self._by_ext.keys())

    def parse(self, ext: str, data: bytes, filename: str) -> ParseResult:
        processor = self._by_ext.get((ext or "").lower())
        if processor is None:
            raise ValueError(f"No processor registered for extension '.{ext}'")
        return processor.parse(data, filename)
