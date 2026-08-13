"""Flag-gated OCR fallback for text-less PDF pages (audit §11 Phase 3).

Phase 3's lightweight deliverable: keep the native-text fast path as the
parser's first choice, and when a PDF page has no extractable text layer,
reuse the multimodal ``VisionClient`` (Qwen2.5-VL) — the same visual front-end
the chat's image-attachment path uses — to OCR the rendered page.  No page
silently vanishes (P8): every outcome lands in an explicit terminal state

- OCR success above the confidence threshold  → ``ocr_text``
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
        vision_client: Any = None,
    ):
        self.enabled = bool(enabled)
        self.confidence_threshold = float(confidence_threshold)
        # Injected vision client (tests pass a fake); created lazily on first
        # OCR so importing this module never touches the network.
        self._vision = vision_client

    def _client(self) -> Any:
        if self._vision is None:
            from multimodal.vision_client import VisionClient

            self._vision = VisionClient()
        return self._vision

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
            result = self._client().describe_image(image_bytes, "png", document.filename)
        except Exception as exc:  # noqa: BLE001 — VisionError or transport.
            logger.warning("OCR failed for %s page %d: %s", document.filename, page, exc)
            return replace(
                document,
                page_terminal_state=PAGE_STATE_ERROR,
                metadata={**document.metadata, "ocr_error": str(exc)},
            )

        ocr_text = _ocr_text(result)
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        base_metadata = {
            **document.metadata,
            "ocr_source": "vision",
            "ocr_confidence": round(confidence, 3),
        }
        if not ocr_text.strip():
            return replace(
                document,
                page_terminal_state=PAGE_STATE_INTENTIONALLY_SKIPPED,
                metadata={**base_metadata, "ocr_skipped_reason": "empty_ocr"},
            )
        if confidence < self.confidence_threshold:
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
    import fitz  # PyMuPDF

    with fitz.open(source_path) as pdf:
        if not 1 <= page_number <= pdf.page_count:
            raise IndexError(f"PDF page {page_number} out of range")
        pix = pdf.load_page(page_number - 1).get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        return pix.tobytes("png")


def _ocr_text(result: Any) -> str:
    """Extract the raw OCR lines from a VisionResult.

    The vision prompt emits OCR text as ``line1 | line2 | …``; prefer that raw
    reading over the wrapper ``content_text`` (which prefixes filename and
    description) so the page blocks stay clean for retrieval.
    """
    structured = getattr(result, "structured", None)
    if isinstance(structured, dict):
        raw = structured.get("ocr")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return getattr(result, "content_text", "") or ""
