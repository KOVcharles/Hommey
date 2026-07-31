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

    def add_hotel_brand(self, brand: str):
        brands = self.get_preference("hotel_brands")
        if not isinstance(brands, list):
            brands = [brands] if brands else []
        if brand not in brands:
            brands.append(brand)
        self.save_preference("hotel_brands", brands)
        logger.info(f"Added hotel brand preference: {brand}")

    def add_airline(self, airline: str):
        airlines = self.get_preference("airlines")
        if not isinstance(airlines, list):
            airlines = [airlines] if airlines else []
        if airline not in airlines:
            airlines.append(airline)
        self.save_preference("airlines", airlines)
        logger.info(f"Added airline preference: {airline}")

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

    def upsert_active_trip(self, trip_info: Dict[str, Any]) -> Dict[str, Any]:
        trip_info = filter_safe_memory_mapping(trip_info)
        current = self.data.get("active_trip") or {}
        if current.get("status") in {"completed", "cancelled"}:
            current = {}
        merged = {**current, **{key: value for key, value in trip_info.items() if value is not None}}
        merged["status"] = merged.get("status", "active")
        merged["updated_at"] = _utc_now_iso()
        if merged["status"] in {"completed", "cancelled"}:
            merged["completed_at"] = _utc_now_iso()
        self.data["active_trip"] = merged
        self._save()
        return dict(merged)

    def get_active_trip(self) -> Optional[Dict[str, Any]]:
        trip = self.data.get("active_trip")
        return dict(trip) if isinstance(trip, dict) else None

    def clear_active_trip(self) -> None:
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
    """
    长期记忆：持久化用户信息
    - 用户偏好（家庭地址、酒店品牌、航空公司等）
    - 历史行程记录
    - 统计信息
    """

    def __init__(self, user_id: str, storage_path: str = "data/memory", postgres_dsn: str = ""):
        """
        初始化长期记忆

        Args:
            user_id: 用户ID
            storage_path: 兼容保留参数（PostgreSQL 后端不使用）
            postgres_dsn: PostgreSQL 连接串
        """
        self.user_id = user_id
        self.storage_path = storage_path
        self.postgres_dsn = postgres_dsn

        if not self.postgres_dsn:
            raise ValueError("postgres_dsn is required when long-term memory backend is 'postgres'")
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        self._jsonb = Jsonb
        self.conn = psycopg.connect(self.postgres_dsn, autocommit=True, row_factory=dict_row)
        self._init_schema()
        self._ensure_user_stats_row()
        logger.info(f"PostgreSQL long-term memory initialized for user: {user_id}")

    def _init_schema(self):
        """初始化 PostgreSQL 表结构"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT NOT NULL,
                    pref_type TEXT NOT NULL,
                    pref_value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, pref_type)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    request_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_session_titles (
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, session_id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trip_history (
                    id BIGSERIAL PRIMARY KEY,
                    trip_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    origin TEXT,
                    destination TEXT,
                    start_date TEXT,
                    end_date TEXT,
                    purpose TEXT,
                    request_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute("ALTER TABLE chat_history ADD COLUMN IF NOT EXISTS request_id TEXT;")
            cur.execute("ALTER TABLE trip_history ADD COLUMN IF NOT EXISTS request_id TEXT;")
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_history_request_role
                ON chat_history (user_id, request_id, role)
                WHERE request_id IS NOT NULL;
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_trip_history_request
                ON trip_history (user_id, request_id)
                WHERE request_id IS NOT NULL;
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_history_user_created
                ON chat_history (user_id, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trip_history_user_created
                ON trip_history (user_id, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_statistics (
                    user_id TEXT PRIMARY KEY,
                    total_trips INTEGER NOT NULL DEFAULT 0,
                    total_messages INTEGER NOT NULL DEFAULT 0,
                    total_queries INTEGER NOT NULL DEFAULT 0,
                    frequent_destinations JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS active_trip_contexts (
                    user_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active',
                    context_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                );
                """
            )
            # 多模态附件（与迁移 0005_multimodal_attachments.sql 保持一致）
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    request_id TEXT,
                    filename TEXT NOT NULL,
                    mime_type TEXT,
                    kind TEXT NOT NULL,
                    size_bytes BIGINT NOT NULL,
                    sha256 TEXT,
                    object_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_attachments_user_created
                ON attachments (user_id, created_at DESC);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS attachment_extractions (
                    attachment_id TEXT PRIMARY KEY
                        REFERENCES attachments(id) ON DELETE CASCADE,
                    parser_version TEXT,
                    language TEXT,
                    content_text TEXT,
                    structured JSONB NOT NULL DEFAULT '{}'::jsonb,
                    char_count INTEGER NOT NULL DEFAULT 0,
                    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_message_attachments (
                    chat_history_id BIGINT NOT NULL
                        REFERENCES chat_history(id) ON DELETE CASCADE,
                    attachment_id TEXT NOT NULL
                        REFERENCES attachments(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (chat_history_id, attachment_id)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_message_attachments_attachment
                ON chat_message_attachments (attachment_id);
                """
            )

    def _ensure_user_stats_row(self):
        """确保用户统计行存在"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_statistics (user_id)
                VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING;
                """,
                (self.user_id,),
            )

    def save_preference(self, pref_type: str, value: Any):
        """
        保存用户偏好（列表格式）

        Args:
            pref_type: 偏好类型
            value: 偏好值
        """
        if not is_safe_preference_value(value):
            raise ValueError(f"Sensitive value is not allowed for preference: {pref_type}")
        value = sanitize_memory_value(value)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_preferences (user_id, pref_type, pref_value, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, pref_type)
                DO UPDATE SET pref_value = EXCLUDED.pref_value, updated_at = NOW();
                """,
                (self.user_id, pref_type, self._jsonb(value)),
            )
        logger.info(f"Saved preference: {pref_type} = {value}")

    def get_preference(self, pref_type: str = None) -> Any:
        """
        获取用户偏好

        Args:
            pref_type: 偏好类型，None返回字典格式的全部偏好

        Returns:
            偏好值或偏好字典
        """
        if pref_type is None:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pref_type, pref_value
                    FROM user_preferences
                    WHERE user_id = %s;
                    """,
                    (self.user_id,),
                )
                rows = cur.fetchall()
            return {row["pref_type"]: row["pref_value"] for row in rows}

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT pref_value
                FROM user_preferences
                WHERE user_id = %s AND pref_type = %s;
                """,
                (self.user_id, pref_type),
            )
            row = cur.fetchone()
        return row["pref_value"] if row else None

    def add_hotel_brand(self, brand: str):
        """添加酒店品牌偏好（追加到列表）"""
        brands = self.get_preference("hotel_brands")
        if not isinstance(brands, list):
            brands = [brands] if brands else []
        if brand not in brands:
            brands.append(brand)
        self.save_preference("hotel_brands", brands)
        logger.info(f"Added hotel brand preference: {brand}")

    def add_airline(self, airline: str):
        """添加航空公司偏好（追加到列表）"""
        airlines = self.get_preference("airlines")
        if not isinstance(airlines, list):
            airlines = [airlines] if airlines else []
        if airline not in airlines:
            airlines.append(airline)
        self.save_preference("airlines", airlines)
        logger.info(f"Added airline preference: {airline}")

    def add_chat_message(
        self,
        role: str,
        content: str,
        session_id: str = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """
        添加聊天消息到长期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            session_id: 会话ID（可选）
        """
        metadata = metadata or {}
        content = redact_sensitive_text(content)
        request_id = metadata.get("request_id")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_history (user_id, session_id, role, content, request_id, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, request_id, role)
                WHERE request_id IS NOT NULL
                DO NOTHING
                RETURNING id;
                """,
                (self.user_id, session_id, role, content, request_id),
            )
            inserted = cur.fetchone()
            if not inserted:
                logger.info("Skipped duplicate chat message for request %s (%s)", request_id, role)
                return False
            cur.execute(
                """
                UPDATE user_statistics
                SET total_messages = total_messages + 1, updated_at = NOW()
                WHERE user_id = %s;
                """,
                (self.user_id,),
            )
        logger.debug(f"Added chat message to long-term memory: {role}")
        return inserted["id"]

    def get_chat_history(
        self,
        limit: int = None,
        session_id: str = None,
        exclude_session_id: str = None,
        request_id: str = None,
    ) -> List[Dict[str, Any]]:
        """
        获取聊天历史

        Args:
            limit: 返回数量限制
            session_id: 会话ID（只返回特定会话的消息）

        Returns:
            消息列表
        """
        sql = """
            SELECT id, role, content, created_at, session_id, request_id
            FROM chat_history
            WHERE user_id = %s
        """
        params: List[Any] = [self.user_id]
        if session_id:
            sql += " AND session_id = %s"
            params.append(session_id)
        if exclude_session_id:
            sql += " AND (session_id IS NULL OR session_id <> %s)"
            params.append(exclude_session_id)
        if request_id:
            sql += " AND request_id = %s"
            params.append(request_id)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        rows.reverse()
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["created_at"].isoformat(),
                "session_id": row["session_id"],
                "request_id": row["request_id"],
            }
            for row in rows
        ]

    def get_chat_session_titles(self) -> Dict[str, str]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, title
                FROM chat_session_titles
                WHERE user_id = %s;
                """,
                (self.user_id,),
            )
            rows = cur.fetchall()
        return {row["session_id"]: row["title"] for row in rows}

    def rename_chat_session(self, session_id: str, title: str) -> None:
        clean_title = redact_sensitive_text(str(title or "").strip())[:80]
        if not clean_title:
            raise ValueError("Session title cannot be empty")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_session_titles (user_id, session_id, title, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id, session_id)
                DO UPDATE SET title = EXCLUDED.title, updated_at = NOW();
                """,
                (self.user_id, session_id, clean_title),
            )

    def delete_chat_session(self, session_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chat_history WHERE user_id = %s AND session_id = %s;",
                (self.user_id, session_id),
            )
            cur.execute(
                "DELETE FROM chat_session_titles WHERE user_id = %s AND session_id = %s;",
                (self.user_id, session_id),
            )
            cur.execute(
                """
                UPDATE user_statistics
                SET total_messages = (
                    SELECT COUNT(*) FROM chat_history WHERE user_id = %s
                ), updated_at = NOW()
                WHERE user_id = %s;
                """,
                (self.user_id, self.user_id),
            )

    def clear_chat_history(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM chat_history WHERE user_id = %s;", (self.user_id,))
            cur.execute("DELETE FROM chat_session_titles WHERE user_id = %s;", (self.user_id,))
            cur.execute(
                """
                UPDATE user_statistics
                SET total_messages = 0, updated_at = NOW()
                WHERE user_id = %s;
                """,
                (self.user_id,),
            )

    def save_trip_history(self, trip_info: Dict[str, Any]):
        """
        保存行程历史

        Args:
            trip_info: 行程信息
        """
        trip_info = filter_safe_memory_mapping(trip_info)
        trip_id = f"trip_{uuid.uuid4().hex[:12]}"
        destination = trip_info.get("destination")
        request_id = trip_info.get("request_id")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trip_history (
                    trip_id, user_id, origin, destination, start_date, end_date, purpose, request_id, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, request_id)
                WHERE request_id IS NOT NULL
                DO NOTHING
                RETURNING trip_id;
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
                logger.info("Skipped duplicate trip for request %s", request_id)
                return None
            if destination:
                cur.execute(
                    """
                    UPDATE user_statistics
                    SET
                        total_trips = total_trips + 1,
                        frequent_destinations = jsonb_set(
                            frequent_destinations,
                            ARRAY[%s],
                            to_jsonb(COALESCE((frequent_destinations ->> %s)::int, 0) + 1),
                            true
                        ),
                        updated_at = NOW()
                    WHERE user_id = %s;
                    """,
                    (destination, destination, self.user_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE user_statistics
                    SET total_trips = total_trips + 1, updated_at = NOW()
                    WHERE user_id = %s;
                    """,
                    (self.user_id,),
                )
        logger.info(f"Saved trip history: {trip_id}")
        return trip_id

    def get_trip_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取历史行程

        Args:
            limit: 返回数量限制

        Returns:
            行程列表
        """
        sql = """
            SELECT trip_id, origin, destination, start_date, end_date, purpose, created_at
            FROM trip_history
            WHERE user_id = %s
            ORDER BY created_at DESC
        """
        params: List[Any] = [self.user_id]
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        with self.conn.cursor() as cur:
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

    def upsert_active_trip(self, trip_info: Dict[str, Any]) -> Dict[str, Any]:
        trip_info = filter_safe_memory_mapping(trip_info)
        current = self.get_active_trip() or {}
        if current.get("status") in {"completed", "cancelled"}:
            current = {}
        merged = {**current, **{key: value for key, value in trip_info.items() if value is not None}}
        status = merged.get("status", "active")
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO active_trip_contexts (user_id, status, context_data, created_at, updated_at)
                VALUES (%s, %s, %s, NOW(), NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    context_data = EXCLUDED.context_data,
                    updated_at = NOW(),
                    completed_at = CASE
                        WHEN EXCLUDED.status IN ('completed', 'cancelled') THEN NOW()
                        ELSE NULL
                    END
                """,
                (self.user_id, status, self._jsonb(merged)),
            )
        return merged

    def get_active_trip(self) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT status, context_data, updated_at FROM active_trip_contexts WHERE user_id = %s",
                (self.user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        data = dict(row["context_data"] or {})
        data["status"] = row["status"]
        data["updated_at"] = row["updated_at"].isoformat()
        return data

    def clear_active_trip(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM active_trip_contexts WHERE user_id = %s", (self.user_id,))

    def get_frequent_destinations(self, top_n: int = 5) -> List[tuple]:
        """
        获取常去目的地

        Args:
            top_n: 返回前N个

        Returns:
            [(destination, count), ...]
        """
        stats = self.get_statistics()
        freq = stats.get("frequent_destinations", {})
        sorted_dest = sorted(freq.items(), key=lambda x: x[1], reverse=True)
        return sorted_dest[:top_n]

    def increment_query_count(self):
        """增加查询计数"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_statistics
                SET total_queries = total_queries + 1, updated_at = NOW()
                WHERE user_id = %s;
                """,
                (self.user_id,),
            )

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT total_trips, total_messages, total_queries, frequent_destinations
                FROM user_statistics
                WHERE user_id = %s;
                """,
                (self.user_id,),
            )
            row = cur.fetchone()
        if not row:
            return {
                "total_trips": 0,
                "total_messages": 0,
                "total_queries": 0,
                "frequent_destinations": {},
            }
        return {
            "total_trips": row["total_trips"],
            "total_messages": row["total_messages"],
            "total_queries": row["total_queries"],
            "frequent_destinations": row["frequent_destinations"] or {},
        }

    def clear_history(self):
        """清空历史记录（保留偏好）"""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM chat_history WHERE user_id = %s;", (self.user_id,))
            cur.execute("DELETE FROM chat_session_titles WHERE user_id = %s;", (self.user_id,))
            cur.execute("DELETE FROM trip_history WHERE user_id = %s;", (self.user_id,))
            cur.execute(
                """
                UPDATE user_statistics
                SET
                    total_trips = 0,
                    total_messages = 0,
                    frequent_destinations = '{}'::jsonb,
                    updated_at = NOW()
                WHERE user_id = %s;
                """,
                (self.user_id,),
            )
        logger.info("Cleared all history (chat + trips)")

        logger.warning(f"Deleted long-term memory data for user: {self.user_id}")


LongTermMemory = PostgresLongTermMemory
