"""多模态附件 P0：解析器与上传校验的纯单元测试（不连数据库）。"""
import io
import zipfile

import pytest

from multimodal import validation
from multimodal.document_processor import (
    DocxProcessor,
    PdfProcessor,
    TxtProcessor,
)
from multimodal.processors import ProcessorRegistry
from multimodal.service import AttachmentService
from multimodal.storage import LocalAttachmentStore
from webui_new.core.errors import BusinessError


# ── 解析器 ────────────────────────────────────────────────────────


def _docx_bytes() -> bytes:
    pytest.importorskip("docx")
    from docx import Document

    doc = Document()
    doc.add_heading("报销制度", level=1)
    doc.add_paragraph("住宿标准每天 500 元。")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "项", "金额"
    table.rows[1].cells[0].text, table.rows[1].cells[1].text = "住宿", "500"
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()

def test_txt_processor_decodes_utf8_and_gbk():
    proc = TxtProcessor()
    utf8 = proc.parse("差旅报销制度".encode("utf-8"), "policy.txt")
    gbk = proc.parse("差旅报销制度".encode("gbk"), "policy.txt")
    assert utf8.content_text == "差旅报销制度"
    assert gbk.content_text == "差旅报销制度"
    assert utf8.char_count == len("差旅报销制度")


def test_docx_processor_extracts_headings_paragraphs_and_tables():
    data = _docx_bytes()
    result = DocxProcessor().parse(data, "policy.docx")
    assert "报销制度" in result.content_text
    assert "住宿标准每天 500 元" in result.content_text
    # 表格被转成 Markdown 行
    assert "住宿" in result.content_text and "500" in result.content_text
    assert validation.detect_kind("policy.docx", data)[0] == "docx"


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


def test_docx_validation_rejects_malformed_and_unsafe_archives(monkeypatch):
    with pytest.raises(BusinessError):
        validation.detect_kind("broken.docx", b"PK\x03\x04not-a-zip")

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("[Content_Types].xml", "types")
        archive.writestr("word/document.xml", "document")
        archive.writestr("../outside.xml", "unsafe")
    with pytest.raises(BusinessError):
        validation.detect_kind("unsafe.docx", unsafe.getvalue())

    monkeypatch.setitem(validation.ATTACHMENT_CONFIG, "max_archive_uncompressed_bytes", 10)
    with pytest.raises(BusinessError):
        validation.detect_kind("large.docx", _docx_bytes())


def test_validate_size_and_count():
    validation.validate_size(1024)
    with pytest.raises(BusinessError):
        validation.validate_size(10 ** 12)  # 远超上限
    validation.validate_count(3)
    with pytest.raises(BusinessError):
        validation.validate_count(999)


# ── 私有存储与上传状态机 ──────────────────────────────────────────


def test_local_store_rejects_object_keys_outside_private_root(tmp_path):
    store = LocalAttachmentStore(str(tmp_path))
    key = store.save("u1", "att_1", b"hello")
    assert store.load(key) == b"hello"
    with pytest.raises(ValueError):
        store.load("../outside")
    with pytest.raises(FileExistsError):
        store.save("u1", "att_1", b"overwrite")


class _UploadRepository:
    def __init__(self, fail_create: bool = False):
        self.fail_create = fail_create
        self.attachment = None
        self.created_status = None
        self.extraction = None
        self.status_updates = []

    def create(self, attachment):
        if self.fail_create:
            raise RuntimeError("database unavailable")
        self.attachment = attachment
        self.created_status = attachment.status

    def complete_processing(self, extraction, user_id):
        self.extraction = extraction
        self.attachment.status = "ready"
        self.status_updates.append((user_id, "ready", None))

    def update_status(self, attachment_id, user_id, status, error_code=None):
        self.status_updates.append((user_id, status, error_code))

    def get_many(self, attachment_ids, user_id):
        if self.attachment and self.attachment.id in attachment_ids and self.attachment.user_id == user_id:
            return [self.attachment]
        return []

    def get_extraction(self, attachment_id):
        if self.extraction and self.extraction.attachment_id == attachment_id:
            return self.extraction
        return None


def test_upload_persists_processing_then_extraction_and_ready(tmp_path):
    repository = _UploadRepository()
    store = LocalAttachmentStore(str(tmp_path))
    service = AttachmentService(store=store, repository=repository)

    result = service.upload(
        user_id="u1",
        filename="policy.txt",
        content="hotel limit 500".encode(),
        request_id="request-1",
    )

    assert result.status == "ready"
    assert repository.created_status == "processing"
    assert repository.attachment.status == "ready"
    assert repository.attachment.request_id == "request-1"
    assert repository.attachment.expires_at
    assert repository.extraction.content_text == "hotel limit 500"
    assert repository.status_updates == [("u1", "ready", None)]
    assert store.load(repository.attachment.object_key) == b"hotel limit 500"


def test_docx_upload_and_question_preserve_correct_paragraph_only_in_agent_query(tmp_path):
    repository = _UploadRepository()
    service = AttachmentService(
        store=LocalAttachmentStore(str(tmp_path)),
        repository=repository,
    )
    uploaded = service.upload(
        user_id="u1",
        filename="policy.docx",
        content=_docx_bytes(),
    )

    normalized = service.normalize("请总结住宿限制", [uploaded.id], "u1")

    assert "住宿标准每天 500 元" in normalized.agent_query
    assert "住宿标准每天 500 元" not in normalized.display_message
    assert "policy.docx" in normalized.display_message


def test_upload_removes_private_object_when_metadata_create_fails(tmp_path):
    service = AttachmentService(
        store=LocalAttachmentStore(str(tmp_path)),
        repository=_UploadRepository(fail_create=True),
    )

    with pytest.raises(RuntimeError):
        service.upload(user_id="u1", filename="policy.txt", content=b"hello")

    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
