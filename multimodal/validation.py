"""Upload validation: magic-number typing + quota enforcement (P0).

类型由文件魔数（而非扩展名）判定；拒绝双扩展名、路径穿越、内容与声明类型不符、
超大小/超数量。所有失败统一抛 BusinessError(400)。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import PurePosixPath

from settings import ATTACHMENT_CONFIG
from webui_new.core.errors import BusinessError

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"

_MIME_BY_EXT = {
    "txt": "text/plain",
    "md": "text/markdown",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_KIND_BY_EXT = {ext: "document" for ext in _MIME_BY_EXT}


def detect_kind(filename: str, data: bytes) -> tuple[str, str, str]:
    """按魔数判定类型，返回 (ext, kind, mime)。不符则抛 BusinessError。"""
    name = (filename or "").strip().lower().lstrip(".")
    if not name or "/" in name or "\\" in name:
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件名不合法", status_code=400)
    # 单一扩展名：拒绝 a.pdf.exe 这类双扩展名伪装。
    if name.count(".") != 1:
        raise BusinessError(
            "UNSUPPORTED_FILE_TYPE",
            "请使用单一扩展名的文件（如 .docx / .pdf / .txt）",
            status_code=400,
        )
    base, ext = name.split(".")
    allowed_extensions = set(ATTACHMENT_CONFIG["allowed_extensions"])
    if not base or ext not in _KIND_BY_EXT or ext not in allowed_extensions:
        allowed = "、".join(f".{e}" for e in ATTACHMENT_CONFIG["allowed_extensions"])
        raise BusinessError("UNSUPPORTED_FILE_TYPE", f"暂不支持该类型，仅支持 {allowed}", status_code=400)

    if ext == "pdf" and not data.startswith(_PDF_MAGIC):
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件内容与 PDF 格式不符", status_code=400)
    if ext == "docx" and not data.startswith(_ZIP_MAGIC):
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件内容与 DOCX 格式不符", status_code=400)
    if ext == "docx":
        _validate_docx_archive(data)
    if ext in ("txt", "md") and not _looks_like_text(data):
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件内容包含非法二进制字符", status_code=400)

    return ext, _KIND_BY_EXT[ext], _MIME_BY_EXT[ext]


def _validate_docx_archive(data: bytes) -> None:
    """Reject malformed, encrypted, or unsafe DOCX ZIP containers before parsing."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError):
        raise BusinessError("INVALID_DOCX", "DOCX 文件已损坏或格式不正确", status_code=400)

    if len(entries) > int(ATTACHMENT_CONFIG["max_archive_entries"]):
        raise BusinessError("UNSAFE_DOCX", "DOCX 文件内部条目过多", status_code=400)

    total_size = 0
    compressed_size = 0
    names: set[str] = set()
    for entry in entries:
        normalized_name = entry.filename.replace("\\", "/")
        path = PurePosixPath(normalized_name)
        if path.is_absolute() or ".." in path.parts:
            raise BusinessError("UNSAFE_DOCX", "DOCX 文件包含非法内部路径", status_code=400)
        if entry.flag_bits & 0x1:
            raise BusinessError("ENCRYPTED_DOCX", "暂不支持加密 DOCX 文件", status_code=400)
        names.add(normalized_name)
        total_size += max(int(entry.file_size), 0)
        compressed_size += max(int(entry.compress_size), 0)

    if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
        raise BusinessError("INVALID_DOCX", "文件不是有效的 DOCX 文档", status_code=400)
    if total_size > int(ATTACHMENT_CONFIG["max_archive_uncompressed_bytes"]):
        raise BusinessError("UNSAFE_DOCX", "DOCX 解压后体积超过限制", status_code=400)

    # Small XML files naturally compress well; enforce the ratio only above 1 MB.
    ratio = total_size / max(compressed_size, 1)
    if total_size > 1024 * 1024 and ratio > int(ATTACHMENT_CONFIG["max_archive_ratio"]):
        raise BusinessError("UNSAFE_DOCX", "DOCX 压缩比异常，已拒绝处理", status_code=400)


def _looks_like_text(data: bytes) -> bool:
    """拒绝含 NUL 字节或高比例控制字符的二进制内容。"""
    if not data:
        return True
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    nontext = sum(1 for b in sample if (b < 9 or 13 < b < 32))
    return nontext / len(sample) < 0.30


def validate_size(size_bytes: int) -> None:
    max_bytes = int(ATTACHMENT_CONFIG["max_size_bytes"])
    if size_bytes > max_bytes:
        raise BusinessError(
            "ATTACHMENT_TOO_LARGE",
            f"单个文件超过 {max_bytes // (1024 * 1024)} MB 限制",
            status_code=400,
        )


def validate_count(count: int) -> None:
    max_n = int(ATTACHMENT_CONFIG["max_per_message"])
    if count > max_n:
        raise BusinessError("TOO_MANY_ATTACHMENTS", f"单条消息最多 {max_n} 个附件", status_code=400)
