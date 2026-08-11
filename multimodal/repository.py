"""Persist attachment metadata and extraction results in PostgreSQL.

Message-to-attachment binding is owned by the memory repository so the link
commits in the same transaction as the user message.
"""
from __future__ import annotations

from typing import Optional

from psycopg.types.json import Jsonb

from context.postgres_pool import get_postgres_pool
from settings import MEMORY_CONFIG
from webui_new.core.errors import ConfigError

from .schemas import Attachment, Extraction


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
    """Read and write attachments through the shared PostgreSQL pool."""

    _COLUMNS = """
        id, user_id, session_id, request_id, filename, mime_type, kind,
        size_bytes, sha256, object_key, status, error_code, created_at, expires_at
    """

    def __init__(self, pool=None):
        self._pool = pool

    @property
    def pool(self):
        if self._pool is not None:
            return self._pool
        dsn = MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
        if not dsn:
            raise ConfigError(
                "ATTACHMENT_STORE_UNCONFIGURED",
                "附件存储未配置（缺少 HOMMEY_POSTGRES_DSN）",
            )
        self._pool = get_postgres_pool(dsn)
        return self._pool

    def create(self, attachment: Attachment) -> None:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO attachments
                        (id, user_id, session_id, request_id, filename, mime_type, kind,
                         size_bytes, sha256, object_key, status, error_code, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        attachment.id, attachment.user_id, attachment.session_id,
                        attachment.request_id, attachment.filename, attachment.mime_type,
                        attachment.kind, attachment.size_bytes, attachment.sha256,
                        attachment.object_key, attachment.status, attachment.error_code,
                        attachment.expires_at,
                    ),
                )

    def get(self, attachment_id: str, user_id: str) -> Optional[Attachment]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLUMNS} FROM attachments WHERE id = %s AND user_id = %s",
                    (attachment_id, user_id),
                )
                row = cur.fetchone()
        return _attachment_from_row(row) if row else None

    def get_many(self, attachment_ids: list[str], user_id: str) -> list[Attachment]:
        if not attachment_ids:
            return []
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._COLUMNS} FROM attachments WHERE user_id = %s AND id = ANY(%s)",
                    (user_id, attachment_ids),
                )
                rows = cur.fetchall()
        by_id = {row["id"]: _attachment_from_row(row) for row in rows}
        return [by_id[attachment_id] for attachment_id in attachment_ids if attachment_id in by_id]

    def list_by_user(self, user_id: str, limit: int = 100) -> list[Attachment]:
        """按创建时间倒序返回该用户的附件（附件面板用）。"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {self._COLUMNS}
                    FROM attachments
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (user_id, max(int(limit), 1)),
                )
                rows = cur.fetchall()
        return [_attachment_from_row(row) for row in rows]

    def update_status(
        self,
        attachment_id: str,
        user_id: str,
        status: str,
        error_code: Optional[str] = None,
    ) -> None:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE attachments SET status = %s, error_code = %s
                    WHERE id = %s AND user_id = %s
                    """,
                    (status, error_code, attachment_id, user_id),
                )

    def complete_processing(self, extraction: Extraction, user_id: str) -> None:
        """Store extracted text and mark its attachment ready in one transaction."""
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
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
                            extracted_at = NOW()
                        """,
                        (
                            extraction.attachment_id, extraction.parser_version,
                            extraction.language, extraction.content_text,
                            Jsonb(extraction.structured), extraction.char_count,
                        ),
                    )
                    cur.execute(
                        """
                        UPDATE attachments
                        SET status = 'ready', error_code = NULL
                        WHERE id = %s AND user_id = %s AND status = 'processing'
                        """,
                        (extraction.attachment_id, user_id),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("Attachment left the processing state")

    def get_extraction(self, attachment_id: str) -> Optional[Extraction]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attachment_id, parser_version, language, content_text,
                           structured, char_count
                    FROM attachment_extractions
                    WHERE attachment_id = %s
                    """,
                    (attachment_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return Extraction(
            attachment_id=row["attachment_id"],
            parser_version=row.get("parser_version"),
            language=row.get("language"),
            content_text=row.get("content_text") or "",
            structured=row.get("structured") or {},
            char_count=row.get("char_count") or 0,
        )

    def attachments_for_message(self, message_id: str) -> list[Attachment]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT a.id, a.user_id, a.session_id, a.request_id, a.filename,
                           a.mime_type, a.kind, a.size_bytes, a.sha256, a.object_key,
                           a.status, a.error_code, a.created_at, a.expires_at
                    FROM conversation_message_attachments cma
                    JOIN attachments a ON a.id = cma.attachment_id
                    WHERE cma.message_id = %s
                    ORDER BY cma.created_at
                    """,
                    (message_id,),
                )
                rows = cur.fetchall()
        return [_attachment_from_row(row) for row in rows]

    def delete(self, attachment_id: str, user_id: str) -> Optional[str]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM attachments
                    WHERE id = %s AND user_id = %s
                    RETURNING object_key
                    """,
                    (attachment_id, user_id),
                )
                row = cur.fetchone()
        return row["object_key"] if row else None
