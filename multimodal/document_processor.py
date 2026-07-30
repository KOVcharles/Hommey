"""Synchronous document parsers for chat attachments (TXT/MD/DOCX/PDF).

P0 仅文档类、请求内同步解析。复用 pypdf（与 rag/parser.py 同源）做文字型 PDF；
DOCX 走 python-docx 转 Markdown。依赖惰性导入，缺失时给清晰错误。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Optional

PARSER_VERSION = "document-p0-1"


@dataclass
class ParseResult:
    content_text: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    char_count: int = 0
    page_count: Optional[int] = None


def _decode_text(data: bytes) -> str:
    """UTF-8 优先、常见中文编码兜底；全部失败则忽略无法解码字符。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore").strip()


class DocumentProcessor:
    supported_extensions: tuple[str, ...] = ()

    def parse(self, data: bytes, filename: str) -> ParseResult:
        raise NotImplementedError


class TxtProcessor(DocumentProcessor):
    supported_extensions = ("txt", "md")

    def parse(self, data: bytes, filename: str) -> ParseResult:
        text = _decode_text(data)
        return ParseResult(
            content_text=text,
            structured={"filename": filename},
            char_count=len(text),
        )


class DocxProcessor(DocumentProcessor):
    supported_extensions = ("docx",)

    def parse(self, data: bytes, filename: str) -> ParseResult:
        try:
            from docx import Document  # type: ignore
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError("DOCX 解析需要 python-docx 依赖") from exc

        document = Document(io.BytesIO(data))
        lines: list[str] = []
        for para in document.paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            if "heading" in style:
                lines.append(f"## {text}")
            elif "list" in style:
                lines.append(f"- {text}")
            else:
                lines.append(text)
        for table in document.tables:
            lines.append("")
            for row in table.rows:
                cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                lines.append("| " + " | ".join(cells) + " |")
        text = "\n".join(lines).strip()
        return ParseResult(
            content_text=text,
            structured={"filename": filename},
            char_count=len(text),
        )


class PdfProcessor(DocumentProcessor):
    supported_extensions = ("pdf",)

    def parse(self, data: bytes, filename: str) -> ParseResult:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError("PDF 解析需要 pypdf 依赖") from exc

        reader = PdfReader(io.BytesIO(data))
        page_texts: list[tuple[int, str]] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                text = ""
            if text:
                page_texts.append((index, text))
        body = "\n\n".join(f"{{第 {idx} 页}}\n{t}" for idx, t in page_texts)
        return ParseResult(
            content_text=body,
            structured={"filename": filename, "pages": [idx for idx, _ in page_texts]},
            char_count=len(body),
            page_count=len(reader.pages),
        )
