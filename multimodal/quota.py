"""Per-user daily quota for paid external calls (vision / ASR).

图片理解与语音转写发生在聊天请求之外（上传路由、/asr 路由），不落在
``execution_budget`` 的请求级预算内。这里提供独立的每用户每日计数，
防止绕过聊天预算刷外部 API。

优先使用 Redis 计数（跨 worker 一致）；Redis 不可用时降级为进程内计数
（单 worker 场景够用，会打一条警告日志）。
"""
from __future__ import annotations

import logging
from datetime import date
from threading import Lock

try:
    import redis as _redis_lib  # type: ignore
except ImportError:  # pragma: no cover - 依赖缺失分支
    _redis_lib = None

logger = logging.getLogger(__name__)


def redis_config_from_settings() -> dict:
    """从短期记忆的 Redis 配置构建配额计数用的 Redis 参数（与记忆分 key 不冲突）。"""
    from settings import MEMORY_CONFIG

    st = MEMORY_CONFIG["short_term"]
    return {
        "enabled": str(st.get("backend", "")).lower() == "redis",
        "host": st.get("redis_host", "127.0.0.1"),
        "port": int(st.get("redis_port", 6379)),
        "db": int(st.get("redis_db", 0)),
        "password": st.get("redis_password"),
    }


class DailyQuota:
    """按 (namespace, user_id, 日期) 计数的每日配额。"""

    def __init__(self, namespace: str, limit: int, redis_config: dict | None = None):
        self.namespace = namespace
        self.limit = int(limit)
        self.redis_config = redis_config or {}
        self._client = None
        self._local: dict[tuple[str, str], int] = {}
        self._lock = Lock()
        self._warned = False

    def _redis(self):
        if self._client is None and _redis_lib is not None:
            try:
                self._client = _redis_lib.Redis(
                    host=self.redis_config.get("host", "127.0.0.1"),
                    port=int(self.redis_config.get("port", 6379)),
                    db=int(self.redis_config.get("db", 0)),
                    password=self.redis_config.get("password") or None,
                    socket_connect_timeout=1.0,
                )
            except Exception:
                self._client = None
        return self._client

    def _redis_key(self, user_id: str) -> str:
        return f"hommey:{self.namespace}:quota:{date.today().isoformat()}:{user_id}"

    def consume(self, user_id: str) -> bool:
        """计数 +1；返回是否允许本次调用（False = 已超每日配额）。"""
        if self.limit <= 0:
            return True
        client = self._redis()
        if client is not None:
            try:
                key = self._redis_key(user_id)
                pipe = client.pipeline()
                pipe.incr(key)
                pipe.expire(key, 86400)
                count, _ = pipe.execute()
                return int(count) <= self.limit
            except Exception:
                # Redis 瞬时故障时降级为进程内计数，不阻断主流程。
                pass
        with self._lock:
            key = (user_id, date.today().isoformat())
            count = self._local.get(key, 0) + 1
            self._local[key] = count
            if not self._warned:
                self._warned = True
                logger.warning("DailyQuota[%s] 回退进程内计数（Redis 不可用）", self.namespace)
            return count <= self.limit
