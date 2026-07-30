"""Upload validation: magic-number typing + quota enforcement (P0).

类型由文件魔数（而非扩展名）判定；拒绝双扩展名、路径穿越、内容与声明类型不符、
超大小/超数量。所有失败统一抛 BusinessError(400)。
"""
from __future__ import annotations

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
    if not base or ext not in _KIND_BY_EXT:
        allowed = "、".join(f".{e}" for e in ATTACHMENT_CONFIG["allowed_extensions"])
        raise BusinessError("UNSUPPORTED_FILE_TYPE", f"暂不支持该类型，仅支持 {allowed}", status_code=400)

    if ext == "pdf" and not data.startswith(_PDF_MAGIC):
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件内容与 PDF 格式不符", status_code=400)
    if ext == "docx" and not data.startswith(_ZIP_MAGIC):
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件内容与 DOCX 格式不符", status_code=400)
    if ext in ("txt", "md") and not _looks_like_text(data):
        raise BusinessError("UNSUPPORTED_FILE_TYPE", "文件内容包含非法二进制字符", status_code=400)

    return ext, _KIND_BY_EXT[ext], _MIME_BY_EXT[ext]


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
