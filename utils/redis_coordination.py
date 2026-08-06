"""Redis-backed coordination primitives for multi-worker concurrency control.

全部原语用 Lua 原子脚本实现，避免 INCR/EXPIRE 等非原子窗口。
复用 MEMORY_CONFIG.short_term 的 Redis 连接配置。
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis

from settings import MEMORY_CONFIG

logger = logging.getLogger(__name__)

_client = None


def get_redis_coordination_client():
    global _client
    if _client is None:
        cfg = MEMORY_CONFIG.get("short_term", {})
        _client = aioredis.Redis(
            host=cfg.get("redis_host", "127.0.0.1"),
            port=int(cfg.get("redis_port", 6379)),
            db=int(cfg.get("redis_db", 0)),
            password=cfg.get("redis_password"),
            decode_responses=True,
        )
    return _client


# ── DistributedLock ────────────────────────────────────────────────

_ACQUIRE_LUA = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
    return 1
end
return 0
"""

_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class DistributedLock:
    def __init__(self, key: str, ttl_ms: int = 45000):
        self._client = get_redis_coordination_client()
        self._key = key
        self._ttl_ms = int(ttl_ms)
        self._token = uuid.uuid4().hex

    @property
    def token(self) -> str:
        return self._token

    async def acquire(self) -> bool:
        return bool(
            await self._client.eval(_ACQUIRE_LUA, 1, self._key, self._token, self._ttl_ms)
        )

    async def renew(self) -> bool:
        return bool(await self._client.eval(_RENEW_LUA, 1, self._key, self._token, self._ttl_ms))

    async def release(self) -> bool:
        return bool(await self._client.eval(_RELEASE_LUA, 1, self._key, self._token))


# ── RedisSemaphore ─────────────────────────────────────────────────

_SEM_ACQUIRE_LUA = """
local n = redis.call('INCR', KEYS[1])
if n <= tonumber(ARGV[1]) then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    return 1
end
redis.call('DECR', KEYS[1])
return 0
"""

_SEM_RELEASE_LUA = """
local n = redis.call('DECR', KEYS[1])
if n < 0 then
    redis.call('SET', KEYS[1], 0)
end
return 1
"""


class RedisSemaphore:
    def __init__(self, key: str, max_concurrency: int = 8, ttl_sec: int = 60):
        self._client = get_redis_coordination_client()
        self._key = key
        self._max = max(1, int(max_concurrency))
        self._ttl_sec = max(1, int(ttl_sec))

    async def acquire(self) -> bool:
        return bool(
            await self._client.eval(
                _SEM_ACQUIRE_LUA, 1, self._key, self._max, self._ttl_sec
            )
        )

    async def release(self) -> None:
        await self._client.eval(_SEM_RELEASE_LUA, 1, self._key)


# ── RedisCircuitBreaker ────────────────────────────────────────────

_CB_STATE_LUA = """
local state = redis.call('GET', KEYS[1])
local opened_at = tonumber(redis.call('GET', KEYS[2]) or '0')
local now = tonumber(ARGV[1])
local recovery = tonumber(ARGV[2])
if state == 'open' and opened_at > 0 and (now - opened_at) >= recovery then
    redis.call('SET', KEYS[1], 'half_open')
    redis.call('SET', KEYS[3], 0)
    state = 'half_open'
end
return state or 'closed'
"""

_CB_FAILURE_LUA = """
local threshold = tonumber(ARGV[1])
local state = redis.call('GET', KEYS[1])
if state == 'open' then return 'open' end
if state == 'half_open' then
    redis.call('SET', KEYS[1], 'open')
    redis.call('SET', KEYS[2], ARGV[2])
    return 'open'
end
local n = redis.call('INCR', KEYS[3])
if n >= threshold then
    redis.call('SET', KEYS[1], 'open')
    redis.call('SET', KEYS[2], ARGV[2])
    return 'open'
end
return 'closed'
"""

_CB_SUCCESS_LUA = """
local half = tonumber(ARGV[1])
local state = redis.call('GET', KEYS[1])
if state == 'half_open' then
    local n = redis.call('INCR', KEYS[3])
    if n >= half then
        redis.call('SET', KEYS[1], 'closed')
        redis.call('SET', KEYS[3], 0)
        return 'closed'
    end
    return 'half_open'
end
redis.call('SET', KEYS[3], 0)
return state or 'closed'
"""


class RedisCircuitBreaker:
    """Async-native, Redis-backed circuit breaker shared across workers.

    全部方法为 async；调用方须 await。与旧的同步 `utils.circuit_breaker.CircuitBreaker`
    不互通，旧类保留给遗留同步调用。
    """

    def __init__(
        self,
        name: str = "hommey",
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 60.0,
        half_open_successes: int = 2,
    ):
        from settings import RESILIENCE_CONFIG

        rc = RESILIENCE_CONFIG
        self._client = get_redis_coordination_client()
        self.failure_threshold = int(
            failure_threshold or rc.get("circuit_failure_threshold", 5)
        )
        self.recovery_timeout_sec = float(
            recovery_timeout_sec or rc.get("circuit_recovery_timeout_sec", 60.0)
        )
        self.half_open_successes = int(
            half_open_successes or rc.get("circuit_half_open_successes", 2)
        )
        self._state_key = f"hommey:cb:{name}:state"
        self._opened_at_key = f"hommey:cb:{name}:opened_at"
        self._count_key = f"hommey:cb:{name}:count"

    async def state(self) -> str:
        return await self._client.eval(
            _CB_STATE_LUA,
            3,
            self._state_key,
            self._opened_at_key,
            self._count_key,
            time.time(),
            self.recovery_timeout_sec,
        )

    async def allow_call(self) -> bool:
        return await self.state() != "open"

    async def raise_if_open(self):
        if not await self.allow_call():
            from utils.circuit_breaker import CircuitOpenError
            raise CircuitOpenError("服务暂时不可用，请稍后再试")

    async def record_failure(self):
        return await self._client.eval(
            _CB_FAILURE_LUA,
            3,
            self._state_key,
            self._opened_at_key,
            self._count_key,
            self.failure_threshold,
            time.time(),
        )

    async def record_success(self):
        return await self._client.eval(
            _CB_SUCCESS_LUA,
            3,
            self._state_key,
            self._opened_at_key,
            self._count_key,
            self.half_open_successes,
        )

    async def get_status(self) -> dict:
        return {
            "state": await self.state(),
            "failure_count": int(await self._client.get(self._count_key) or 0),
            "last_failure_time": None,
            "opened_at": await self._client.get(self._opened_at_key),
        }


# ── factory helpers ────────────────────────────────────────────────

def create_distributed_lock(key: str) -> DistributedLock:
    import settings
    cc = getattr(settings, "CONCURRENCY_CONFIG", {})
    ttl_ms = int(cc.get("distributed_lock_ttl_sec", 45) * 1000)
    return DistributedLock(key, ttl_ms=ttl_ms)


def create_redis_semaphore() -> RedisSemaphore:
    import settings
    cc = getattr(settings, "CONCURRENCY_CONFIG", {})
    return RedisSemaphore(
        "hommey:global:semaphore",
        max_concurrency=int(cc.get("global_concurrency_limit", 8)),
        ttl_sec=int(cc.get("semaphore_ttl_sec", 60)),
    )


def create_redis_circuit_breaker() -> RedisCircuitBreaker:
    return RedisCircuitBreaker()
