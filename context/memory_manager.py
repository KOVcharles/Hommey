"""
记忆管理器 (Memory Manager)
统一管理两层记忆，提供简单的API
"""
from typing import Dict, Any
import uuid
from copy import copy
from .memory_service import MemoryService
from utils.memory_safety import filter_safe_memory_mapping, redact_sensitive_text
import logging

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    记忆管理器：统一管理两层记忆
    - 短期记忆：最近对话（会话级）
    - 长期记忆：用户偏好和历史（跨会话）
    """

    def __init__(self, user_id: str, session_id: str | None = None, storage_path: str = "data/memory"):
        """
        初始化记忆管理器

        Args:
            user_id: 用户ID
            session_id: 会话ID
            storage_path: 长期记忆存储路径
        """
        self.user_id = user_id
        self.memory_service = MemoryService(
            user_id=user_id,
            requested_session_id=session_id,
            storage_path=storage_path,
        )
        self.session_id = self.memory_service.session_id
        self.short_term = self.memory_service.short_term
        self.long_term = self.memory_service.long_term
        # Stage-2 domains are exposed separately so legacy preference APIs stay unchanged.
        self.profile_repository = self.memory_service.profile_repository
        self.current_request_id: str | None = None
        self._current_turn_id: str | None = None

        logger.info(f"Memory manager initialized for user {user_id}, session {session_id}")

    def for_session(self, session_id: str) -> "MemoryManager":
        """Request-owned session, cache facade and message/turn identifiers."""
        bound = copy(self)
        bound.memory_service = self.memory_service.for_session(session_id)
        bound.session_id = bound.memory_service.session_id
        bound.short_term = bound.memory_service.short_term
        bound.current_request_id = None
        bound._current_turn_id = None
        return bound

    def rotate_session(self, session_id: str) -> str:
        """Start a new short-term session while preserving long-term memory."""
        previous_session = self.session_id
        self.session_id = self.memory_service.rotate_session(session_id, reason="manual")
        self.short_term = self.memory_service.short_term
        logger.info("Rotated memory session: %s -> %s", previous_session, self.session_id)
        return self.session_id

    def activate_session(self, session_id: str) -> str:
        """Switch to an existing session and rebuild its recent-memory view."""
        self.session_id = self.memory_service.activate_session(session_id)
        self.short_term = self.memory_service.short_term
        return self.session_id

    # ========== 短期记忆操作 ==========

    def add_message(self, role: str, content: str, metadata: Dict = None):
        """
        添加消息到短期记忆和长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 元数据

        Returns:
            新写入或幂等命中的消息 id；写入失败时返回 False。
        """
        metadata = dict(metadata or {})
        safe_content = redact_sensitive_text(content)
        if role == "user":
            self.current_request_id = metadata.get("request_id") or uuid.uuid4().hex
            self._current_turn_id = metadata.get("turn_id")
        else:
            self.current_request_id = metadata.get("request_id") or self.current_request_id or uuid.uuid4().hex
        metadata["request_id"] = self.current_request_id
        if self._current_turn_id:
            metadata["turn_id"] = self._current_turn_id

        result = self.memory_service.append_message(role, safe_content, metadata)
        if result.get("turn_id"):
            self._current_turn_id = result["turn_id"]
        return result.get("message_id") or False

    def get_recorded_response(self, request_id: str) -> str | None:
        """Return a completed assistant response for an idempotent retry."""
        if not request_id:
            return None
        return self.memory_service.get_recorded_response(request_id)

    def get_recorded_answer_document(self, request_id: str) -> dict | None:
        """Return the structured answer saved for an idempotent retry, when present."""
        if not request_id:
            return None
        rows = self.long_term.get_chat_history(limit=2, request_id=request_id, session_id=self.session_id)
        for row in reversed(rows):
            if row.get("role") == "assistant" and isinstance(row.get("answer_document"), dict):
                return row["answer_document"]
        return None

    def get_recorded_presentation_document(self, request_id: str) -> dict | None:
        """Return a typed presentation saved for an idempotent retry."""
        if not request_id:
            return None
        rows = self.long_term.get_chat_history(limit=2, request_id=request_id, session_id=self.session_id)
        for row in reversed(rows):
            document = row.get("presentation_document")
            if row.get("role") == "assistant" and isinstance(document, dict):
                return document
        return None

    # ========== 长期记忆操作 ==========
    # 注意：大部分方法直接使用 self.short_term 和 self.long_term 即可，无需封装

    # ========== 综合查询 ==========


    def get_active_trip(self, session_id: str | None = None) -> Dict[str, Any] | None:
        session_id = session_id or self.session_id
        if not session_id:
            raise ValueError("Session ID is required to read an active trip")
        trip = self.long_term.get_active_trip(session_id)
        if not trip or trip.get("status", "active") in {"completed", "cancelled"}:
            return None
        return trip

    def update_active_trip(self, trip_info: Dict[str, Any]) -> Dict[str, Any]:
        if not self.session_id:
            raise ValueError("Bind a session before updating an active trip")
        return self.long_term.upsert_active_trip(
            filter_safe_memory_mapping(trip_info),
            self.session_id,
        )

    def complete_active_trip(self, reason: str = "planning_completed") -> Dict[str, Any] | None:
        trip = self.get_active_trip()
        if not trip:
            return None
        return self.long_term.upsert_active_trip(
            {"status": "completed", "completion_reason": reason},
            self.session_id,
        )

    def cancel_active_trip(self, reason: str = "user_cancelled") -> Dict[str, Any] | None:
        trip = self.get_active_trip()
        if not trip:
            return None
        return self.long_term.upsert_active_trip(
            {"status": "cancelled", "completion_reason": reason},
            self.session_id,
        )


    # ========== 会话管理 ==========

    def end_session(self):
        """结束会话"""
        self.memory_service.close_session(reason="manual")
        logger.info(f"Session ended: {self.session_id}")


    # ========== 增量会话摘要（v1） ==========
