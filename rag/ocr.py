"""Flag-gated OCR fallback for text-less PDF pages (audit §11 Phase 3).

Phase 3's lightweight deliverable: keep the native-text fast path as the
parser's first choice, and when a PDF page has no extractable text layer, use
the shared document-focused ``DocumentOcrClient`` to OCR the rendered page.
No page silently vanishes (P8): every outcome lands in an explicit terminal state

- OCR success (and, when supplied, above the confidence threshold) → ``ocr_text``
- OCR success below the threshold              → ``intentionally_skipped`` with
                                                ``ocr_skipped_reason``
- OCR call or page-render failure               → ``error`` with ``ocr_error``

The standalone document worker + task state table the audit sketches is left
as an extension point; the fallback is small enough to live inline in the
pipeline behind ``ocr_enabled``.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, List, Optional

from .block_parser import parse_text_blocks
from .parser import _render_blocks
from .schemas import (
    PAGE_STATE_ERROR,
    PAGE_STATE_INTENTIONALLY_SKIPPED,
    PAGE_STATE_OCR_TEXT,
    ParsedDocument,
)

logger = logging.getLogger(__name__)


class PageOcrFallback:
    """Re-parse text-less PDF pages through the vision model, when enabled."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        confidence_threshold: float = 0.5,
        ocr_client: Any = None,
    ):
        self.enabled = bool(enabled)
        self.confidence_threshold = float(confidence_threshold)
        # Injected OCR client (tests pass a fake); created lazily on first
        # OCR so importing this module never touches the network.
        self._ocr_client = ocr_client

    def _client(self) -> Any:
        if self._ocr_client is None:
            from multimodal.document_ocr_client import DocumentOcrClient

            self._ocr_client = DocumentOcrClient()
        return self._ocr_client

    def apply(self, documents: List[ParsedDocument]) -> List[ParsedDocument]:
        """Replace text-less PDF pages with OCR'd pages; others pass through."""
        if not self.enabled:
            return documents
        return [self._maybe_ocr(document) for document in documents]

    def _maybe_ocr(self, document: ParsedDocument) -> ParsedDocument:
        if document.file_type != "pdf" or document.page_number is None:
            return document
        if document.page_terminal_state not in (
            PAGE_STATE_INTENTIONALLY_SKIPPED,
            PAGE_STATE_ERROR,
        ):
            return document
        # A page that already produced text (even partially) is a native-text
        # page; OCR is the fallback for genuinely text-less pages only.
        if document.text.strip():
            return document

        page = document.page_number
        try:
            image_bytes = _render_page_png(document.source_path, page)
        except Exception as exc:  # noqa: BLE001 — surface any render failure.
            logger.exception("Failed to render PDF page %d for OCR", page)
            return replace(
                document,
                page_terminal_state=PAGE_STATE_ERROR,
                metadata={**document.metadata, "ocr_error": f"render_failed: {exc}"},
            )

        try:
            result = self._client().ocr_page(
                image_bytes,
                "png",
                filename=document.filename,
                page_number=page,
            )
        except Exception as exc:  # noqa: BLE001 — VisionError or transport.
            logger.warning("OCR failed for %s page %d: %s", document.filename, page, exc)
            return replace(
                document,
                page_terminal_state=PAGE_STATE_ERROR,
                metadata={**document.metadata, "ocr_error": str(exc)},
            )

        ocr_text = _ocr_text(result)
        raw_confidence = getattr(result, "confidence", None)
        confidence = float(raw_confidence) if raw_confidence is not None else None
        base_metadata = {
            **document.metadata,
            "ocr_source": "document_ocr",
        }
        model = getattr(result, "model", None)
        if model:
            base_metadata["ocr_model"] = str(model)
        if confidence is not None:
            base_metadata["ocr_confidence"] = round(confidence, 3)
        if not ocr_text.strip():
            return replace(
                document,
                page_terminal_state=PAGE_STATE_INTENTIONALLY_SKIPPED,
                metadata={**base_metadata, "ocr_skipped_reason": "empty_ocr"},
            )
        if confidence is not None and confidence < self.confidence_threshold:
            return replace(
                document,
                page_terminal_state=PAGE_STATE_INTENTIONALLY_SKIPPED,
                metadata={**base_metadata, "ocr_skipped_reason": "low_confidence"},
            )

        blocks = parse_text_blocks(ocr_text, page_number=page, file_type="pdf")
        return replace(
            document,
            text=_render_blocks(blocks, ocr_text),
            blocks=blocks,
            page_terminal_state=PAGE_STATE_OCR_TEXT,
            metadata=base_metadata,
        )


def _render_page_png(source_path: str, page_number: int) -> bytes:
    """Rasterize PDF ``page_number`` (1-based) to PNG bytes via pymupdf."""
    import pymupdf

    with pymupdf.open(source_path) as pdf:
        if not 1 <= page_number <= pdf.page_count:
            raise IndexError(f"PDF page {page_number} out of range")
        pix = pdf.load_page(page_number - 1).get_pixmap(
            matrix=pymupdf.Matrix(2, 2), alpha=False
        )
        return pix.tobytes("png")


def _ocr_text(result: Any) -> str:
    """Extract OCR Markdown, with compatibility for legacy VisionResult fakes."""
    text = getattr(result, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    structured = getattr(result, "structured", None)
    if isinstance(structured, dict):
        raw = structured.get("ocr")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return getattr(result, "content_text", "") or ""
