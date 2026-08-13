"""Unified text decoding strategy for RAG ingestion.

The audit (§4.14) calls out that knowledge-base TXT/MD is forced UTF-8 while
chat attachments try a cascade of encodings, so the same file can be accepted
in one path and rejected in the other.  This module is the single decode
strategy shared by the RAG parser and the upload validator.
"""
from __future__ import annotations

# Order matters: strict decoders first, most specific first.  ``utf-8-sig``
# strips a BOM and accepts plain UTF-8; GB18030 covers GBK.
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")


def decode_text_bytes(data: bytes) -> str:
    """Decode bytes with the unified strategy; never silently drop characters.

    Raises ``UnicodeDecodeError`` if no candidate encoding can decode the whole
    payload, so a corrupted file surfaces instead of being silently accepted.
    """
    last_error: UnicodeDecodeError | None = None
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding).strip()
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return data.decode("utf-8").strip()


def detect_encoding(data: bytes) -> str:
    """Return the first encoding that fully decodes the payload, else None."""
    for encoding in TEXT_ENCODINGS:
        try:
            data.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return None
