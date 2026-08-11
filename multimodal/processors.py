"""Processor registry: dispatch by file extension (P0 + P1-A image)."""
from __future__ import annotations

from .document_processor import (
    DocxProcessor,
    DocumentProcessor,
    ParseResult,
    PdfProcessor,
    PARSER_VERSION,
    TxtProcessor,
)
from .image_processor import ImageProcessor


class ProcessorRegistry:
    def __init__(self, processors: list[DocumentProcessor] | None = None):
        self._by_ext: dict[str, DocumentProcessor] = {}
        for processor in processors or [TxtProcessor(), DocxProcessor(), PdfProcessor(), ImageProcessor()]:
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

    def parser_version_for(self, ext: str) -> str:
        """返回该扩展名对应处理器的提取契约版本（未注册时回退全局默认）。"""
        processor = self._by_ext.get((ext or "").lower())
        if processor is None:
            return PARSER_VERSION
        return getattr(processor, "parser_version", PARSER_VERSION)
