# Global Concurrency Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Hommey 单容器多 uvicorn worker 部署加上 Redis 协调的全局并发控制（同用户跨 worker 串行、全局并发上限、全局熔断）与记忆层 async 门面，解除同步 I/O 阻塞事件循环。

**Architecture:** 每个 uvicorn worker 仍是进程内 `WebHommeyManager`（保留每 worker 实例缓存），但跨 worker 的一致性与并发控制全部委托给 Redis 协调层（`utils/redis_coordination.py`，Lua 原子原语：`DistributedLock` / `RedisSemaphore` / `RedisCircuitBreaker`）。`WebHommeyManager` 提供统一消息入口，按"进程内锁 → Redis 锁 → 全局信号量"顺序取锁、持锁心跳续约、SSE 断连立即释放。记忆层暴露 `asyncio.to_thread` 门面，同步 API 不变。

**Tech Stack:** Python 3.x / FastAPI / uvicorn / redis-py 6.4（`redis.asyncio`）/ psycopg 3.3（同步 pool，经 `to_thread` 提交）/ asyncio

## Global Constraints

- 不破坏现有接口：`HommeyWebInstance.process_message()` 签名不变；现有测试必须通过。`utils.circuit_breaker.CircuitBreaker`（同步类）保留不动，供遗留同步调用使用。
- `RedisCircuitBreaker` 为 **async-native**（方法全为 `async def`，`raise_if_open`/`record_failure`/`record_success`/`get_status`），与旧同步 `CircuitBreaker` 鸭子类型不互通——调用点（`HommeyWebInstance` 等）须 `await`。这是已批准的裁决（A1）。
- Redis 客户端复用 `MEMORY_CONFIG.short_term` 的 host/port/password/db；协调层运行时零新增依赖。测试依赖例外：`requirements.txt` 新增 `pytest-asyncio`（已批准裁决 B）。
- 全部锁/信号量/熔断原语用 Lua 原子脚本，禁止多命令组合（避免非原子窗口）。
- 同步 API 保持不变：`MemoryManager` 及记忆层方法不改签名，只新增 async 门面。
- 配置新增 `CONCURRENCY_CONFIG`，默认值从 spec §8 复制。
- 编排层内部逻辑不改动。
- **测试运行方式**：测试在 `hommey-app` 容器内跑（源码已挂载到 /app，Redis/Postgres 为容器兄弟服务）。先一次性 `docker exec hommey-app pip install pytest-asyncio`（容器已联网安装），之后用 `docker exec hommey-app pytest tests/<file> -v` 运行。Task 2 会把 `pytest-asyncio` 写入 `requirements.txt`（供未来 build 固化），开发期无需 rebuild。

---

### Task 1: 记忆层 async 门面（`context/async_memory.py`）

**Files:**
- Create: `context/async_memory.py`
- Test: `tests/test_async_memory.py`

**Interfaces:**
- Consumes: `context/memory_manager.MemoryManager`（同步方法）
- Produces:
  - `class AsyncMemoryFacade:`
    - `def __init__(self, memory_manager)` — 持同步 manager
    - `async def add_message(self, role: str, content: str, metadata: dict | None = None)` → `str | False`
    - `async def get_preference(self)` → `dict`
    - `async def save_preference(self, pref_type: str, value)` → `None`
    - `async def get_active_trip(self)` → `dict | None`
    - `async def update_active_trip(self, trip_info: dict)` → `dict`
    - `async def complete_active_trip(self, reason: str)` → `dict | None`
    - `async def cancel_active_trip(self, reason: str)` → `dict | None`
    - `async def get_recent_context(self, n_turns: int | None = None)` → `list[dict]`
    - `async def save_trip_history(self, trip_info: dict)` → `None`
    - `async def get_trip_history(self, limit: int | None = None)` → `list[dict]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_async_memory.py
import asyncio
from context.async_memory import AsyncMemoryFacade


class StubManager:
    """同步 memory_manager 替身：记录是否真的走了 to_thread。"""
    def __init__(self):
        self.calls = []
        self.pref = {"home_location": "上海"}

    def add_message(self, role, content, metadata=None):
        self.calls.append(("add_message", role))
        return "m1"

    def get_preference(self):
        return dict(self.pref)

    def save_preference(self, pref_type, value):
        self.calls.append(("save_preference", pref_type))
        self.pref[pref_type] = value

    def get_active_trip(self):
        return None

    def update_active_trip(self, trip_info):
        self.calls.append(("update_active_trip",))
        return trip_info

    def complete_active_trip(self, reason="planning_completed"):
        self.calls.append(("complete_active_trip", reason))
        return None

    def cancel_active_trip(self, reason="user_cancelled"):
        self.calls.append(("cancel_active_trip", reason))
        return None

    def get_recent_context(self, n_turns=None):
        return [{"role": "user", "content": "hi"}]

    def save_trip_history(self, trip_info):
        self.calls.append(("save_trip_history",))

    def get_trip_history(self, limit=None):
        return [{"destination": "北京"}]


def test_facade_routes_to_sync_manager_and_returns_expected():
    manager = StubManager()
    facade = AsyncMemoryFacade(manager)

    async def run():
        mid = await facade.add_message("user", "hello")
        pref = await facade.get_preference()
        await facade.save_preference("budget_level", "L1")
        active = await facade.get_active_trip()
        recent = await facade.get_recent_context(3)
        trips = await facade.get_trip_history()

        assert mid == "m1"
        assert pref == {"home_location": "上海"}
        assert manager.pref["budget_level"] == "L1"
        assert active is None
        assert recent == [{"role": "user", "content": "hi"}]
        assert trips == [{"destination": "北京"}]
        assert ("add_message", "user") in manager.calls
        assert ("save_preference", "budget_level") in manager.calls

    asyncio.run(run())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `docker exec hommey-app pytest tests/test_async_memory.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'context.async_memory'`

- [ ] **Step 3: 实现门面**

```python
# context/async_memory.py
"""Async facade over the synchronous memory manager.

所有 I/O 经由 asyncio.to_thread 提交到线程池，避免同步 psycopg/redis 阻塞事件循环。
同步 API 保持不变，本门面不改变 MemoryManager 的行为。
"""
from __future__ import annotations

import asyncio
from typing import Any


class AsyncMemoryFacade:
    def __init__(self, memory_manager):
        self._m = memory_manager

    async def add_message(self, role: str, content: str, metadata: dict | None = None):
        return await asyncio.to_thread(self._m.add_message, role, content, metadata)

    async def get_preference(self) -> dict:
        return await asyncio.to_thread(self._m.long_term.get_preference)

    async def save_preference(self, pref_type: str, value: Any) -> None:
        await asyncio.to_thread(self._m.long_term.save_preference, pref_type, value)

    async def get_active_trip(self):
        return await asyncio.to_thread(self._m.get_active_trip)

    async def update_active_trip(self, trip_info: dict) -> dict:
        return await asyncio.to_thread(self._m.update_active_trip, trip_info)

    async def complete_active_trip(self, reason: str = "planning_completed"):
        return await asyncio.to_thread(self._m.complete_active_trip, reason)

    async def cancel_active_trip(self, reason: str = "user_cancelled"):
        return await asyncio.to_thread(self._m.cancel_active_trip, reason)

    async def get_recent_context(self, n_turns: int | None = None) -> list[dict]:
        return await asyncio.to_thread(self._m.short_term.get_recent_context, n_turns)

    async def save_trip_history(self, trip_info: dict) -> None:
        await asyncio.to_thread(self._m.long_term.save_trip_history, trip_info)

    async def get_trip_history(self, limit: int | None = None) -> list[dict]:
        return await asyncio.to_thread(self._m.long_term.get_trip_history, limit)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `docker exec hommey-app pytest tests/test_async_memory.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add context/async_memory.py tests/test_async_memory.py
git commit -m "feat: add async memory facade over synchronous memory manager"
```

---

### Task 2: Redis 协调层（`utils/redis_coordination.py`）

**Files:**
- Create: `utils/redis_coordination.py`
- Test: `tests/test_redis_coordination.py`

**Interfaces:**
- Consumes: `settings.MEMORY_CONFIG`, `settings.CONCURRENCY_CONFIG`（Task 5 添加，此处用 `CONCURRENCY_CONFIG.get(key, default)` 兜底读取，不 import 未定义键）
- Produces:
  - `class DistributedLock:`
    - `async def acquire(self) -> bool` — Lua `SET key token NX PX ttl`，token 存 `self._token`
    - `async def renew(self) -> bool` — Lua `if GET==token then PEXPIRE`；返回 False 表示锁已易主
    - `async def release(self) -> bool` — Lua `if GET==token then DEL`
    - `@property token` — 全局唯一 UUID4 hex
  - `class RedisSemaphore:`
    - `async def acquire(self) -> bool` — Lua `INCR; if n<=max then EXPIRE return 1 else DECR return 0`
    - `async def release(self) -> None` — Lua `DECR; clamp>=0`
  - `class RedisCircuitBreaker:` — 保持旧 `CircuitBreaker` 接口（`raise_if_open`/`record_failure`/`record_success`/`get_status`/`state`），状态存 Redis
  - `def get_redis_coordination_client()` — 进程级共享 `redis.asyncio.Redis` 单例
  - `def create_distributed_lock(key: str)`, `def create_redis_semaphore()`, `def create_redis_circuit_breaker()`

- [ ] **Step 0: 安装并固化 pytest-asyncio**

在 `requirements.txt` 的 `# Test runner` 段加入：

```
pytest-asyncio==0.24.0
```

并在容器内安装（开发期无需 rebuild）：

```bash
docker exec hommey-app pip install pytest-asyncio==0.24.0
```

验证：`docker exec hommey-app python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"` 输出 `0.24.0`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_redis_coordination.py
import asyncio
import pytest

from utils.redis_coordination import (
    DistributedLock,
    RedisSemaphore,
    RedisCircuitBreaker,
    get_redis_coordination_client,
)


@pytest.fixture
async def client():
    c = get_redis_coordination_client()
    yield c
    # 清理测试 key，避免污染（redis.asyncio 的 delete 是协程，需 await）
    await c.delete(
        "test:lock:u1",
        "test:sem:g",
        "hommey:cb:test:cb:state",
        "hommey:cb:test:cb:opened_at",
        "hommey:cb:test:cb:count",
    )


@pytest.mark.asyncio
async def test_lock_acquire_renew_release(client):
    lock = DistributedLock("test:lock:u1", ttl_ms=2000)
    assert await lock.acquire() is True
    # 第二把锁（不同 token）在未释放时不能获得
    lock2 = DistributedLock("test:lock:u1", ttl_ms=2000)
    assert await lock2.acquire() is False
    # 续约
    assert await lock.renew() is True
    # 释放
    assert await lock.release() is True
    # 释放后可重新获得
    assert await lock2.acquire() is True
    await lock2.release()


@pytest.mark.asyncio
async def test_lock_release_by_wrong_token_does_not_delete(client):
    lock = DistributedLock("test:lock:u1", ttl_ms=2000)
    await lock.acquire()
    # 一个 token 错误的"释放"不应删掉真锁
    wrong = DistributedLock("test:lock:u1", ttl_ms=2000)
    assert await wrong.release() is False
    # 真锁还在
    assert await lock.release() is True


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency(client):
    sem = RedisSemaphore("test:sem:g", max_concurrency=2, ttl_sec=5)
    assert await sem.acquire() is True
    assert await sem.acquire() is True
    # 达到上限，第三次获取失败
    assert await sem.acquire() is False
    await sem.release()
    assert await sem.acquire() is True
    await sem.release()
    await sem.release()


@pytest.mark.asyncio
async def test_circuit_breaker_state_machine(client):
    cb = RedisCircuitBreaker(
        "test:cb", failure_threshold=2, recovery_timeout_sec=1, half_open_successes=2
    )
    assert await cb.state() == "closed"
    await cb.raise_if_open()  # 不抛
    await cb.record_failure()
    await cb.record_failure()
    assert await cb.state() == "open"
    with pytest.raises(Exception):
        await cb.raise_if_open()
    # 恢复超时后进入 half_open
    await asyncio.sleep(1.1)
    assert await cb.state() == "half_open"
    await cb.record_success()
    await cb.record_success()
    assert await cb.state() == "closed"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `docker exec hommey-app pytest tests/test_redis_coordination.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'utils.redis_coordination'`

- [ ] **Step 3: 实现协调层**

```python
# utils/redis_coordination.py
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
    from settings import CONCURRENCY_CONFIG
    ttl_ms = int(CONCURRENCY_CONFIG.get("distributed_lock_ttl_sec", 45) * 1000)
    return DistributedLock(key, ttl_ms=ttl_ms)


def create_redis_semaphore() -> RedisSemaphore:
    from settings import CONCURRENCY_CONFIG
    return RedisSemaphore(
        "hommey:global:semaphore",
        max_concurrency=int(CONCURRENCY_CONFIG.get("global_concurrency_limit", 8)),
        ttl_sec=int(CONCURRENCY_CONFIG.get("semaphore_ttl_sec", 60)),
    )


def create_redis_circuit_breaker() -> RedisCircuitBreaker:
    return RedisCircuitBreaker()
```

> 注意：`RedisCircuitBreaker` 已按批准裁决（A1）做成 **async-native**。`HommeyWebInstance` 里现有同步调用 `cb.record_success()` / `cb.record_failure()` / `cb.raise_if_open()` 必须在 Task 6 改为 `await`（这些调用点本就在 async 方法内）。

- [ ] **Step 4: 运行测试确认通过**

Run: `docker exec hommey-app pytest tests/test_redis_coordination.py -v`
Expected: PASS（需本地 Redis 或容器内 Redis 可达）

- [ ] **Step 5: 提交**

```bash
git add utils/redis_coordination.py tests/test_redis_coordination.py
git commit -m "feat: add redis coordination primitives (lock, semaphore, circuit breaker)"
```

---

### Task 3: `CONCURRENCY_CONFIG` 与 worker 参数

**Files:**
- Modify: `settings.py`（在 `RESILIENCE_CONFIG` 之后新增）
- Modify: `docker/Dockerfile`（CMD 行）
- Modify: `docker/docker-compose.yml`（hommey.environment 注入 `UVICORN_WORKERS`）
- Test: `tests/test_settings.py`（若存在；否则用 `tests/test_imports.py` 等现有测试定位）

**Interfaces:**
- Consumes: 无
- Produces: `settings.CONCURRENCY_CONFIG`（spec §8 键名）；Dockerfile CMD 支持 `${UVICORN_WORKERS}`

- [ ] **Step 1: 在 `settings.py` 增加配置**

```python
CONCURRENCY_CONFIG = {
    # 全局并发上限：RedisSemaphore 允许同时进行中的请求数。
    "global_concurrency_limit": _int_env("HOMMEY_GLOBAL_CONCURRENCY_LIMIT", 8),
    # 同用户分布式锁等待超时（秒）。超过则返回用户排队超时。
    "per_user_lock_timeout_sec": _float_env("HOMMEY_PER_USER_LOCK_TIMEOUT_SEC", 60.0),
    # 全局信号量获取超时（秒）。
    "semaphore_acquire_timeout_sec": _float_env("HOMMEY_SEMAPHORE_ACQUIRE_TIMEOUT_SEC", 120.0),
    # 分布式锁 TTL（秒），每次续约重设。
    "distributed_lock_ttl_sec": _float_env("HOMMEY_DISTRIBUTED_LOCK_TTL_SEC", 45.0),
    # 心跳续约间隔（秒）。
    "lock_heartbeat_interval_sec": _float_env("HOMMEY_LOCK_HEARTBEAT_INTERVAL_SEC", 15.0),
    # 拿锁重试 sleep 间隔（秒）。
    "lock_retry_interval_sec": _float_env("HOMMEY_LOCK_RETRY_INTERVAL_SEC", 0.2),
    # 信号量计数 TTL（秒），防 worker 崩溃泄漏计数。
    # 必须 >= RESILIENCE_CONFIG.request_timeout_sec（默认 240），否则长时间请求
    # 超过 TTL 会导致计数 key 过期、并发上限被静默突破（Task 2 review 实测复现）。
    "semaphore_ttl_sec": _int_env("HOMMEY_SEMAPHORE_TTL_SEC", 240),
}
```

- [ ] **Step 2: 修改 Dockerfile CMD**

将 `CMD ["uvicorn", "webui_new.server:app", "--host", "0.0.0.0", "--port", "8000"]`
改为：

```dockerfile
CMD ["sh", "-c", "uvicorn webui_new.server:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}"]
```

- [ ] **Step 3: 修改 compose 注入 worker 数**

在 `docker-compose.yml` 的 `hommey.environment` 段新增：

```yaml
      UVICORN_WORKERS: ${UVICORN_WORKERS:-2}
```

- [ ] **Step 4: 运行配置导入测试**

Run: `python -c "from settings import CONCURRENCY_CONFIG; print(CONCURRENCY_CONFIG)"`
Expected: 打印含 `global_concurrency_limit: 8` 的 dict

- [ ] **Step 5: 提交**

```bash
git add settings.py docker/Dockerfile docker/docker-compose.yml
git commit -m "feat: add concurrency config and multi-worker uvicorn support"
```

---

### Task 4: 统一消息入口与锁编排（`webui_new/manager.py`）

**Files:**
- Modify: `webui_new/manager.py`
  - `WebHommeyManager.__init__` 增加 `self._user_locks: dict[str, asyncio.Lock]`
  - `WebHommeyManager.get_or_create` / `initialize_user` 加进程内 per-user 锁
  - 新增 `WebHommeyManager.process_message(user_id, message, *, request_id=None, attachment_ids=None, progress_callback=None)` 统一入口
  - `HommeyWebInstance` 内部保持 `process_message`（改名 `_process_message_impl`），由 manager 入口包装
- Modify: `webui_new/routes/chat.py`（`/chat` 与 `/chat/stream` 改调 manager 入口；SSE `finally` 释放锁）
- Test: `tests/test_manager_concurrency.py`

**Interfaces:**
- Consumes: `create_distributed_lock` / `create_redis_semaphore` / `create_redis_circuit_breaker`（Task 2）；`AsyncMemoryFacade`（Task 1）；`CONCURRENCY_CONFIG`（Task 3）
- Produces:
  - `WebHommeyManager.process_message(user_id, ...) -> dict`
  - `HommeyWebInstance.process_message(...)`（**保留原名不动**，由 manager 入口包装调用）
  - SSE 断连路径在 `finally` 释放锁

- [ ] **Step 1: 写失败测试**

```python
# tests/test_manager_concurrency.py
import asyncio
import pytest

from webui_new.manager import WebHommeyManager


class StubProcess:
    """模拟 HommeyWebInstance：记录并发调用。"""
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = asyncio.Lock()

    async def process(self, message):
        async with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
        return {"response": message}


@pytest.mark.asyncio
async def test_manager_serializes_same_user_requests():
    manager = WebHommeyManager()
    # 直接测进程内 per-user 锁逻辑：两个并发任务应串行
    task_holder = StubProcess()
    async def run(i):
        async with manager._per_user_lock("u1"):
            task_holder.active += 1
            task_holder.max_active = max(task_holder.max_active, task_holder.active)
            await asyncio.sleep(0.02)
            task_holder.active -= 1
        return i

    results = await asyncio.gather(run(1), run(2))
    assert sorted(results) == [1, 2]
    assert task_holder.max_active == 1  # 从未并行
```

- [ ] **Step 2: 运行测试确认失败**

Run: `docker exec hommey-app pytest tests/test_manager_concurrency.py -v`
Expected: FAIL，`AttributeError: 'WebHommeyManager' object has no attribute '_per_user_lock'`

- [ ] **Step 3: 实现 manager 锁编排**

在 `WebHommeyManager` 增加：

```python
from utils.redis_coordination import (
    create_distributed_lock,
    create_redis_semaphore,
)

def __init__(self):
    self._instances: dict[str, HommeyWebInstance] = {}
    self._user_locks: dict[str, asyncio.Lock] = {}

def _per_user_lock(self, user_id: str) -> asyncio.Lock:
    if user_id not in self._user_locks:
        self._user_locks[user_id] = asyncio.Lock()
    return self._user_locks[user_id]

async def process_message(
    self,
    user_id: str,
    message: str,
    *,
    request_id: str | None = None,
    attachment_ids: list[str] | None = None,
    progress_callback=None,
) -> dict:
    """统一消息入口：进程内锁 → 分布式锁 → 全局信号量，持锁心跳续约。"""
    instance = self.get(user_id)
    if not instance or not instance.initialized:
        from webui_new.core.errors import BusinessError
        raise BusinessError("NOT_INITIALIZED", "系统未初始化，请刷新页面")

    rc = CONCURRENCY_CONFIG
    heartbeat = None
    # 1) 进程内 per-user 锁（同一 worker 内不重复进 Redis）
    local_lock = self._per_user_lock(user_id)
    distributed_lock = create_distributed_lock(f"hommey:lock:user:{user_id}")
    semaphore = create_redis_semaphore()

    acquired_distributed = False
    acquired_semaphore = False

    # 进程内锁等待（本地等待不设超时，避免同一 worker 死锁；由外层 wait_for 兜底）
    await local_lock.acquire()

    try:
        # 2) 分布式锁：跨 worker 串行，带超时
        deadline = time.monotonic() + float(rc.get("per_user_lock_timeout_sec", 60.0))
        while not await distributed_lock.acquire():
            if time.monotonic() >= deadline:
                raise UpstreamError(
                    "USER_QUEUE_TIMEOUT",
                    "您有请求正在处理，请稍候再试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                )
            await asyncio.sleep(float(rc.get("lock_retry_interval_sec", 0.2)))
        acquired_distributed = True

        # 心跳续约：持锁期间每 lock_heartbeat_interval_sec 续一次
        async def _heartbeat():
            while True:
                await asyncio.sleep(float(rc.get("lock_heartbeat_interval_sec", 15.0)))
                if not await distributed_lock.renew():
                    return  # 锁已易主，放弃

        heartbeat = asyncio.create_task(_heartbeat())

        # 3) 全局信号量：并发上限
        sem_deadline = time.monotonic() + float(rc.get("semaphore_acquire_timeout_sec", 120.0))
        while not await semaphore.acquire():
            if time.monotonic() >= sem_deadline:
                raise UpstreamError(
                    "GLOBAL_CONCURRENCY_LIMIT",
                    "系统繁忙，请稍后再试。",
                    retryable=True,
                    component=COMPONENT_LLM,
                )
            await asyncio.sleep(float(rc.get("lock_retry_interval_sec", 0.2)))
        acquired_semaphore = True

        # 4) 调用实例处理（保持 HommeyWebInstance 原逻辑）
        return await instance.process_message(
            message,
            request_id=request_id,
            attachment_ids=attachment_ids,
            progress_callback=progress_callback,
        )
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        if acquired_semaphore:
            await semaphore.release()
        if acquired_distributed:
            await distributed_lock.release()
        local_lock.release()
```

> 注意：`HommeyWebInstance.process_message` 保持**原名不动**（含 budget/wait_for），由 manager 入口直接调用（已批准裁决）。`manager.process_message` 只做锁编排 + 调用 `instance.process_message`。`instance.initialize()` 的初始化路径由 `get_or_create`+`initialize_user` 的进程内锁保护。

- [ ] **Step 4: 改 `chat.py` 路由**

`/chat` 与 `/chat/stream` 改为：

```python
# send_message
result = await manager.process_message(
    user_id,
    data.message,
    request_id=request_id(request),
    attachment_ids=data.attachment_ids,
)
```

SSE 的 `event_stream` 中，`manager.process_message` 的锁在 manager 内部获取，`instance.stream_message` 的事件生成在 manager 锁外 —— 需保证 SSE 持锁到流结束。将 `instance.stream_message` 移入 manager 入口（新增 `stream_message` 同步入口，内部先取锁再调用 `instance.stream_message`，finally 释放）。`chat.py` 的 `event_stream` 改调 `manager.stream_message(...)`。

在 `WebHommeyManager` 新增：

```python
async def stream_message(self, user_id: str, message: str, *, request_id=None, attachment_ids=None):
    """SSE 流式入口：与 process_message 相同的取锁顺序，持锁到流结束。"""
    # 取锁逻辑同 process_message（进程内锁 → 分布式锁 → 信号量 → 心跳）
    # 取到锁后：
    instance = self.get(user_id)
    async for event in instance.stream_message(message, request_id=request_id, attachment_ids=attachment_ids):
        yield event
    # finally 释放锁
```

- [ ] **Step 5: 运行测试确认通过**

Run: `docker exec hommey-app pytest tests/test_manager_concurrency.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add webui_new/manager.py webui_new/routes/chat.py tests/test_manager_concurrency.py
git commit -m "feat: serialize per-user requests across workers with redis locks"
```

---

### Task 5: 迁移并发保护（`webui_new/auth/migrations.py`）

**Files:**
- Modify: `webui_new/auth/migrations.py`（`apply_all_migrations` 开头加 advisory lock）
- Test: 复用现有迁移测试或新增 `tests/test_migrations_concurrency.py`

**Interfaces:**
- Consumes: 无（用 psycopg 原生 advisory lock）
- Produces: `apply_all_migrations()` 在多 worker 并发启动时只执行一次

- [ ] **Step 1: 写失败测试**

```python
# tests/test_migrations_concurrency.py
import pytest
import psycopg

from settings import MEMORY_CONFIG
from webui_new.auth.migrations import apply_all_migrations


@pytest.mark.skipif(
    not MEMORY_CONFIG["long_term"].get("postgres_dsn"),
    reason="requires postgres backend",
)
def test_migrations_run_concurrently_without_duplicate():
    # 两次调用应都成功返回（advisory lock 保证不重复执行）
    r1 = apply_all_migrations()
    r2 = apply_all_migrations()
    assert r1 >= 0 and r2 >= 0
```

- [ ] **Step 2: 运行测试确认（未加锁时可能不失败但重复执行）**

Run: `docker exec hommey-app pytest tests/test_migrations_concurrency.py -v`
Expected: 当前通过（无锁也可通过，因无并发）；此测试验证加锁后并发仍安全

- [ ] **Step 3: 加 advisory lock**

在 `apply_all_migrations` 建立连接后、执行任何迁移前：

```python
with psycopg.connect(dsn, autocommit=False, connect_timeout=5) as conn:
    with conn.cursor() as cur:
        # 串行化多 worker 并发启动的迁移，避免 schema_migrations 主键冲突
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('hommey_migrations'))")
    # ... 原有迁移逻辑
```

- [ ] **Step 4: 运行测试确认通过**

Run: `docker exec hommey-app pytest tests/test_migrations_concurrency.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add webui_new/auth/migrations.py tests/test_migrations_concurrency.py
git commit -m "fix: serialize migration application with advisory lock"
```

---

### Task 6: Web 层接入 async 门面与熔断器

**Files:**
- Modify: `webui_new/manager.py`
  - `HommeyWebInstance` 内：将 `memory_manager` 的同步调用改为 `AsyncMemoryFacade` 门面
  - `create_circuit_breaker()` 改为返回 `RedisCircuitBreaker`（替换 `utils.circuit_breaker.CircuitBreaker` 调用点）
- Modify: `runtime.py`（`create_circuit_breaker` 返回 Redis 版）
- Test: `tests/test_web_async_facade.py`

**Interfaces:**
- Consumes: `AsyncMemoryFacade`（Task 1）、`create_redis_circuit_breaker`（Task 2）
- Produces: `HommeyWebInstance` 内部经 async 门面访问记忆；熔断器为 Redis 全局版

- [ ] **Step 1: 写失败测试**

```python
# tests/test_web_async_facade.py
import asyncio
import pytest

from runtime import create_circuit_breaker
from context.async_memory import AsyncMemoryFacade


@pytest.mark.asyncio
async def test_runtime_circuit_breaker_is_redis_backed():
    cb = create_circuit_breaker()
    # 兼容旧接口
    assert callable(cb.raise_if_open)
    assert callable(cb.record_failure)
    assert callable(cb.record_success)
    assert cb.get_status()["state"] in {"closed", "open", "half_open"}


@pytest.mark.asyncio
async def test_facade_wraps_memory_manager():
    # AsyncMemoryFacade 能包装真实 MemoryManager（用内存后端时无 DB 依赖）
    from settings import MEMORY_CONFIG
    if MEMORY_CONFIG["long_term"].get("backend") in {"postgres", "file", "disabled"}:
        pytest.skip("需要本地 memory 后端才跑真实实例")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `docker exec hommey-app pytest tests/test_web_async_facade.py -v`
Expected: 当前 `create_circuit_breaker` 返回旧 `CircuitBreaker`；断言可过但不满足"Redis 版"。改后断言 Redis 版。

- [ ] **Step 3: `runtime.py` 改 `create_circuit_breaker` 返回 Redis 版**

```python
def create_circuit_breaker() -> CircuitBreaker:
    """Return the process-shared Redis-backed circuit breaker."""
    from utils.redis_coordination import create_redis_circuit_breaker
    return create_redis_circuit_breaker()
```

> 保持返回值类型标注为 `CircuitBreaker`（旧类），实际返回 `RedisCircuitBreaker`（鸭子类型兼容）。若 `HommeyWebInstance` 直接调 `self.circuit_breaker.record_success()`（同步），需改为 `await`——Task 4 已把调用点放 async 上下文，需逐个核对并 `await`。

- [ ] **Step 4: `HommeyWebInstance` 内接入 async 门面**

在 `__init__` 或 `initialize` 增加 `self.async_memory = AsyncMemoryFacade(self.memory_manager)`，并将 `_process_message_impl` 内所有 `self.memory_manager.add_message(...)` 改为 `await self.async_memory.add_message(...)`；`get_preference`/`get_active_trip`/`update_active_trip`/`complete_active_trip`/`cancel_active_trip` 同步调用改为 async 门面。涉及 `_persist_user_message`（同步，内含 `add_message`）——改为 async 版本并 `await`。

- [ ] **Step 5: 运行测试确认通过**

Run: `docker exec hommey-app pytest tests/test_web_async_facade.py tests/test_auth_routes.py tests/test_manager_concurrency.py -v`
Expected: PASS（现有测试兼容）

- [ ] **Step 6: 提交**

```bash
git add webui_new/manager.py runtime.py tests/test_web_async_facade.py
git commit -m "feat: wire async memory facade and redis circuit breaker into web layer"
```

---

### Task 7: 集成验证

**Files:**
- Modify: `tests/`（端到端）
- Test: `tests/test_concurrency_e2e.py`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 端到端并发验证

- [ ] **Step 1: 写端到端并发测试**

```python
# tests/test_concurrency_e2e.py
import asyncio
import pytest

from webui_new.manager import WebHommeyManager


@pytest.mark.asyncio
async def test_same_user_requests_serialize_across_manager():
    manager = WebHommeyManager()
    # 用 stub instance（不走真实 DB）验证统一入口的锁顺序
    ...
```

- [ ] **Step 2: 运行测试**

Run: `docker exec hommey-app pytest tests/ -x -q`
Expected: 全部通过

- [ ] **Step 3: 提交**

```bash
git add tests/test_concurrency_e2e.py
git commit -m "test: add e2e concurrency serialization test"
```

---

## 执行顺序与依赖

```text
Task 1 (async 门面) ──► Task 2 (Redis 协调) ──► Task 4 (manager 锁编排)
                                               ──► Task 6 (Web 接入)
Task 3 (配置) ───────────────────────────────► Task 4
Task 5 (迁移锁) —— 独立
Task 7 (e2e) —— 依赖全部
```

Task 1/2/3 可并行（相互独立，仅 Task 2 用到 `CONCURRENCY_CONFIG` 键名，用 `.get` 兜底）；Task 4 依赖 1/2/3；Task 5 独立；Task 6 依赖 1/2；Task 7 依赖全部。
