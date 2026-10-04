"""
长期记忆 (Long-term Memory)
支持本地 JSON 文件和 PostgreSQL 两种后端，便于本地调试与生产持久化切换。
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import uuid


from utils.memory_safety import (
    filter_safe_memory_mapping,
    is_safe_preference_value,
    redact_sensitive_text,
    sanitize_memory_value,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileLongTermMemory:
    """
    本地 JSON 长期记忆：适合开发调试，无需 PostgreSQL。
    数据按用户保存到 data/memory/{user_id}.json。
    """

    def __init__(self, user_id: str, storage_path: str = "data/memory", postgres_dsn: str = ""):
        self.user_id = user_id
        self.storage_path = storage_path
        self.file_path = Path(storage_path) / f"{user_id}.json"
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        logger.info(f"File long-term memory initialized for user: {user_id} ({self.file_path})")

    def _default_data(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "preferences": {},
            "chat_history": [],
            "session_titles": {},
            "trip_history": [],
            "active_trip": None,
            "active_trips": {},
            "statistics": {
                "total_trips": 0,
                "total_messages": 0,
                "total_queries": 0,
                "frequent_destinations": {},
            },
        }

    def _load(self) -> Dict[str, Any]:
        if not self.file_path.exists():
            data = self._default_data()
            self._save(data)
            return data

        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            backup_path = self.file_path.with_suffix(f".broken-{uuid.uuid4().hex[:8]}.json")
            try:
                self.file_path.replace(backup_path)
                logger.warning(f"Broken memory file moved to {backup_path}: {exc}")
            except OSError:
                logger.warning(f"Failed to backup broken memory file: {exc}")
            data = self._default_data()
            self._save(data)
            return data

        default = self._default_data()
        for key, value in default.items():
            data.setdefault(key, value)
        data.setdefault("statistics", {}).setdefault("frequent_destinations", {})
        data["statistics"].setdefault("total_trips", len(data.get("trip_history", [])))
        data["statistics"].setdefault("total_messages", len(data.get("chat_history", [])))
        data["statistics"].setdefault("total_queries", 0)
        return data

    def _save(self, data: Optional[Dict[str, Any]] = None):
        target = data if data is not None else self.data
        tmp_path = self.file_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(target, f, ensure_ascii=False, indent=2)
        tmp_path.replace(self.file_path)

    def save_preference(self, pref_type: str, value: Any):
        if not is_safe_preference_value(value):
            raise ValueError(f"Sensitive value is not allowed for preference: {pref_type}")
        value = sanitize_memory_value(value)
        self.data.setdefault("preferences", {})[pref_type] = value
        self._save()
        logger.info(f"Saved preference: {pref_type} = {value}")

    def get_preference(self, pref_type: str = None) -> Any:
        preferences = self.data.setdefault("preferences", {})
        if pref_type is None:
            return dict(preferences)
        return preferences.get(pref_type)


    def add_chat_message(
        self,
        role: str,
        content: str,
        session_id: str = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        metadata = metadata or {}
        content = redact_sensitive_text(content)
        request_id = metadata.get("request_id")
        if request_id and any(
            row.get("request_id") == request_id and row.get("role") == role
            for row in self.data.setdefault("chat_history", [])
        ):
            logger.info("Skipped duplicate chat message for request %s (%s)", request_id, role)
            return False
        message_id = f"msg_{uuid.uuid4().hex}"
        self.data.setdefault("chat_history", []).append({
            "id": message_id,
            "role": role,
            "content": content,
            "timestamp": _utc_now_iso(),
            "session_id": session_id,
            "request_id": request_id,
            "answer_document": metadata.get("answer_document"),
            "presentation_document": metadata.get("presentation_document"),
            "content_type": metadata.get("content_type", "text"),
        })
        stats = self.data.setdefault("statistics", {})
        stats["total_messages"] = int(stats.get("total_messages", 0)) + 1
        self._save()
        logger.debug(f"Added chat message to long-term memory: {role}")
        return message_id

    def get_chat_history(
        self,
        limit: int = None,
        session_id: str = None,
        exclude_session_id: str = None,
        request_id: str = None,
    ) -> List[Dict[str, Any]]:
        rows = self.data.setdefault("chat_history", [])
        if session_id:
            rows = [row for row in rows if row.get("session_id") == session_id]
        if exclude_session_id:
            rows = [row for row in rows if row.get("session_id") != exclude_session_id]
        if request_id:
            rows = [row for row in rows if row.get("request_id") == request_id]
        if limit:
            rows = rows[-limit:]
        return [dict(row) for row in rows]

    def get_chat_session_titles(self) -> Dict[str, str]:
        return dict(self.data.setdefault("session_titles", {}))

    def rename_chat_session(self, session_id: str, title: str) -> None:
        clean_title = redact_sensitive_text(str(title or "").strip())[:80]
        if not clean_title:
            raise ValueError("Session title cannot be empty")
        self.data.setdefault("session_titles", {})[session_id] = clean_title
        self._save()

    def delete_chat_session(self, session_id: str) -> None:
        rows = self.data.setdefault("chat_history", [])
        self.data["chat_history"] = [
            row for row in rows if row.get("session_id") != session_id
        ]
        self.data.setdefault("session_titles", {}).pop(session_id, None)
        self.data.setdefault("active_trips", {}).pop(str(session_id), None)
        self.data.setdefault("statistics", {})["total_messages"] = len(
            self.data["chat_history"]
        )
        self._save()

    def clear_chat_history(self) -> None:
        self.data["chat_history"] = []
        self.data["session_titles"] = {}
        self.data.setdefault("statistics", {})["total_messages"] = 0
        self._save()

    def save_trip_history(self, trip_info: Dict[str, Any]):
        trip_info = filter_safe_memory_mapping(trip_info)
        request_id = trip_info.get("request_id")
        if request_id:
            existing = next(
                (
                    row for row in self.data.setdefault("trip_history", [])
                    if row.get("request_id") == request_id
                ),
                None,
            )
            if existing:
                logger.info("Skipped duplicate trip for request %s", request_id)
                return existing.get("trip_id")
        trip_id = f"trip_{uuid.uuid4().hex[:12]}"
        destination = trip_info.get("destination")
        trip = {
            "trip_id": trip_id,
            "timestamp": _utc_now_iso(),
            "origin": trip_info.get("origin"),
            "destination": destination,
            "start_date": trip_info.get("start_date"),
            "end_date": trip_info.get("end_date"),
            "purpose": trip_info.get("purpose"),
            "request_id": request_id,
        }
        self.data.setdefault("trip_history", []).append(trip)
        stats = self.data.setdefault("statistics", {})
        stats["total_trips"] = int(stats.get("total_trips", 0)) + 1
        freq = stats.setdefault("frequent_destinations", {})
        if destination:
            freq[destination] = int(freq.get(destination, 0)) + 1
        self._save()
        logger.info(f"Saved trip history: {trip_id}")
        return trip_id

    def get_trip_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self.data.setdefault("trip_history", [])
        if limit:
            rows = rows[-limit:]
        return [dict(row) for row in rows]

    def upsert_active_trip(
        self,
        trip_info: Dict[str, Any],
        session_id: str | None = None,
    ) -> Dict[str, Any]:
        trip_info = filter_safe_memory_mapping(trip_info)
        session_key = str(session_id or "legacy")
        active_trips = self.data.setdefault("active_trips", {})
        current = active_trips.get(session_key) or (
            self.data.get("active_trip") if session_id is None else None
        ) or {}
        if current.get("status") in {"completed", "cancelled"}:
            current = {}
        merged = {**current, **{key: value for key, value in trip_info.items() if value is not None}}
        merged["status"] = merged.get("status", "active")
        merged["updated_at"] = _utc_now_iso()
        if merged["status"] in {"completed", "cancelled"}:
            merged["completed_at"] = _utc_now_iso()
        active_trips[session_key] = merged
        if session_id is None:
            self.data["active_trip"] = merged
        self._save()
        return dict(merged)

    def get_active_trip(self, session_id: str | None = None) -> Optional[Dict[str, Any]]:
        session_key = str(session_id or "legacy")
        trip = self.data.setdefault("active_trips", {}).get(session_key)
        if trip is None and session_id is None:
            trip = self.data.get("active_trip")
        return dict(trip) if isinstance(trip, dict) else None

    def clear_active_trip(self, session_id: str | None = None) -> None:
        session_key = str(session_id or "legacy")
        self.data.setdefault("active_trips", {}).pop(session_key, None)
        if session_id is None:
            self.data["active_trip"] = None
        self._save()

    def get_frequent_destinations(self, top_n: int = 5) -> List[tuple]:
        stats = self.get_statistics()
        freq = stats.get("frequent_destinations", {})
        sorted_dest = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return sorted_dest[:top_n]

    def increment_query_count(self):
        stats = self.data.setdefault("statistics", {})
        stats["total_queries"] = int(stats.get("total_queries", 0)) + 1
        self._save()

    def get_statistics(self) -> Dict[str, Any]:
        stats = self.data.setdefault("statistics", {})
        return {
            "total_trips": int(stats.get("total_trips", 0)),
            "total_messages": int(stats.get("total_messages", 0)),
            "total_queries": int(stats.get("total_queries", 0)),
            "frequent_destinations": dict(stats.get("frequent_destinations", {})),
        }

    def clear_history(self):
        self.data["chat_history"] = []
        self.data["session_titles"] = {}
        self.data["trip_history"] = []
        stats = self.data.setdefault("statistics", {})
        stats["total_trips"] = 0
        stats["total_messages"] = 0
        stats["frequent_destinations"] = {}
        self._save()
        logger.info("Cleared all history (chat + trips)")

    def delete_all(self):
        self.data = self._default_data()
        self._save()
        logger.warning(f"Deleted long-term memory data for user: {self.user_id}")


class DisabledLongTermMemory(FileLongTermMemory):
    """空长期记忆：完全不持久化，用于临时调试。"""

    def __init__(self, user_id: str, storage_path: str = "data/memory", postgres_dsn: str = ""):
        self.user_id = user_id
        self.storage_path = storage_path
        self.file_path = None
        self.data = self._default_data()
        logger.info(f"Disabled long-term memory initialized for user: {user_id}")

    def _save(self, data: Optional[Dict[str, Any]] = None):
        return None


class PostgresLongTermMemory:
    """Compatibility constructor backed by the shared stage-1 repository pool.

    Runtime code and compatibility imports use the shared pooled adapter.
    """

    def __new__(cls, user_id: str, storage_path: str = "data/memory", postgres_dsn: str = ""):
        from settings import MEMORY_CONFIG
        from .memory_repository import PostgresCompatibilityStore, PostgresMemoryRepository
        from .postgres_pool import get_postgres_pool

        dsn = postgres_dsn or MEMORY_CONFIG.get("long_term", {}).get("postgres_dsn", "")
        repository = PostgresMemoryRepository(
            get_postgres_pool(dsn),
            raw_message_retention_days=MEMORY_CONFIG.get("retention", {}).get(
                "raw_message_days", 14
            ),
        )
        return PostgresCompatibilityStore(str(user_id), repository)


LongTermMemory = PostgresLongTermMemory
