"""多模态附件 P0：解析器与上传校验的纯单元测试（不连数据库）。"""
import io

import pytest

from multimodal import validation
from multimodal.document_processor import (
    DocxProcessor,
    PdfProcessor,
    TxtProcessor,
)
from multimodal.processors import ProcessorRegistry
from webui_new.core.errors import BusinessError


# ── 解析器 ────────────────────────────────────────────────────────

def test_txt_processor_decodes_utf8_and_gbk():
    proc = TxtProcessor()
    utf8 = proc.parse("差旅报销制度".encode("utf-8"), "policy.txt")
    gbk = proc.parse("差旅报销制度".encode("gbk"), "policy.txt")
    assert utf8.content_text == "差旅报销制度"
    assert gbk.content_text == "差旅报销制度"
    assert utf8.char_count == len("差旅报销制度")


def test_docx_processor_extracts_headings_paragraphs_and_tables():
    pytest.importorskip("docx")
    from docx import Document

    doc = Document()
    doc.add_heading("报销制度", level=1)
    doc.add_paragraph("住宿标准每天 500 元。")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "项", "金额"
    table.rows[1].cells[0].text, table.rows[1].cells[1].text = "住宿", "500"
    buf = io.BytesIO()
    doc.save(buf)

    result = DocxProcessor().parse(buf.getvalue(), "policy.docx")
    assert "报销制度" in result.content_text
    assert "住宿标准每天 500 元" in result.content_text
    # 表格被转成 Markdown 行
    assert "住宿" in result.content_text and "500" in result.content_text


def test_pdf_processor_handles_blank_pdf_without_crashing():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()

    result = PdfProcessor().parse(data, "blank.pdf")
    assert result.page_count == 1
    # 空白页无文本
    assert result.content_text == ""


def test_processor_registry_dispatches_by_extension():
    reg = ProcessorRegistry()
    assert "docx" in reg.supported_extensions()
    result = reg.parse("txt", "hello".encode("utf-8"), "h.txt")
    assert result.content_text == "hello"
    with pytest.raises(ValueError):
        reg.parse("exe", b"x", "h.exe")


# ── 上传校验 ──────────────────────────────────────────────────────

def test_detect_kind_uses_magic_not_extension():
    ext, kind, mime = validation.detect_kind("policy.pdf", b"%PDF-1.4\n%binary")
    assert ext == "pdf" and kind == "document" and mime == "application/pdf"
    # 内容与扩展名不符
    with pytest.raises(BusinessError):
        validation.detect_kind("policy.pdf", b"not a pdf at all" + b"\x00" * 4)


def test_detect_kind_rejects_double_extension_and_unsupported():
    with pytest.raises(BusinessError):
        validation.detect_kind("invoice.pdf.exe", b"MZ")
    with pytest.raises(BusinessError):
        validation.detect_kind("archive.zip", b"PK\x03\x04" + b"\x00" * 10)


def test_detect_kind_rejects_path_traversal():
    with pytest.raises(BusinessError):
        validation.detect_kind("../evil.pdf", b"%PDF-1.4")


def test_validate_size_and_count():
    validation.validate_size(1024)
    with pytest.raises(BusinessError):
        validation.validate_size(10 ** 12)  # 远超上限
    validation.validate_count(3)
    with pytest.raises(BusinessError):
        validation.validate_count(999)
