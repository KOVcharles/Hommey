"""Phase 3 regression tests (audit §11 Phase 3: OCR 与版面解析, 轻量交付).

Scope delivered: native-text fast path stays first; text-less PDF pages fall
back to the shared DocumentOcrClient (flag-gated), and every outcome lands in an
explicit terminal state (P8) — nothing vanishes silently.  No PaddleOCR/Docling
PoC; the standalone worker/task-table is left as an extension point.

- OCR 关闭 → 页面原样通过        → ``test_disabled_passes_pages_through``
- OCR 成功且高置信度 → ocr_text  → ``test_ocr_success_marks_page_ocr_text``
- OCR 低置信度 → 记录跳过原因    → ``test_low_confidence_page_skipped_with_reason``
- OCR 调用失败 → error + 原因    → ``test_vision_failure_marks_page_error``
- 页面渲染失败 → error + 原因    → ``test_render_failure_marks_page_error``
- 原生文本页不做 OCR            → ``test_native_text_page_untouched``
- 指纹随 OCR 开关变化            → ``test_ocr_flag_changes_index_fingerprint``
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag.ocr import PageOcrFallback
from rag.schemas import (
    PAGE_STATE_ERROR,
    PAGE_STATE_INTENTIONALLY_SKIPPED,
    PAGE_STATE_NATIVE_TEXT,
    PAGE_STATE_OCR_TEXT,
    ParsedDocument,
)


class _FakeOcr:
    """Minimal fake for the shared document OCR client."""

    def __init__(self, ocr="第一行 | 第二行", confidence=0.9, error=None):
        self.ocr = ocr
        self.confidence = confidence
        self.error = error

    def ocr_page(self, data, ext, *, filename, page_number):
        if self.error:
            raise self.error
        return SimpleNamespace(
            text=self.ocr,
            model="fake-ocr",
            confidence=self.confidence,
        )


def _skipped_pdf_page(**overrides) -> ParsedDocument:
    base = ParsedDocument(
        text="",
        source_path="/tmp/scanned.pdf",
        filename="scanned.pdf",
        file_type="pdf",
        page_number=1,
        title="scanned",
        category="business_travel",
        metadata={"document_version": "v1"},
        blocks=[],
        page_terminal_state=PAGE_STATE_INTENTIONALLY_SKIPPED,
        parser_name="pdf_text",
        parser_version="pdf-text-block-v2",
    )
    return base if not overrides else ParsedDocument(**{**base.__dict__, **overrides})


def _apply(fallback, page):
    return fallback.apply([page])[0]


def test_disabled_passes_pages_through(monkeypatch):
    page = _skipped_pdf_page()
    fallback = PageOcrFallback(enabled=False, ocr_client=_FakeOcr())
    result = _apply(fallback, page)
    assert result is page  # untouched object, no OCR attempted


def test_ocr_success_marks_page_ocr_text(monkeypatch):
    monkeypatch.setattr("rag.ocr._render_page_png", lambda *_: b"png-bytes")
    page = _skipped_pdf_page()
    fallback = PageOcrFallback(enabled=True, confidence_threshold=0.5, ocr_client=_FakeOcr())
    result = _apply(fallback, page)
    assert result.page_terminal_state == PAGE_STATE_OCR_TEXT
    assert result.blocks
    assert "第一行" in result.text
    assert "第二行" in result.text
    assert result.metadata["ocr_confidence"] == 0.9
    assert result.metadata["ocr_source"] == "document_ocr"
    assert result.metadata["ocr_model"] == "fake-ocr"


def test_low_confidence_page_skipped_with_reason(monkeypatch):
    monkeypatch.setattr("rag.ocr._render_page_png", lambda *_: b"png-bytes")
    page = _skipped_pdf_page()
    fallback = PageOcrFallback(enabled=True, confidence_threshold=0.8, ocr_client=_FakeOcr(confidence=0.4))
    result = _apply(fallback, page)
    assert result.page_terminal_state == PAGE_STATE_INTENTIONALLY_SKIPPED
    assert result.metadata["ocr_skipped_reason"] == "low_confidence"


def test_vision_failure_marks_page_error(monkeypatch):
    monkeypatch.setattr("rag.ocr._render_page_png", lambda *_: b"png-bytes")
    page = _skipped_pdf_page()
    fallback = PageOcrFallback(
        enabled=True,
        ocr_client=_FakeOcr(error=RuntimeError("ocr api down")),
    )
    result = _apply(fallback, page)
    assert result.page_terminal_state == PAGE_STATE_ERROR
    assert "ocr api down" in result.metadata["ocr_error"]


def test_render_failure_marks_page_error(monkeypatch):
    def _boom(*_):
        raise ValueError("cannot render")

    monkeypatch.setattr("rag.ocr._render_page_png", _boom)
    page = _skipped_pdf_page()
    fallback = PageOcrFallback(enabled=True, ocr_client=_FakeOcr())
    result = _apply(fallback, page)
    assert result.page_terminal_state == PAGE_STATE_ERROR
    assert "render_failed" in result.metadata["ocr_error"]


def test_native_text_page_untouched(monkeypatch):
    monkeypatch.setattr("rag.ocr._render_page_png", lambda *_: (_ for _ in ()).throw(AssertionError("must not render")))
    page = _skipped_pdf_page(
        text="有原生文本的页面",
        page_terminal_state=PAGE_STATE_NATIVE_TEXT,
    )
    fallback = PageOcrFallback(enabled=True, ocr_client=_FakeOcr())
    result = _apply(fallback, page)
    assert result.page_terminal_state == PAGE_STATE_NATIVE_TEXT
    assert result.text == "有原生文本的页面"


def test_ocr_flag_changes_index_fingerprint():
    from rag.versions import compute_index_fingerprint

    common = dict(
        embedding_model="m",
        embedding_dimension=1024,
        embedding_backend="b",
        chunk_min_tokens=150,
        chunk_max_tokens=400,
        chunk_overlap_tokens=60,
    )
    off = compute_index_fingerprint(**common, ocr_enabled=False)
    on = compute_index_fingerprint(**common, ocr_enabled=True)
    assert off != on
    assert off == compute_index_fingerprint(
        **common,
        ocr_enabled=False,
        ocr_model="unused-while-disabled",
    )
    # Threshold changes are also part of the fingerprint.
    assert on != compute_index_fingerprint(**common, ocr_enabled=True, ocr_confidence_threshold=0.9)
    assert on != compute_index_fingerprint(
        **common,
        ocr_enabled=True,
        ocr_model="deepseek-ai/DeepSeek-OCR",
    )


# ---- pipeline integration ---------------------------------------------------


def test_pipeline_ocr_recovers_scanned_pdf(tmp_path):
    """A scanned (image-only) PDF is OCR'd end-to-end and becomes retrievable."""
    import fitz

    pdf_path = tmp_path / "扫描件.pdf"
    document = fitz.open()
    page = document.new_page()
    # A text-free page: a black bar only — pypdf extracts no text layer.
    page.draw_rect(fitz.Rect(50, 50, 300, 80), color=(0, 0, 0), fill=(0, 0, 0))
    document.save(str(pdf_path))
    document.close()

    from rag.config import RAGPipelineConfig
    from rag.ocr import PageOcrFallback
    from rag.pipeline import RAGPipeline
    from rag.vector_store import InMemoryVectorStore

    ocr = _FakeOcr(ocr="差旅报销标准：市内交通实报实销", confidence=0.95)
    config = RAGPipelineConfig(
        ocr_enabled=True,
        ocr_confidence_threshold=0.5,
        documents_dir=str(tmp_path),
    )
    store = InMemoryVectorStore()
    pipeline = RAGPipeline(
        config=config,
        vector_store=store,
        ocr_fallback=PageOcrFallback(
                enabled=True, confidence_threshold=0.5, ocr_client=ocr
        ),
    )
    try:
        report = pipeline.ingest(pdf_path, rebuild=True)
    finally:
        pipeline.close()

    assert report.status == "success"
    assert report.pages_parsed == 1
    assert report.chunks_loaded >= 1
    assert any("差旅报销标准" in chunk.content for chunk in store.rows)

    # Same ingest with OCR disabled leaves the scanned page skipped and empty.
    # The all-or-nothing refresh semantics then correctly report error (a full
    # rebuild that produced nothing retrievable is a failed refresh, not a
    # silent no-op), and zero chunks are written.
    store2 = InMemoryVectorStore()
    pipeline2 = RAGPipeline(
        config=RAGPipelineConfig(documents_dir=str(tmp_path)),
        vector_store=store2,
    )
    try:
        report2 = pipeline2.ingest(pdf_path, rebuild=True)
    finally:
        pipeline2.close()
    assert report2.status == "error"
    assert report2.pages_parsed == 1
    assert report2.chunks_loaded == 0
    assert store2.rows == []
