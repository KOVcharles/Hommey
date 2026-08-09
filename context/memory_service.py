"""Memory service for durable sessions plus incremental domain repositories."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from settings import MEMORY_CONFIG
from utils.memory_safety import redact_sensitive_text

from .long_term_memory import DisabledLongTermMemory, FileLongTermMemory
from .memory_repository import PostgresCompatibilityStore, PostgresMemoryRepository
from .postgres_pool import get_postgres_pool
from .profile_repository import PostgresProfileRepository
from .short_term_memory import ShortTermMemory

logger = logging.getLogger(__name__)


class RecentContextFacade:
    """Preserve the old short-term API while delegating to MemoryService."""

    def __init__(self, service: "MemoryService"):
        self._service = service

    @property
    def backend(self) -> str:
        return self._service.cache_backend

    @property
    def max_turns(self) -> int:
        return self._service.max_turns

    def add_message(self, role: str, content: str, metadata: dict | None = None):
        return self._service.append_message(role, content, metadata)

    def get_recent_context(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        return self._service.get_recent_context(n_turns)

    def get_context_string(self, n_turns: int = 5) -> str:
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"
        return "\n".join(
            f"{'用户' if message['role'] == 'user' else '助手'}: {message['content']}"
            for message in messages
        )

    def get_statistics(self) -> dict[str, Any]:
        return self._service.get_statistics()

    def clear(self):
        self._service.rotate_session(reason="clear")


class MemoryService:
    """Unified session/message service with optional PostgreSQL domain stores."""

    def __init__(
        self,
        user_id: str,
        requested_session_id: str | None = None,
        storage_path: str = "data/memory",
    ):
        self.user_id = str(user_id)
        self._last_activity_monotonic: Optional[float] = None
        short_config = MEMORY_CONFIG.get("short_term", {})
        long_config = MEMORY_CONFIG.get("long_term", {})
        self.max_turns = max(int(short_config.get("max_turns", 10)), 1)
        self.idle_timeout_sec = max(int(short_config.get("session_idle_timeout_sec", 600)), 1)
        self.cache_backend = str(short_config.get("backend", "memory")).lower()
        self.repository: PostgresMemoryRepository | None = None
        self.profile_repository: PostgresProfileRepository | None = None

        backend = str(long_config.get("backend", "file")).lower()
        self.backend = backend
        if backend == "postgres":
            pool = get_postgres_pool(long_config.get("postgres_dsn", ""))
            self.repository = PostgresMemoryRepository(
                pool,
                raw_message_retention_days=MEMORY_CONFIG.get("retention", {}).get(
                    "raw_message_days", 14
                ),
            )
            self.profile_repository = PostgresProfileRepository(pool)
            session = self.repository.get_or_create_session(self.user_id, self.idle_timeout_sec)
            self.session_id = str(session.session_id)
            self.long_term = PostgresCompatibilityStore(self.user_id, self.repository)
        elif backend == "disabled":
            self.session_id = requested_session_id or str(uuid.uuid4())
            self.long_term = DisabledLongTermMemory(self.user_id, storage_path=storage_path)
        elif backend == "file":
            self.session_id = requested_session_id or str(uuid.uuid4())
            self.long_term = FileLongTermMemory(self.user_id, storage_path=storage_path)
        else:
            raise ValueError(
                f"Unsupported long-term memory backend: {backend}. Use 'file', 'postgres', or 'disabled'."
            )

        self._cache = self._create_cache(self.session_id)
        self.short_term = RecentContextFacade(self)

    def _create_cache(self, session_id: str) -> ShortTermMemory:
        config = MEMORY_CONFIG.get("short_term", {})
        return ShortTermMemory(
            user_id=self.user_id,
            session_id=session_id,
            max_turns=self.max_turns,
            redis_host=config.get("redis_host", "127.0.0.1"),
            redis_port=config.get("redis_port", 6379),
            redis_db=config.get("redis_db", 0),
            redis_password=config.get("redis_password"),
            key_prefix=config.get("redis_key_prefix", "hommey:short_term"),
            backend=self.cache_backend,
            redis_ttl_sec=config.get("redis_ttl_sec", 86400),
        )

    def append_message(
        self,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(metadata or {})
        safe_content = redact_sensitive_text(content)
        if self.repository is None:
            if role == "user" and metadata.get("attachment_ids"):
                raise RuntimeError("Chat attachments require PostgreSQL memory")
            persisted = self.long_term.add_chat_message(
                role,
                safe_content,
                self.session_id,
                metadata=metadata,
            )
            if persisted is not False:
                self._cache.add_message(role, safe_content, metadata)
            return {
                "message_id": persisted if persisted is not False else None,
                "inserted": persisted is not False,
                "turn_id": metadata.get("turn_id"),
                "request_id": metadata.get("request_id"),
            }

        request_id = metadata.get("request_id") or uuid.uuid4()
        record = self.repository.append_message(
            user_id=self.user_id,
            session_id=self.session_id,
            role=role,
            content=safe_content,
            request_id=request_id,
            turn_id=metadata.get("turn_id"),
            content_type=metadata.get("content_type", "text"),
            token_count=metadata.get("token_count"),
            attachment_ids=metadata.get("attachment_ids") if role == "user" else None,
            answer_document=metadata.get("answer_document") if role == "assistant" else None,
            presentation_document=metadata.get("presentation_document") if role == "assistant" else None,
        )
        if record.inserted:
            try:
                cache_metadata = {**metadata, "turn_id": str(record.turn_id), "request_id": str(record.request_id)}
                self._cache.add_message(role, safe_content, cache_metadata)
            except Exception as exc:
                logger.warning("Redis recent-memory write failed; PostgreSQL remains authoritative: %s", exc)
        return {
            "message_id": str(record.message_id),
            "inserted": record.inserted,
            "turn_id": str(record.turn_id),
            "request_id": str(record.request_id),
            "sequence_no": record.sequence_no,
        }

    def get_recent_context(self, n_turns: int | None = None) -> list[dict[str, Any]]:
        requested_turns = self.max_turns if n_turns is None else min(max(int(n_turns), 0), self.max_turns)
        limit = requested_turns * 2
        if limit <= 0:
            return []
        if self.repository is None:
            return self._cache.get_recent_context(requested_turns)

        session = self.repository.get_session(self.user_id, self.session_id)
        expected = min(session.message_count if session else 0, self.max_turns * 2)
        expected_version = self.repository.get_message_version(self.user_id)
        try:
            cached = self._cache.get_recent_context(self.max_turns)
            cache_version = int(self._cache.get_statistics().get("message_version", 0))
            if len(cached) >= expected and cache_version == expected_version:
                return cached[-limit:]
        except Exception as exc:
            logger.warning("Redis recent-memory read failed; falling back to PostgreSQL: %s", exc)

        rows = self.repository.get_messages(
            self.user_id,
            session_id=self.session_id,
            limit=self.max_turns * 2,
        )
        cache_rows = [
            {
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
                "metadata": {
                    "request_id": row["request_id"],
                    "turn_id": row["turn_id"],
                    "sequence_no": row["sequence_no"],
                },
            }
            for row in rows
        ]
        try:
            self._cache.replace_messages(
                cache_rows,
                message_version=expected_version,
            )
        except Exception as exc:
            logger.warning("Redis recent-memory warmup failed: %s", exc)
        return cache_rows[-limit:]

    def get_statistics(self) -> dict[str, Any]:
        if self.repository is None:
            return self._cache.get_statistics()
        session = self.repository.get_session(self.user_id, self.session_id)
        rows = self.get_recent_context(self.max_turns)
        return {
            "total_messages": session.message_count if session else 0,
            "message_version": self.repository.get_message_version(self.user_id),
            "max_turns": self.max_turns,
            "backend": self.cache_backend,
            "fact_source": "postgres",
            "oldest_message_time": rows[0].get("timestamp") if rows else None,
            "newest_message_time": rows[-1].get("timestamp") if rows else None,
        }

    def ensure_active_session(self) -> bool:
        if self.repository is None:
            now = time.monotonic()
            rotated = bool(
                self._last_activity_monotonic is not None
                and now - self._last_activity_monotonic >= self.idle_timeout_sec
            )
            self._last_activity_monotonic = now
            if rotated:
                self.rotate_session(reason="idle")
            return rotated

        session = self.repository.get_or_create_session(self.user_id, self.idle_timeout_sec)
        new_session_id = str(session.session_id)
        rotated = new_session_id != self.session_id
        if rotated:
            self.session_id = new_session_id
            self._cache = self._create_cache(self.session_id)
        return rotated

    def rotate_session(self, requested_session_id: str | None = None, *, reason: str = "manual") -> str:
        try:
            self._cache.clear()
        except Exception as exc:
            logger.warning("Failed to clear recent-memory cache during session rotation: %s", exc)
        if self.repository is not None:
            session = self.repository.rotate_session(self.user_id, reason=reason)
            self.session_id = str(session.session_id)
        else:
            self.session_id = requested_session_id or str(uuid.uuid4())
            self._last_activity_monotonic = time.monotonic()
        self._cache = self._create_cache(self.session_id)
        return self.session_id

    def activate_session(self, session_id: str) -> str:
        """Switch the recent-memory facade to an existing durable session."""
        try:
            self._cache.clear()
        except Exception as exc:
            logger.warning("Failed to clear recent-memory cache during session switch: %s", exc)
        if self.repository is not None:
            session = self.repository.activate_session(self.user_id, session_id)
            self.session_id = str(session.session_id)
        else:
            self.session_id = session_id
        self._cache = self._create_cache(self.session_id)
        if self.repository is None:
            rows = self.long_term.get_chat_history(
                limit=self.max_turns * 2,
                session_id=self.session_id,
            )
            self._cache.replace_messages(rows)
        return self.session_id

    def close_session(self, reason: str = "manual") -> None:
        if self.repository is not None:
            self.repository.close_session(self.user_id, self.session_id, reason=reason)
        try:
            self._cache.clear()
        except Exception as exc:
            logger.warning("Failed to clear recent-memory cache while closing session: %s", exc)

    def get_recorded_response(self, request_id: str) -> str | None:
        rows = self.long_term.get_chat_history(limit=2, request_id=request_id)
        for row in reversed(rows):
            if row.get("role") == "assistant":
                return row.get("content") or None
        return None
