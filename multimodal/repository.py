"""Attachment DB repository (psycopg short connections, dict_row).

与 webui_new/auth/storage.py 同款短连接惯法（autocommit + dict_row + 惰性 import psycopg），
复用 MEMORY_CONFIG.long_term.postgres_dsn。全部 SQL 参数化；附件正文不入日志。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg.types.json import Jsonb

from settings import MEMORY_CONFIG
from webui_new.core.errors import ConfigError

from .schemas import ATTACHMENT_STATUS_READY, Attachment, Extraction

logger = logging.getLogger(__name__)


@contextmanager
def _get_conn() -> Iterator:
    import psycopg
    from psycopg.rows import dict_row

    dsn = MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
    if not dsn:
        raise ConfigError(
            "AUTH_STORE_UNCONFIGURED",
            "附件存储未配置（缺少 HOMMEY_POSTGRES_DSN）",
        )
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def _attachment_from_row(row: dict) -> Attachment:
    created_at = row.get("created_at")
    expires_at = row.get("expires_at")
    return Attachment(
        id=row["id"],
        user_id=row["user_id"],
        session_id=row.get("session_id"),
        request_id=row.get("request_id"),
        filename=row["filename"],
        mime_type=row.get("mime_type"),
        kind=row["kind"],
        size_bytes=row["size_bytes"],
        sha256=row.get("sha256"),
        object_key=row["object_key"],
        status=row["status"],
        error_code=row.get("error_code"),
        created_at=created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        expires_at=expires_at.isoformat() if hasattr(expires_at, "isoformat") else expires_at,
    )


class AttachmentRepository:
    """attachments / attachment_extractions / chat_message_attachments 的读写。"""

    def create(self, att: Attachment) -> None:
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                INSERT INTO attachments
                    (id, user_id, session_id, request_id, filename, mime_type, kind,
                     size_bytes, sha256, object_key, status, error_code)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    att.id, att.user_id, att.session_id, att.request_id, att.filename,
                    att.mime_type, att.kind, att.size_bytes, att.sha256, att.object_key,
                    att.status, att.error_code,
                ),
            )

    def get(self, attachment_id: str, user_id: str) -> Optional[Attachment]:
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                SELECT id, user_id, session_id, request_id, filename, mime_type, kind,
                       size_bytes, sha256, object_key, status, error_code, created_at, expires_at
                FROM attachments
                WHERE id = %s AND user_id = %s;
                """,
                (attachment_id, user_id),
            )
            row = cur.fetchone()
        return _attachment_from_row(row) if row else None

    def get_many(self, attachment_ids: list[str], user_id: str) -> list[Attachment]:
        if not attachment_ids:
            return []
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                SELECT id, user_id, session_id, request_id, filename, mime_type, kind,
                       size_bytes, sha256, object_key, status, error_code, created_at, expires_at
                FROM attachments
                WHERE user_id = %s AND id = ANY(%s);
                """,
                (user_id, list(attachment_ids)),
            )
            rows = cur.fetchall()
        return [_attachment_from_row(r) for r in rows]

    def update_status(
        self, attachment_id: str, user_id: str, status: str, error_code: Optional[str] = None
    ) -> None:
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                UPDATE attachments SET status = %s, error_code = %s
                WHERE id = %s AND user_id = %s;
                """,
                (status, error_code, attachment_id, user_id),
            )

    def set_extraction(self, extraction: Extraction) -> None:
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                INSERT INTO attachment_extractions
                    (attachment_id, parser_version, language, content_text, structured, char_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (attachment_id) DO UPDATE SET
                    parser_version = EXCLUDED.parser_version,
                    language = EXCLUDED.language,
                    content_text = EXCLUDED.content_text,
                    structured = EXCLUDED.structured,
                    char_count = EXCLUDED.char_count,
                    extracted_at = NOW();
                """,
                (
                    extraction.attachment_id, extraction.parser_version, extraction.language,
                    extraction.content_text, Jsonb(extraction.structured), extraction.char_count,
                ),
            )

    def get_extraction(self, attachment_id: str) -> Optional[Extraction]:
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                SELECT attachment_id, parser_version, language, content_text, structured, char_count
                FROM attachment_extractions
                WHERE attachment_id = %s;
                """,
                (attachment_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        structured = row.get("structured") or {}
        if isinstance(structured, str):
            import json
            structured = json.loads(structured)
        return Extraction(
            attachment_id=row["attachment_id"],
            parser_version=row.get("parser_version"),
            language=row.get("language"),
            content_text=row.get("content_text") or "",
            structured=structured,
            char_count=row.get("char_count") or 0,
        )

    def bind(self, message_id: int, attachment_ids: list[str], user_id: str) -> int:
        """把附件关联到 chat_history 消息；只绑定属于该用户且 ready 的附件。返回绑定条数。"""
        if not attachment_ids:
            return 0
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                INSERT INTO chat_message_attachments (chat_history_id, attachment_id)
                SELECT %s, a.id
                FROM attachments a
                WHERE a.user_id = %s AND a.status = %s AND a.id = ANY(%s)
                ON CONFLICT (chat_history_id, attachment_id) DO NOTHING;
                """,
                (message_id, user_id, ATTACHMENT_STATUS_READY, list(attachment_ids)),
            )
            return cur.rowcount or 0

    def attachments_for_message(self, message_id: int) -> list[Attachment]:
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                """
                SELECT a.id, a.user_id, a.session_id, a.request_id, a.filename, a.mime_type,
                       a.kind, a.size_bytes, a.sha256, a.object_key, a.status, a.error_code,
                       a.created_at, a.expires_at
                FROM chat_message_attachments cma
                JOIN attachments a ON a.id = cma.attachment_id
                WHERE cma.chat_history_id = %s
                ORDER BY cma.created_at;
                """,
                (message_id,),
            )
            rows = cur.fetchall()
        return [_attachment_from_row(r) for r in rows]

    def delete(self, attachment_id: str, user_id: str) -> Optional[str]:
        """删除附件记录，返回其 object_key 供上层清理原文件。"""
        with _get_conn() as cur:  # type: ignore[arg-type]
            cur.execute(
                "SELECT object_key FROM attachments WHERE id = %s AND user_id = %s;",
                (attachment_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "DELETE FROM attachments WHERE id = %s AND user_id = %s;",
                (attachment_id, user_id),
            )
        return row["object_key"]
