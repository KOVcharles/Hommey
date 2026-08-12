"""PostgreSQL repositories for the stage-1 memory foundation."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from utils.memory_safety import (
    filter_safe_memory_mapping,
    is_safe_preference_value,
    redact_sensitive_text,
    sanitize_memory_value,
)
from .preference_schema import (
    PREFERENCE_LIST_COLUMNS,
    PREFERENCE_SCALAR_COLUMNS,
    normalize_list_preference,
    normalize_scalar_preference,
    preference_row_to_mapping,
)


class AttachmentBindingError(ValueError):
    """The requested attachment set cannot be bound to the user message."""


def stable_uuid(value: str | uuid.UUID | None, *, namespace: str) -> uuid.UUID:
    """Convert public string identifiers to deterministic database UUIDs."""
    if isinstance(value, uuid.UUID):
        return value
    if value:
        try:
            return uuid.UUID(str(value))
        except ValueError:
            return uuid.uuid5(uuid.NAMESPACE_URL, f"hommey:{namespace}:{value}")
    return uuid.uuid4()


@dataclass(frozen=True)
class SessionRecord:
    session_id: uuid.UUID
    user_id: str
    status: str
    started_at: datetime
    last_active_at: datetime
    message_count: int
    last_sequence: int
    summary_watermark: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "SessionRecord":
        return cls(
            session_id=row["session_id"],
            user_id=row["user_id"],
            status=row["status"],
            started_at=row["started_at"],
            last_active_at=row["last_active_at"],
            message_count=int(row["message_count"]),
            last_sequence=int(row["last_sequence"]),
            summary_watermark=int(row["summary_watermark"]),
        )


@dataclass(frozen=True)
class MessageRecord:
    message_id: uuid.UUID
    request_id: uuid.UUID
    turn_id: uuid.UUID
    session_id: uuid.UUID
    user_id: str
    sequence_no: int
    role: str
    content: str
    content_type: str
    created_at: datetime
    inserted: bool = True

    @classmethod
    def from_row(cls, row: dict[str, Any], *, inserted: bool = True) -> "MessageRecord":
        return cls(
            message_id=row["message_id"],
            request_id=row["request_id"],
            turn_id=row["turn_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            sequence_no=int(row["sequence_no"]),
            role=row["role"],
            content=row["content"],
            content_type=row["content_type"],
            created_at=row["created_at"],
            inserted=inserted,
        )


@dataclass(frozen=True)
class ClaimedSummaryRange:
    """A contiguous range of conversation_messages claimed for summarization.

    Returned by :meth:`PostgresMemoryRepository.claim_summary_range`. ``segment_no``
    equals ``source_sequence_from``: deterministic, monotonic, and unique among
    successful inserts (the watermark is the single authority).
    """

    summary_id: uuid.UUID
    user_id: str
    session_id: uuid.UUID
    segment_no: int
    source_sequence_from: int
    source_sequence_to: int
    source_message_count: int
    messages: list[tuple[int, str, str]]  # (sequence_no, role, content)


class PostgresMemoryRepository:
    """Transactional fact-source access for sessions and messages."""

    _SESSION_COLUMNS = """
        session_id, user_id, status, started_at, last_active_at,
        message_count, last_sequence, summary_watermark
    """
    _MESSAGE_COLUMNS = """
        message_id, request_id, turn_id, session_id, user_id, sequence_no,
        role, content, content_type, answer_document, presentation_document,
        created_at
    """

    def __init__(self, pool, *, raw_message_retention_days: int = 14):
        self.pool = pool
        self.raw_message_retention_days = max(int(raw_message_retention_days), 1)

    @staticmethod
    def _lock_user(cur, user_id: str) -> None:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (user_id,))

    def get_or_create_session(self, user_id: str, idle_timeout_sec: int) -> SessionRecord:
        """Resume a fresh active session or atomically rotate an idle one."""
        timeout = max(int(idle_timeout_sec), 1)
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._lock_user(cur, user_id)
                    cur.execute(
                        f"""
                        SELECT {self._SESSION_COLUMNS},
                               last_active_at >= NOW() - (%s * INTERVAL '1 second') AS reusable
                        FROM conversation_sessions
                        WHERE user_id = %s AND status = 'active'
                        ORDER BY last_active_at DESC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (timeout, user_id),
                    )
                    row = cur.fetchone()
                    if row and row["reusable"]:
                        cur.execute(
                            f"""
                            UPDATE conversation_sessions
                            SET last_active_at = NOW()
                            WHERE session_id = %s
                            RETURNING {self._SESSION_COLUMNS}
                            """,
                            (row["session_id"],),
                        )
                        return SessionRecord.from_row(cur.fetchone())

                    if row:
                        cur.execute(
                            """
                            UPDATE conversation_sessions
                            SET status = 'closed', closed_at = NOW(), close_reason = 'idle'
                            WHERE session_id = %s
                            """,
                            (row["session_id"],),
                        )
                    return self._insert_session(cur, user_id)

    def rotate_session(self, user_id: str, reason: str = "manual") -> SessionRecord:
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._lock_user(cur, user_id)
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET status = 'closed', closed_at = NOW(), close_reason = %s
                        WHERE user_id = %s AND status = 'active'
                        """,
                        (reason, user_id),
                    )
                    return self._insert_session(cur, user_id)

    def activate_session(
        self,
        user_id: str,
        session_id: str | uuid.UUID,
    ) -> SessionRecord:
        """Make an existing user session active without creating a replacement."""
        sid = stable_uuid(session_id, namespace="session")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._lock_user(cur, user_id)
                    cur.execute(
                        "SELECT session_id FROM conversation_sessions WHERE user_id = %s AND session_id = %s FOR UPDATE",
                        (user_id, sid),
                    )
                    if not cur.fetchone():
                        raise ValueError("Session not found")

                    # The partial unique index permits only one active session per user.
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET status = 'closed', closed_at = NOW(), close_reason = 'switched'
                        WHERE user_id = %s AND status = 'active' AND session_id <> %s
                        """,
                        (user_id, sid),
                    )
                    cur.execute(
                        f"""
                        UPDATE conversation_sessions
                        SET status = 'active', closed_at = NULL, close_reason = NULL,
                            last_active_at = NOW()
                        WHERE user_id = %s AND session_id = %s
                        RETURNING {self._SESSION_COLUMNS}
                        """,
                        (user_id, sid),
                    )
                    return SessionRecord.from_row(cur.fetchone())

    def close_session(self, user_id: str, session_id: str | uuid.UUID, reason: str = "manual") -> None:
        sid = stable_uuid(session_id, namespace="session")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET status = 'closed', closed_at = NOW(), close_reason = %s
                        WHERE session_id = %s AND user_id = %s AND status = 'active'
                        """,
                        (reason, sid, user_id),
                    )

    def _insert_session(self, cur, user_id: str) -> SessionRecord:
        session_id = uuid.uuid4()
        cur.execute(
            f"""
            INSERT INTO conversation_sessions (session_id, user_id, status)
            VALUES (%s, %s, 'active')
            RETURNING {self._SESSION_COLUMNS}
            """,
            (session_id, user_id),
        )
        return SessionRecord.from_row(cur.fetchone())

    def get_session(self, user_id: str, session_id: str | uuid.UUID) -> Optional[SessionRecord]:
        sid = stable_uuid(session_id, namespace="session")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {self._SESSION_COLUMNS} FROM conversation_sessions WHERE user_id = %s AND session_id = %s",
                    (user_id, sid),
                )
                row = cur.fetchone()
        return SessionRecord.from_row(row) if row else None

    def append_message(
        self,
        *,
        user_id: str,
        session_id: str | uuid.UUID,
        role: str,
        content: str,
        request_id: str | uuid.UUID,
        turn_id: str | uuid.UUID | None = None,
        content_type: str = "text",
        token_count: int | None = None,
        attachment_ids: Iterable[str] | None = None,
        answer_document: dict[str, Any] | None = None,
        presentation_document: dict[str, Any] | None = None,
    ) -> MessageRecord:
        from psycopg.types.json import Jsonb

        sid = stable_uuid(session_id, namespace="session")
        rid = stable_uuid(request_id, namespace=f"request:{user_id}")
        tid = stable_uuid(turn_id, namespace=f"turn:{user_id}:{rid}") if turn_id else uuid.uuid5(
            uuid.NAMESPACE_URL, f"hommey:turn:{user_id}:{rid}"
        )
        safe_content = redact_sensitive_text(content)
        retention_until = datetime.now(timezone.utc) + timedelta(days=self.raw_message_retention_days)

        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT {self._MESSAGE_COLUMNS}
                        FROM conversation_messages
                        WHERE user_id = %s AND request_id = %s AND role = %s
                        """,
                        (user_id, rid, role),
                    )
                    existing = cur.fetchone()
                    if existing:
                        # A retry may be the first process that has the typed
                        # payload (for example after a response disconnect).
                        # Fill null metadata only; never mutate an established
                        # idempotent response.
                        if role == "assistant" and (answer_document or presentation_document):
                            cur.execute(
                                f"""
                                UPDATE conversation_messages
                                SET answer_document = COALESCE(answer_document, %s),
                                    presentation_document = COALESCE(presentation_document, %s)
                                WHERE message_id = %s
                                RETURNING {self._MESSAGE_COLUMNS}
                                """,
                                (
                                    Jsonb(answer_document) if answer_document else None,
                                    Jsonb(presentation_document) if presentation_document else None,
                                    existing["message_id"],
                                ),
                            )
                            existing = cur.fetchone()
                        record = MessageRecord.from_row(existing, inserted=False)
                        if role == "user" and attachment_ids:
                            self._bind_ready_attachments(
                                cur,
                                record=record,
                                attachment_ids=attachment_ids,
                            )
                        return record

                    cur.execute(
                        """
                        SELECT last_sequence
                        FROM conversation_sessions
                        WHERE session_id = %s AND user_id = %s AND status = 'active'
                        FOR UPDATE
                        """,
                        (sid, user_id),
                    )
                    session = cur.fetchone()
                    if not session:
                        raise RuntimeError("Cannot append a message to a missing or closed session")
                    sequence_no = int(session["last_sequence"]) + 1
                    message_id = uuid.uuid4()
                    cur.execute(
                        f"""
                        INSERT INTO conversation_messages (
                            message_id, request_id, turn_id, session_id, user_id,
                            sequence_no, role, content, content_type, token_count,
                            retention_until, answer_document, presentation_document
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING {self._MESSAGE_COLUMNS}
                        """,
                        (
                            message_id,
                            rid,
                            tid,
                            sid,
                            user_id,
                            sequence_no,
                            role,
                            safe_content,
                            content_type,
                            token_count,
                            retention_until,
                            Jsonb(answer_document) if answer_document else None,
                            Jsonb(presentation_document) if presentation_document else None,
                        ),
                    )
                    inserted = cur.fetchone()
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET message_count = message_count + 1,
                            last_sequence = %s,
                            last_active_at = NOW()
                        WHERE session_id = %s
                        """,
                        (sequence_no, sid),
                    )
                    cur.execute(
                        """
                        INSERT INTO memory_versions (user_id, namespace, version)
                        VALUES (%s, 'messages', 1)
                        ON CONFLICT (user_id, namespace) DO UPDATE
                        SET version = memory_versions.version + 1, updated_at = NOW()
                        """,
                        (user_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO user_statistics (user_id, total_messages)
                        VALUES (%s, 1)
                        ON CONFLICT (user_id) DO UPDATE
                        SET total_messages = user_statistics.total_messages + 1, updated_at = NOW()
                        """,
                        (user_id,),
                    )
                    record = MessageRecord.from_row(inserted)
                    if role == "user" and attachment_ids:
                        self._bind_ready_attachments(
                            cur,
                            record=record,
                            attachment_ids=attachment_ids,
                        )
        return record

    @staticmethod
    def _bind_ready_attachments(cur, *, record: MessageRecord, attachment_ids: Iterable[str]) -> None:
        """Bind validated attachments in the message transaction."""
        unique_ids = list(dict.fromkeys(str(value) for value in attachment_ids if value))
        if not unique_ids:
            return

        # Security boundary: ownership and ready state are rechecked under lock.
        cur.execute(
            """
            SELECT id
            FROM attachments
            WHERE user_id = %s AND status = 'ready' AND id = ANY(%s)
              AND (expires_at IS NULL OR expires_at > NOW())
            FOR UPDATE
            """,
            (record.user_id, unique_ids),
        )
        found_ids = {row["id"] for row in cur.fetchall()}
        if found_ids != set(unique_ids):
            raise AttachmentBindingError(
                "Attachment is missing, not ready, or owned by another user"
            )

        # 幂等：对已有消息的重试，附件集合必须与本次消息已绑定的集合一致。
        # （附件可被多条消息复用，故按 message_id 作用域比较，而非全局唯一。）
        cur.execute(
            """
            SELECT attachment_id
            FROM conversation_message_attachments
            WHERE message_id = %s AND attachment_id = ANY(%s)
            """,
            (record.message_id, unique_ids),
        )
        bound_ids = {row["attachment_id"] for row in cur.fetchall()}
        if not record.inserted and bound_ids != set(unique_ids):
            raise AttachmentBindingError("Idempotent retry changed the attachment set")

        # Atomicity boundary: these rows commit or roll back with the user message.
        cur.executemany(
            """
            INSERT INTO conversation_message_attachments (message_id, attachment_id)
            VALUES (%s, %s)
            ON CONFLICT (message_id, attachment_id) DO NOTHING
            """,
            [(record.message_id, attachment_id) for attachment_id in unique_ids],
        )
        cur.execute(
            """
            UPDATE attachments
            SET session_id = %s, request_id = %s
            WHERE user_id = %s AND id = ANY(%s)
            """,
            (str(record.session_id), str(record.request_id), record.user_id, unique_ids),
        )

    def get_messages(
        self,
        user_id: str,
        *,
        limit: int | None = None,
        session_id: str | uuid.UUID | None = None,
        exclude_session_id: str | uuid.UUID | None = None,
        request_id: str | uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {self._MESSAGE_COLUMNS} FROM conversation_messages WHERE user_id = %s AND deleted_at IS NULL"
        params: list[Any] = [user_id]
        if session_id:
            sql += " AND session_id = %s"
            params.append(stable_uuid(session_id, namespace="session"))
        if exclude_session_id:
            sql += " AND session_id <> %s"
            params.append(stable_uuid(exclude_session_id, namespace="session"))
        if request_id:
            sql += " AND request_id = %s"
            params.append(stable_uuid(request_id, namespace=f"request:{user_id}"))
        sql += " ORDER BY created_at DESC, sequence_no DESC"
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        rows.reverse()
        return [
            {
                "message_id": str(row["message_id"]),
                "request_id": str(row["request_id"]),
                "turn_id": str(row["turn_id"]),
                "session_id": str(row["session_id"]),
                "sequence_no": int(row["sequence_no"]),
                "role": row["role"],
                "content": row["content"],
                "content_type": row["content_type"],
                "answer_document": row.get("answer_document"),
                "presentation_document": row.get("presentation_document"),
                "timestamp": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def update_message_documents(
        self,
        *,
        user_id: str,
        message_id: str | uuid.UUID,
        answer_document: dict[str, Any] | None = None,
        presentation_document: dict[str, Any] | None = None,
    ) -> bool:
        """Fill missing typed payloads while repairing pre-0016 history."""
        if not answer_document and not presentation_document:
            return False
        from psycopg.types.json import Jsonb

        mid = stable_uuid(message_id, namespace="message")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE conversation_messages
                    SET answer_document = COALESCE(answer_document, %s),
                        presentation_document = COALESCE(presentation_document, %s)
                    WHERE user_id = %s AND message_id = %s
                    """,
                    (
                        Jsonb(answer_document) if answer_document else None,
                        Jsonb(presentation_document) if presentation_document else None,
                        user_id,
                        mid,
                    ),
                )
                return cur.rowcount > 0

    def get_message_version(self, user_id: str) -> int:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version FROM memory_versions WHERE user_id = %s AND namespace = 'messages'",
                    (user_id,),
                )
                row = cur.fetchone()
        return int(row["version"]) if row else 0

    # ========== 增量会话摘要 ==========

    @staticmethod
    def _accumulate_segment(
        rows: list[tuple[int, str, str]],
        max_messages: int,
        max_chars: int,
    ) -> list[tuple[int, str, str]] | None:
        """Greedily take messages until ``max_messages`` or ``max_chars`` is reached.

        Character count is the size measurement (the ``token_count`` column is
        never populated in this codebase). Returns ``None`` if the whole backlog
        is below both thresholds — in that case nothing is summarized yet.
        """
        total_chars = 0
        chosen: list[tuple[int, str, str]] = []
        for seq, role, content in rows:
            total_chars += len(content or "")
            chosen.append((seq, role, content))
            if len(chosen) >= max_messages or total_chars >= max_chars:
                break
        if len(chosen) < max_messages and total_chars < max_chars:
            return None
        return chosen

    def claim_summary_range(
        self,
        user_id: str,
        session_id: str | uuid.UUID,
        *,
        max_turns: int = 5,
        max_chars: int = 6000,
    ) -> ClaimedSummaryRange | None:
        """Claim the next unsummarized message range for this session.

        Concurrency contract: the per-user advisory lock (:meth:`_lock_user`)
        serializes all claim transactions for one user across threads/processes,
        and the watermark is advanced inside the same transaction, so a second
        claim always reads the first's committed watermark and continues from
        ``to + 1`` — ranges are disjoint by construction. ``FOR UPDATE`` on the
        session row also serializes against ``append_message`` (which takes the
        same lock), giving a consistent ``last_sequence`` snapshot.

        Trade-off (see plan C1): if a crash happens between claim and insert,
        the claimed range is skipped (no self-heal) in exchange for the hard
        disjointness guarantee. Raw messages are unaffected.
        """
        sid = stable_uuid(session_id, namespace="session")
        max_messages = max(int(max_turns), 1) * 2  # 1 turn = user + assistant pair

        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    self._lock_user(cur, user_id)
                    cur.execute(
                        """
                        SELECT summary_watermark, last_sequence
                        FROM conversation_sessions
                        WHERE user_id = %s AND session_id = %s
                        FOR UPDATE
                        """,
                        (user_id, sid),
                    )
                    session = cur.fetchone()
                    if not session:
                        raise ValueError("Session not found for summary claim")
                    watermark = int(session["summary_watermark"])
                    last_sequence = int(session["last_sequence"])

                    from_seq = watermark + 1  # watermark is the single authority (C1)
                    if from_seq > last_sequence:
                        return None  # nothing unsummarized

                    cur.execute(
                        """
                        SELECT sequence_no, role, content
                        FROM conversation_messages
                        WHERE user_id = %s AND session_id = %s
                          AND sequence_no BETWEEN %s AND %s
                          AND deleted_at IS NULL
                        ORDER BY sequence_no ASC
                        """,
                        (user_id, sid, from_seq, last_sequence),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        return None
                    # dict_row cursors return dicts; normalize to tuples for the pure helper.
                    tuple_rows = [
                        (int(row["sequence_no"]), row["role"], row["content"])
                        for row in rows
                    ]
                    chosen = self._accumulate_segment(tuple_rows, max_messages, max_chars)
                    if chosen is None:
                        return None  # below both thresholds: don't advance

                    to_seq = int(chosen[-1][0])
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET summary_watermark = %s
                        WHERE user_id = %s AND session_id = %s
                        """,
                        (to_seq, user_id, sid),
                    )
                    return ClaimedSummaryRange(
                        summary_id=uuid.uuid4(),
                        user_id=user_id,
                        session_id=sid,
                        segment_no=from_seq,
                        source_sequence_from=from_seq,
                        source_sequence_to=to_seq,
                        source_message_count=len(chosen),
                        messages=list(chosen),
                    )

    def insert_session_summary(
        self,
        *,
        user_id: str,
        summary_id: uuid.UUID,
        session_id: str | uuid.UUID,
        segment_no: int,
        summary_text: str,
        source_sequence_from: int,
        source_sequence_to: int,
        source_message_count: int,
        model_name: str | None = None,
        prompt_version: str | None = None,
        summary_data: dict | None = None,
    ) -> None:
        """Idempotently persist one summary segment (upsert on the range key)."""
        from psycopg.types.json import Jsonb

        sid = stable_uuid(session_id, namespace="session")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO session_summaries (
                            summary_id, user_id, session_id, segment_no,
                            summary_text, summary_data,
                            source_sequence_from, source_sequence_to,
                            source_message_count, model_name, prompt_version, status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'done')
                        ON CONFLICT (session_id, source_sequence_from, source_sequence_to)
                        DO UPDATE SET
                            summary_text = EXCLUDED.summary_text,
                            summary_data = EXCLUDED.summary_data,
                            model_name = EXCLUDED.model_name,
                            prompt_version = EXCLUDED.prompt_version,
                            status = 'done',
                            updated_at = NOW()
                        """,
                        (
                            summary_id,
                            user_id,
                            sid,
                            int(segment_no),
                            summary_text,
                            Jsonb(summary_data or {}),
                            int(source_sequence_from),
                            int(source_sequence_to),
                            int(source_message_count),
                            model_name,
                            prompt_version,
                        ),
                    )

    def get_session_summaries(
        self,
        user_id: str,
        session_id: str | uuid.UUID | None = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return finished summary segments, oldest first, optionally per session."""
        sql = """
            SELECT summary_id, user_id, session_id, segment_no, summary_text,
                   summary_data, source_sequence_from, source_sequence_to,
                   source_message_count, model_name, prompt_version, created_at
            FROM session_summaries
            WHERE user_id = %s AND status = 'done'
        """
        params: list[Any] = [user_id]
        if session_id is not None:
            sql += " AND session_id = %s"
            params.append(stable_uuid(session_id, namespace="session"))
        sql += " ORDER BY created_at ASC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [
            {
                "summary_id": str(row["summary_id"]),
                "session_id": str(row["session_id"]),
                "segment_no": int(row["segment_no"]),
                "summary_text": row["summary_text"],
                "source_sequence_from": int(row["source_sequence_from"]),
                "source_sequence_to": int(row["source_sequence_to"]),
                "source_message_count": int(row["source_message_count"]),
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows
        ]


class PostgresCompatibilityStore:
    """Stage-1 adapter for preference, trip and task APIs used by existing agents."""

    def __init__(self, user_id: str, repository: PostgresMemoryRepository):
        self.user_id = user_id
        self.repository = repository
        self.pool = repository.pool

    def add_chat_message(self, role: str, content: str, session_id=None, metadata=None):
        metadata = metadata or {}
        request_id = metadata.get("request_id") or uuid.uuid4()
        result = self.repository.append_message(
            user_id=self.user_id,
            session_id=session_id,
            role=role,
            content=content,
            request_id=request_id,
            turn_id=metadata.get("turn_id"),
            answer_document=metadata.get("answer_document"),
            presentation_document=metadata.get("presentation_document"),
        )
        return result.inserted

    def get_chat_history(self, limit=None, session_id=None, exclude_session_id=None, request_id=None):
        return self.repository.get_messages(
            self.user_id,
            limit=limit,
            session_id=session_id,
            exclude_session_id=exclude_session_id,
            request_id=request_id,
        )

    def get_chat_session_titles(self) -> dict[str, str]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id, title FROM chat_session_titles WHERE user_id = %s",
                    (self.user_id,),
                )
                rows = cur.fetchall()
        return {row["session_id"]: row["title"] for row in rows}

    def rename_chat_session(self, session_id: str, title: str) -> None:
        clean_title = redact_sensitive_text(str(title or "").strip())[:80]
        if not clean_title:
            raise ValueError("Session title cannot be empty")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_session_titles (user_id, session_id, title)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, session_id) DO UPDATE
                    SET title = EXCLUDED.title, updated_at = NOW()
                    """,
                    (self.user_id, session_id, clean_title),
                )

    def delete_chat_session(self, session_id: str) -> None:
        sid = stable_uuid(session_id, namespace="session")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # Physical message deletion removes attachment links through the FK cascade.
                    cur.execute(
                        "DELETE FROM conversation_messages WHERE user_id = %s AND session_id = %s",
                        (self.user_id, sid),
                    )
                    cur.execute(
                        "DELETE FROM session_summaries WHERE user_id = %s AND session_id = %s",
                        (self.user_id, sid),
                    )
                    cur.execute(
                        "DELETE FROM chat_session_titles WHERE user_id = %s AND session_id = %s",
                        (self.user_id, session_id),
                    )
                    cur.execute(
                        "DELETE FROM active_trip_contexts WHERE user_id = %s AND session_id = %s",
                        (self.user_id, str(session_id)),
                    )
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET status = 'closed', closed_at = NOW(), close_reason = 'deleted'
                        WHERE user_id = %s AND session_id = %s
                        """,
                        (self.user_id, sid),
                    )

    def clear_chat_history(self) -> None:
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # Keep session rows for audit, but remove messages, titles, and attachment links.
                    cur.execute(
                        "DELETE FROM conversation_messages WHERE user_id = %s",
                        (self.user_id,),
                    )
                    cur.execute(
                        "DELETE FROM session_summaries WHERE user_id = %s",
                        (self.user_id,),
                    )
                    cur.execute(
                        "DELETE FROM chat_session_titles WHERE user_id = %s",
                        (self.user_id,),
                    )
                    cur.execute(
                        """
                        UPDATE conversation_sessions
                        SET status = 'closed', closed_at = NOW(), close_reason = 'cleared',
                            summary_watermark = 0
                        WHERE user_id = %s AND status = 'active'
                        """,
                        (self.user_id,),
                    )

    def save_preference(self, pref_type: str, value: Any):
        pref_type = str(pref_type or "").strip()
        if not pref_type:
            raise ValueError("Preference type cannot be empty")
        if not is_safe_preference_value(value):
            raise ValueError(f"Sensitive value is not allowed for preference: {pref_type}")
        from psycopg.types.json import Jsonb

        value = sanitize_memory_value(value)
        stored_value = value
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if pref_type in PREFERENCE_SCALAR_COLUMNS:
                        column = PREFERENCE_SCALAR_COLUMNS[pref_type]
                        stored_value = normalize_scalar_preference(value)
                        cur.execute(
                            f"""
                            INSERT INTO user_travel_preferences (
                                user_id, {column}, preference_updated_at
                            )
                            VALUES (%s, %s, jsonb_build_object(%s::text, to_jsonb(NOW())))
                            ON CONFLICT (user_id) DO UPDATE SET
                                {column} = EXCLUDED.{column},
                                preference_updated_at =
                                    user_travel_preferences.preference_updated_at
                                    || EXCLUDED.preference_updated_at,
                                updated_at = NOW()
                            """,
                            (self.user_id, stored_value, pref_type),
                        )
                    elif pref_type in PREFERENCE_LIST_COLUMNS:
                        column = PREFERENCE_LIST_COLUMNS[pref_type]
                        stored_value = normalize_list_preference(value)
                        cur.execute(
                            f"""
                            INSERT INTO user_travel_preferences (
                                user_id, {column}, preference_updated_at
                            )
                            VALUES (%s, %s, jsonb_build_object(%s::text, to_jsonb(NOW())))
                            ON CONFLICT (user_id) DO UPDATE SET
                                {column} = EXCLUDED.{column},
                                preference_updated_at =
                                    user_travel_preferences.preference_updated_at
                                    || EXCLUDED.preference_updated_at,
                                updated_at = NOW()
                            """,
                            (self.user_id, Jsonb(stored_value), pref_type),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO user_travel_preferences (
                                user_id, extra_preferences, preference_updated_at
                            )
                            VALUES (
                                %s,
                                jsonb_build_object(%s::text, %s),
                                jsonb_build_object(%s::text, to_jsonb(NOW()))
                            )
                            ON CONFLICT (user_id) DO UPDATE SET
                                extra_preferences =
                                    user_travel_preferences.extra_preferences
                                    || EXCLUDED.extra_preferences,
                                preference_updated_at =
                                    user_travel_preferences.preference_updated_at
                                    || EXCLUDED.preference_updated_at,
                                updated_at = NOW()
                            """,
                            (self.user_id, pref_type, Jsonb(stored_value), pref_type),
                        )

                    # Keep the EAV table as a rollback mirror during migration.
                    cur.execute(
                        """
                        INSERT INTO user_preferences (user_id, pref_type, pref_value, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (user_id, pref_type) DO UPDATE
                        SET pref_value = EXCLUDED.pref_value, updated_at = NOW()
                        """,
                        (self.user_id, pref_type, Jsonb(stored_value)),
                    )

    def get_preference(self, pref_type: str = None) -> Any:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        home_location,
                        transportation_preference,
                        hotel_brands,
                        airlines,
                        seat_preference,
                        meal_preference,
                        budget_level,
                        extra_preferences
                    FROM user_travel_preferences
                    WHERE user_id = %s
                    """,
                    (self.user_id,),
                )
                row = cur.fetchone()
                if row:
                    preferences = preference_row_to_mapping(row)
                else:
                    cur.execute(
                        """
                        SELECT pref_type, pref_value
                        FROM user_preferences
                        WHERE user_id = %s
                        """,
                        (self.user_id,),
                    )
                    preferences = {
                        item["pref_type"]: item["pref_value"]
                        for item in cur.fetchall()
                    }
        if pref_type is None:
            return preferences
        return preferences.get(pref_type)

    def add_hotel_brand(self, brand: str):
        brands = self.get_preference("hotel_brands") or []
        if brand not in brands:
            brands.append(brand)
        self.save_preference("hotel_brands", brands)

    def add_airline(self, airline: str):
        airlines = self.get_preference("airlines") or []
        if airline not in airlines:
            airlines.append(airline)
        self.save_preference("airlines", airlines)

    def save_trip_history(self, trip_info: dict[str, Any]):
        from psycopg.types.json import Jsonb

        trip_info = filter_safe_memory_mapping(trip_info)
        request_id = trip_info.get("request_id")
        trip_id = f"trip_{uuid.uuid4().hex[:12]}"
        destination = trip_info.get("destination")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO trip_history (
                            trip_id, user_id, origin, destination, start_date, end_date,
                            purpose, request_id, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (user_id, request_id)
                        WHERE request_id IS NOT NULL DO NOTHING
                        RETURNING trip_id
                        """,
                        (
                            trip_id,
                            self.user_id,
                            trip_info.get("origin"),
                            destination,
                            trip_info.get("start_date"),
                            trip_info.get("end_date"),
                            trip_info.get("purpose"),
                            request_id,
                        ),
                    )
                    inserted = cur.fetchone()
                    if not inserted:
                        return None
                    if destination:
                        cur.execute(
                            """
                            INSERT INTO user_statistics (
                                user_id, total_trips, frequent_destinations
                            ) VALUES (%s, 1, %s)
                            ON CONFLICT (user_id) DO UPDATE SET
                                total_trips = user_statistics.total_trips + 1,
                                frequent_destinations = jsonb_set(
                                    user_statistics.frequent_destinations,
                                    ARRAY[%s],
                                    to_jsonb(COALESCE(
                                        (user_statistics.frequent_destinations ->> %s)::int,
                                        0
                                    ) + 1),
                                    true
                                ),
                                updated_at = NOW()
                            """,
                            (self.user_id, Jsonb({destination: 1}), destination, destination),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO user_statistics (user_id, total_trips)
                            VALUES (%s, 1)
                            ON CONFLICT (user_id) DO UPDATE SET
                                total_trips = user_statistics.total_trips + 1,
                                updated_at = NOW()
                            """,
                            (self.user_id,),
                        )
        return trip_id

    def get_trip_history(self, limit: int | None = 10) -> list[dict[str, Any]]:
        sql = """
            SELECT trip_id, origin, destination, start_date, end_date, purpose, created_at
            FROM trip_history WHERE user_id = %s ORDER BY created_at DESC
        """
        params: list[Any] = [self.user_id]
        if limit:
            sql += " LIMIT %s"
            params.append(int(limit))
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        rows.reverse()
        return [
            {
                "trip_id": row["trip_id"],
                "timestamp": row["created_at"].isoformat(),
                "origin": row["origin"],
                "destination": row["destination"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "purpose": row["purpose"],
            }
            for row in rows
        ]

    def upsert_active_trip(
        self,
        trip_info: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        safe = filter_safe_memory_mapping(trip_info)
        session_key = str(session_id or "legacy")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT status, context_data FROM active_trip_contexts
                           WHERE user_id = %s AND session_id = %s FOR UPDATE""",
                        (self.user_id, session_key),
                    )
                    row = cur.fetchone()
                    current = dict(row["context_data"] or {}) if row else {}
                    if row and row["status"] in {"completed", "cancelled"}:
                        current = {}
                    merged = {**current, **{key: value for key, value in safe.items() if value is not None}}
                    status = merged.get("status", "active")
                    cur.execute(
                        """
                        INSERT INTO active_trip_contexts (user_id, session_id, status, context_data)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (user_id, session_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            context_data = EXCLUDED.context_data,
                            updated_at = NOW(),
                            completed_at = CASE
                                WHEN EXCLUDED.status IN ('completed', 'cancelled') THEN NOW()
                                ELSE NULL
                            END
                        """,
                        (self.user_id, session_key, status, Jsonb(merged)),
                    )
        return merged

    def get_active_trip(self, session_id: str | None = None) -> Optional[dict[str, Any]]:
        session_key = str(session_id or "legacy")
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT status, context_data, updated_at
                       FROM active_trip_contexts
                       WHERE user_id = %s AND session_id = %s""",
                    (self.user_id, session_key),
                )
                row = cur.fetchone()
        if not row:
            return None
        data = dict(row["context_data"] or {})
        data["status"] = row["status"]
        data["updated_at"] = row["updated_at"].isoformat()
        return data

    def clear_active_trip(self, session_id: str | None = None):
        session_key = str(session_id or "legacy")
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM active_trip_contexts WHERE user_id = %s AND session_id = %s",
                        (self.user_id, session_key),
                    )

    def get_statistics(self) -> dict[str, Any]:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT total_trips, total_messages, total_queries, frequent_destinations
                    FROM user_statistics WHERE user_id = %s
                    """,
                    (self.user_id,),
                )
                row = cur.fetchone()
        return dict(row) if row else {
            "total_trips": 0,
            "total_messages": 0,
            "total_queries": 0,
            "frequent_destinations": {},
        }

    def get_frequent_destinations(self, top_n: int = 5) -> list[tuple[str, int]]:
        items = self.get_statistics().get("frequent_destinations", {}).items()
        return sorted(items, key=lambda item: item[1], reverse=True)[:top_n]

    def increment_query_count(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO user_statistics (user_id, total_queries)
                        VALUES (%s, 1)
                        ON CONFLICT (user_id) DO UPDATE
                        SET total_queries = user_statistics.total_queries + 1, updated_at = NOW()
                        """,
                        (self.user_id,),
                    )

    def clear_history(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE conversation_messages SET deleted_at = NOW() WHERE user_id = %s AND deleted_at IS NULL",
                        (self.user_id,),
                    )
                    cur.execute("DELETE FROM trip_history WHERE user_id = %s", (self.user_id,))

    def delete_all(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    for table in (
                        "conversation_messages",
                        "session_summaries",
                        "conversation_sessions",
                        "memory_versions",
                        "user_preferences",
                        "trip_history",
                        "active_trip_contexts",
                        "user_statistics",
                    ):
                        cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (self.user_id,))

    def close(self):
        """Compatibility no-op: the shared process pool owns connections."""
