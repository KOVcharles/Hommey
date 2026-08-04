"""
短期记忆 (Short-term Memory)
使用 Redis 存储当前会话最近的对话历史，用于理解上下文和消歧
"""
from typing import List, Dict, Any
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """
    短期记忆：存储最近的对话历史
    - 存储最近 5-10 轮对话
    - 自动淘汰旧消息
    - 用于上下文理解
    """

    def __init__(
        self,
        user_id: str,
        session_id: str,
        max_turns: int = 10,
        redis_host: str = "127.0.0.1",
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: str = None,
        key_prefix: str = "hommey:short_term",
        backend: str = "memory",
        redis_ttl_sec: int = 86400,
    ):
        """
        初始化短期记忆

        Args:
            user_id: 用户ID
            session_id: 会话ID
            max_turns: 最大保存轮数（一轮 = 一对用户-助手消息）
            redis_host: Redis 地址
            redis_port: Redis 端口
            redis_db: Redis DB 编号
            redis_password: Redis 密码（可选）
            key_prefix: Redis key 前缀
            backend: 短期记忆后端，可选 memory 或 redis
        """
        self.user_id = user_id
        self.session_id = session_id
        self.max_turns = max_turns
        self.backend = backend.lower()
        self.redis_key = f"{key_prefix}:{user_id}:{session_id}"
        self.redis_version_key = f"{self.redis_key}:version"
        self.redis_ttl_sec = max(int(redis_ttl_sec), 1)
        self.messages: List[Dict[str, Any]] = []
        self.message_version = 0
        self.redis_client = None

        if self.backend == "redis":
            import redis

            self.redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                password=redis_password,
                decode_responses=True,
            )
        elif self.backend != "memory":
            raise ValueError(f"Unsupported short-term memory backend: {backend}. Use 'memory' or 'redis'.")

    def add_message(self, role: str, content: str, metadata: Dict = None):
        """
        添加消息到短期记忆

        Args:
            role: 角色 (user/assistant)
            content: 消息内容
            metadata: 额外的元数据
        """
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {}
        }

        # 追加并裁剪，保持最近 max_turns 轮（2 * max_turns 条消息）
        max_messages = self.max_turns * 2
        if self.backend == "redis":
            pipeline = self.redis_client.pipeline(transaction=True)
            pipeline.rpush(self.redis_key, json.dumps(message, ensure_ascii=False))
            pipeline.ltrim(self.redis_key, -max_messages, -1)
            pipeline.incr(self.redis_version_key)
            pipeline.expire(self.redis_key, self.redis_ttl_sec)
            pipeline.expire(self.redis_version_key, self.redis_ttl_sec)
            pipeline.execute()
        else:
            self.messages.append(message)
            self.messages = self.messages[-max_messages:]
            self.message_version += 1

        logger.debug(f"Added message to short-term memory: {role}")

    def get_recent_context(self, n_turns: int = None) -> List[Dict[str, Any]]:
        """
        获取最近 n 轮对话

        Args:
            n_turns: 获取轮数，默认为全部

        Returns:
            最近的消息列表
        """
        if n_turns is None:
            return self._get_messages()

        # 调用方请求的轮数不应超过系统配置的最大轮数
        if n_turns > self.max_turns:
            n_turns = self.max_turns

        # n轮 = 2n条消息
        n_messages = n_turns * 2
        return self._get_messages(limit=n_messages)

    def get_context_string(self, n_turns: int = 5) -> str:
        """
        获取最近对话的字符串表示

        Args:
            n_turns: 获取轮数

        Returns:
            格式化的对话字符串
        """
        messages = self.get_recent_context(n_turns)
        if not messages:
            return "无历史对话"

        lines = []
        for msg in messages:
            role_name = "用户" if msg["role"] == "user" else "助手"
            lines.append(f"{role_name}: {msg['content']}")

        return "\n".join(lines)

    def clear(self):
        """清空短期记忆"""
        if self.backend == "redis":
            self.redis_client.delete(self.redis_key, self.redis_version_key)
        else:
            self.messages.clear()
            self.message_version = 0
        logger.info("Short-term memory cleared")

    def replace_messages(self, messages: List[Dict[str, Any]], message_version: int = 0):
        """Replace the bounded cache from the PostgreSQL fact source."""
        max_messages = self.max_turns * 2
        bounded = [dict(message) for message in messages[-max_messages:]]
        if self.backend == "redis":
            pipeline = self.redis_client.pipeline(transaction=True)
            pipeline.delete(self.redis_key, self.redis_version_key)
            if bounded:
                pipeline.rpush(
                    self.redis_key,
                    *(json.dumps(message, ensure_ascii=False) for message in bounded),
                )
                pipeline.expire(self.redis_key, self.redis_ttl_sec)
            pipeline.set(self.redis_version_key, max(int(message_version), 0))
            pipeline.expire(self.redis_version_key, self.redis_ttl_sec)
            pipeline.execute()
        else:
            self.messages = bounded
            self.message_version = max(int(message_version), len(bounded))

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        if self.backend == "redis":
            total_messages = self.redis_client.llen(self.redis_key)
            message_version = int(self.redis_client.get(self.redis_version_key) or 0)
            oldest_message_time = None
            newest_message_time = None

            if total_messages > 0:
                first = self.redis_client.lindex(self.redis_key, 0)
                last = self.redis_client.lindex(self.redis_key, -1)
                if first:
                    oldest_message_time = json.loads(first).get("timestamp")
                if last:
                    newest_message_time = json.loads(last).get("timestamp")
        else:
            total_messages = len(self.messages)
            message_version = self.message_version
            oldest_message_time = self.messages[0].get("timestamp") if self.messages else None
            newest_message_time = self.messages[-1].get("timestamp") if self.messages else None

        return {
            "total_messages": total_messages,
            "message_version": message_version,
            "max_turns": self.max_turns,
            "backend": self.backend,
            "oldest_message_time": oldest_message_time,
            "newest_message_time": newest_message_time
        }

    def _get_messages(self, limit: int = None) -> List[Dict[str, Any]]:
        """从 Redis 获取消息列表（按时间顺序）"""
        if self.backend != "redis":
            if limit is None:
                return [dict(message) for message in self.messages]
            return [dict(message) for message in self.messages[-limit:]]

        if limit is None:
            raw_messages = self.redis_client.lrange(self.redis_key, 0, -1)
        else:
            raw_messages = self.redis_client.lrange(self.redis_key, -limit, -1)

        messages: List[Dict[str, Any]] = []
        for raw in raw_messages:
            try:
                messages.append(json.loads(raw))
            except (TypeError, json.JSONDecodeError):
                logger.warning("Skip invalid short-term memory record in Redis")
        return messages
