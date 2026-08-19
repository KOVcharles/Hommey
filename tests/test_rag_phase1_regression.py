"""Phase 1 regression tests (audit §11 Phase 1 完成条件).

Each test maps to one Phase 1 completion criterion:

- 相同文件重复增量入库新增 0 块  → ``test_incremental_reingest_adds_zero_new_chunks``
- 没有 heading-only 证据块        → ``test_chunker_never_emits_heading_only_chunk``
- chunk_id 跨重建稳定            → ``test_chunk_id_stable_across_rebuilds``
- 所有原始页面有明确终态          → ``test_empty_page_has_intentionally_skipped_terminal_state``
- ``sources[].file`` 为真实 filename → ``test_chunk_metadata_emits_real_filename``
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from rag.block_parser import parse_text_blocks
from rag.chunker import BlockChunker, token_count
from rag.heading_rules import match_heading
from rag.schemas import (
    BLOCK_TYPE_HEADING,
    PAGE_STATE_INTENTIONALLY_SKIPPED,
    Block,
    DocumentChunk,
    ParsedDocument,
    RawDocument,
)
from rag.vector_store import InMemoryVectorStore


# ---- idempotency (P6) -------------------------------------------------------


def _chunk(content: str, *, document_id: str, document_version: str) -> DocumentChunk:
    chunk_hash = f"hash-{content}"
    return DocumentChunk(
        content=content,
        source_path=f"/docs/{document_id}",
        filename=document_id,
        file_type="txt",
        page_number=None,
        chunk_index=1,
        content_type="paragraph",
        hash=chunk_hash,
        chunk_id=f"{document_id}::{document_version}::c1::b1::01",
        chunk_hash=chunk_hash,
        chunk_ordinal=1,
        document_id=document_id,
        document_version=document_version,
        metadata={"document_id": document_id, "document_version": document_version},
    )


def test_incremental_reingest_adds_zero_new_chunks():
    store = InMemoryVectorStore()
    chunks = [_chunk("住宿标准 500 元", document_id="01_travel_standards.txt", document_version="v1")]

    first = store.add_chunks(chunks)
    second = store.add_chunks(chunks)

    assert first["added_count"] == 1
    assert second["added_count"] == 0
    assert second["total_count"] == 1


def test_content_change_adds_new_rows_under_same_document():
    store = InMemoryVectorStore()
    store.add_chunks([_chunk("住宿标准 500 元", document_id="policy.txt", document_version="v1")])

    changed = _chunk("住宿标准 600 元", document_id="policy.txt", document_version="v2")
    result = store.add_chunks([changed])

    # Content changed → different chunk_hash → new row; the superseded v1 rows
    # are then retired by document_id (audit §4.3: 先 hash 幂等，后按 doc_id 切换
    # 版本), so the store holds exactly the newest version.
    assert result["added_count"] == 1
    assert result["total_count"] == 1
    assert len(store.rows) == 1
    assert store.rows[0].document_version == "v2"


def test_repeated_document_version_changes_do_not_reuse_or_delete_live_ids():
    store = InMemoryVectorStore()
    store.add_chunks([
        _chunk("住宿标准 500 元", document_id="policy.txt", document_version="v1"),
        _chunk("机票经济舱", document_id="transport.txt", document_version="v1"),
    ])

    store.add_chunks([_chunk("住宿标准 600 元", document_id="policy.txt", document_version="v2")])
    store.add_chunks([_chunk("住宿标准 700 元", document_id="policy.txt", document_version="v3")])

    assert len(store.rows) == 2
    by_document = {row.document_id: row for row in store.rows}
    assert by_document["policy.txt"].document_version == "v3"
    assert by_document["transport.txt"].content == "机票经济舱"


# ---- chunker invariants (P2/P3/P6) -----------------------------------------


def _parsed_document(text: str, *, filename: str = "doc.txt") -> ParsedDocument:
    blocks = parse_text_blocks(text, page_number=None, file_type="txt")
    return ParsedDocument(
        text=text,
        source_path=f"/docs/{filename}",
        filename=filename,
        file_type="txt",
        page_number=None,
        metadata={"document_id": filename, "document_version": "v1"},
        blocks=blocks,
    )


def test_chunker_never_emits_heading_only_chunk():
    # A heading with content, then a dangling heading at the end of the file
    # (the classic "orphan heading" from the old flush-on-heading chunker).
    doc = _parsed_document("一、住宿标准\n\n每晚不超过500元\n\n二、交通标准")

    chunks = BlockChunker().chunk([doc])

    assert chunks, "expected at least one chunk"
    for chunk in chunks:
        assert "交通标准" not in chunk.heading_path or chunk.content.strip()
        assert chunk.content.strip(), f"heading-only chunk leaked: {chunk.chunk_id}"


def test_chunk_id_stable_across_rebuilds():
    doc = _parsed_document("一、住宿标准\n\n每晚不超过500元\n\n二、交通标准\n\n经济舱")

    first = BlockChunker().chunk([doc])
    second = BlockChunker().chunk([doc])

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert all(c.chunk_id for c in first)
    # Location segment is section-based for txt (c{n}), never page-based.
    assert any("::c" in c.chunk_id for c in first)


def test_chunk_id_includes_document_version_and_location():
    doc = _parsed_document("一、住宿标准\n\n每晚不超过500元")

    chunks = BlockChunker().chunk([doc])

    chunk_id = chunks[0].chunk_id
    assert "::v1::" in chunk_id
    assert chunks[0].chunk_hash
    assert chunks[0].document_id == "doc.txt"


def test_heading_path_attached_to_content_chunks():
    doc = _parsed_document("一、住宿标准\n\n每晚不超过500元")

    chunks = BlockChunker().chunk([doc])

    assert chunks[0].heading_path
    assert "住宿标准" in chunks[0].heading_path[0]


def _pdf_page(page_number: int, blocks) -> ParsedDocument:
    return ParsedDocument(
        text="\n\n".join(block.text for block in blocks),
        source_path="/docs/policy.pdf",
        filename="policy.pdf",
        file_type="pdf",
        page_number=page_number,
        metadata={"document_id": "policy.pdf", "document_version": "v1"},
        blocks=blocks,
    )


def test_decimal_amount_line_is_not_a_heading():
    # A decimal amount or version line at the start of a line must never be
    # misparsed as a numbered heading ("12.5 元" → heading "5 元").
    assert match_heading("12.5 元的早餐补贴标准", "txt") is None
    assert match_heading("1.5倍 交通补贴", "txt") is None
    assert match_heading("3.14 倍系数", "txt") is None
    assert match_heading("一.5 元", "txt") is None
    # Real numbered headings still match.
    assert match_heading("1. 交通标准", "txt") == (3, "交通标准")
    assert match_heading("1、交通标准", "txt") == (3, "交通标准")
    assert match_heading("一、住宿标准", "txt") == (1, "住宿标准")


def test_setext_underline_is_a_heading_only_in_markdown():
    # "注意事项\n----" is a section separator in TXT/PDF, not a setext heading.
    txt_blocks = parse_text_blocks("注意事项\n----", page_number=None, file_type="txt")
    assert all(block.block_type != BLOCK_TYPE_HEADING for block in txt_blocks)

    md_blocks = parse_text_blocks("注意事项\n====", page_number=None, file_type="md")
    assert any(
        block.block_type == BLOCK_TYPE_HEADING and block.text == "注意事项"
        for block in md_blocks
    )


def test_all_heading_document_emits_no_chunks():
    doc = _parsed_document("一、差旅申请流程\n二、交通标准\n三、住宿标准")

    chunks = BlockChunker().chunk([doc])

    assert chunks == []


def test_overlong_paragraph_under_heading_is_sentence_split():
    body = "差旅费用报销标准如下。" + ("具体金额以公司最新规定为准。" * 40)
    doc = _parsed_document("一、交通标准\n" + body)

    chunks = BlockChunker().chunk([doc])

    assert len(chunks) > 1
    for chunk in chunks:
        assert token_count(chunk.content) <= 400, f"oversized chunk: {chunk.chunk_id}"
    assert "交通标准" in chunks[0].content


def test_pdf_section_heading_carries_across_pages():
    page3 = parse_text_blocks("一、住宿标准\n每晚不超过500元", page_number=3, file_type="pdf")
    page4 = parse_text_blocks("国际出差标准另行规定", page_number=4, file_type="pdf")

    chunks = BlockChunker().chunk([_pdf_page(3, page3), _pdf_page(4, page4)])

    by_page = {chunk.page_number: chunk for chunk in chunks}
    assert 3 in by_page and 4 in by_page
    assert "住宿标准" in by_page[3].heading_path[0]
    # The paragraph on page 4 continues the section opened on page 3.
    assert by_page[4].heading_path == ["住宿标准"]


def test_inmemory_store_dedups_and_retires_versions():
    store = InMemoryVectorStore()
    v1 = _chunk("住宿标准 500 元", document_id="policy.txt", document_version="v1")

    # Duplicates within one batch are deduplicated (audit P6).
    store.add_chunks([v1, v1])
    assert store.stats()["total_documents"] == 1

    changed = _chunk("住宿标准 600 元", document_id="policy.txt", document_version="v2")
    store.add_chunks([changed])

    assert store.stats()["total_documents"] == 1
    assert store.rows[0].document_version == "v2"


# ---- page terminal states (P8) ---------------------------------------------


def test_empty_page_has_intentionally_skipped_terminal_state(monkeypatch):
    pdf = SimpleNamespace(source_path="/docs/scan.pdf", filename="scan.pdf", file_type="pdf", metadata={})
    fake_reader = SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "  ")])
    monkeypatch.setitem(
        __import__("sys").modules,
        "pypdf",
        SimpleNamespace(PdfReader=lambda path: fake_reader),
    )

    from rag.parser import PdfTextParser

    parsed = PdfTextParser().parse(pdf)

    assert len(parsed) == 1
    assert parsed[0].page_terminal_state == PAGE_STATE_INTENTIONALLY_SKIPPED


# ---- source citation identity (§4.12 / §6.1.6) -----------------------------


def test_chunk_metadata_emits_real_filename():
    chunk = _chunk("住宿标准", document_id="01_travel_standards.txt", document_version="v1")
    metadata = chunk.to_metadata()

    assert metadata["filename"] == "01_travel_standards.txt"
    assert metadata["source_path"] == "/docs/01_travel_standards.txt"
    assert metadata["source"] == "/docs/01_travel_standards.txt"
    assert metadata["file_path"] == "/docs/01_travel_standards.txt"
    assert metadata["chunk_id"]
