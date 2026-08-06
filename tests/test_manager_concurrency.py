# tests/test_manager_concurrency.py
import asyncio
import pytest

import webui_new.manager as manager_module
from webui_new.core.errors import UpstreamError
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


class FakeDistributedLock:
    """模拟 create_distributed_lock 返回的分布式锁。"""
    def __init__(self, key, acquire_result=True, renew_result=True, renew_exc=None):
        self.key = key
        self.acquire_result = acquire_result
        self.renew_result = renew_result
        self.renew_exc = renew_exc
        self.acquire_calls = 0
        self.released = False

    async def acquire(self):
        self.acquire_calls += 1
        return self.acquire_result

    async def renew(self):
        if self.renew_exc is not None:
            raise self.renew_exc
        return self.renew_result

    async def release(self):
        self.released = True


class FakeSemaphore:
    """模拟 create_redis_semaphore 返回的全局信号量。"""
    def __init__(self, acquire_result=True):
        self.acquire_result = acquire_result
        self.acquire_calls = 0
        self.released = False

    async def acquire(self):
        self.acquire_calls += 1
        return self.acquire_result

    async def release(self):
        self.released = True


class FakeInstance:
    """已初始化的实例替身：转发到给定处理函数，接受 manager 传入的全部 kwargs。"""
    def __init__(self, process_fn):
        self.initialized = True
        self._process_fn = process_fn

    async def process_message(self, message, request_id=None, attachment_ids=None, progress_callback=None):
        return await self._process_fn(message)


def _mock_redis(monkeypatch, lock=None, sem=None):
    """monkeypatch Redis 协调原语，避免触碰真实 Redis。"""
    monkeypatch.setattr(
        manager_module,
        "create_distributed_lock",
        (lambda key: lock) if lock is not None else (lambda key: FakeDistributedLock(key)),
    )
    monkeypatch.setattr(
        manager_module,
        "create_redis_semaphore",
        (lambda: sem) if sem is not None else (lambda: FakeSemaphore()),
    )


def _patch_concurrency(monkeypatch, **overrides):
    """覆盖 manager 模块的 CONCURRENCY_CONFIG，缩短超时以便测试。"""
    cfg = {
        "per_user_lock_timeout_sec": 60.0,
        "lock_retry_interval_sec": 0.01,
        "semaphore_acquire_timeout_sec": 120.0,
        "lock_heartbeat_interval_sec": 15.0,
    }
    cfg.update(overrides)
    monkeypatch.setattr(manager_module, "CONCURRENCY_CONFIG", cfg)


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


@pytest.mark.asyncio
async def test_process_message_serializes_same_user_via_mock_redis(monkeypatch):
    """mock Redis 层后，manager.process_message 同用户经本地锁串行。"""
    manager = WebHommeyManager()
    _mock_redis(monkeypatch)
    holder = StubProcess()
    manager._instances["u1"] = FakeInstance(holder.process)

    async def run(i):
        return await manager.process_message("u1", f"m{i}")

    results = await asyncio.gather(run(1), run(2))
    assert [r["response"] for r in results] == ["m1", "m2"]
    assert holder.max_active == 1  # 同用户从未并行


@pytest.mark.asyncio
async def test_process_message_local_lock_timeout_raises_user_queue_timeout(monkeypatch):
    """本地锁被占用且超时：抛 USER_QUEUE_TIMEOUT（Important 1）。"""
    manager = WebHommeyManager()
    _mock_redis(monkeypatch)
    _patch_concurrency(monkeypatch, per_user_lock_timeout_sec=0.05)
    manager._instances["u1"] = FakeInstance(StubProcess().process)

    # 先占用本地锁，模拟同 worker 内另一请求正在处理
    local_lock = manager._per_user_lock("u1")
    await local_lock.acquire()
    try:
        with pytest.raises(UpstreamError) as excinfo:
            await manager.process_message("u1", "m")
        assert excinfo.value.code == "USER_QUEUE_TIMEOUT"
        assert excinfo.value.retryable is True
    finally:
        local_lock.release()


@pytest.mark.asyncio
async def test_process_message_distributed_lock_timeout_raises_user_queue_timeout(monkeypatch):
    """分布式锁被占用且超时：抛 USER_QUEUE_TIMEOUT。"""
    manager = WebHommeyManager()
    lock = FakeDistributedLock("hommey:lock:user:u1", acquire_result=False)
    _mock_redis(monkeypatch, lock=lock)
    _patch_concurrency(monkeypatch, per_user_lock_timeout_sec=0.05)
    manager._instances["u1"] = FakeInstance(StubProcess().process)

    with pytest.raises(UpstreamError) as excinfo:
        await manager.process_message("u1", "m")
    assert excinfo.value.code == "USER_QUEUE_TIMEOUT"
    assert excinfo.value.retryable is True
    assert lock.acquire_calls > 0
    assert lock.released is False  # 从未拿到锁，不应释放


@pytest.mark.asyncio
async def test_process_message_semaphore_timeout_raises_global_limit(monkeypatch):
    """全局信号量打满且超时：抛 GLOBAL_CONCURRENCY_LIMIT。"""
    manager = WebHommeyManager()
    sem = FakeSemaphore(acquire_result=False)
    _mock_redis(monkeypatch, sem=sem)
    _patch_concurrency(monkeypatch, semaphore_acquire_timeout_sec=0.05)
    manager._instances["u1"] = FakeInstance(StubProcess().process)

    with pytest.raises(UpstreamError) as excinfo:
        await manager.process_message("u1", "m")
    assert excinfo.value.code == "GLOBAL_CONCURRENCY_LIMIT"
    assert excinfo.value.retryable is True
    assert sem.acquire_calls > 0
    assert sem.released is False  # 未拿到信号量，不应 release


@pytest.mark.asyncio
async def test_process_message_aborts_when_lock_lost(monkeypatch):
    """心跳续约失败（锁易主）时中止在途处理，抛 LOCK_LOST（Important 3）。"""
    manager = WebHommeyManager()
    lock = FakeDistributedLock("hommey:lock:user:u1", renew_result=False)
    _mock_redis(monkeypatch, lock=lock)
    # 心跳间隔远小于处理时长，保证续约失败先于处理完成被观察
    _patch_concurrency(monkeypatch, lock_heartbeat_interval_sec=0.005)
    holder = StubProcess()
    manager._instances["u1"] = FakeInstance(holder.process)

    with pytest.raises(UpstreamError) as excinfo:
        await manager.process_message("u1", "m")
    assert excinfo.value.code == "LOCK_LOST"
    assert excinfo.value.retryable is True
    assert lock.released is True  # 释放链仍执行


@pytest.mark.asyncio
async def test_process_message_aborts_when_heartbeat_renew_raises(monkeypatch):
    """心跳 renew() 抛异常（瞬时 Redis 连接错误）也置 lock_lost，中止在途处理并释放锁。

    防止 heartbeat 静默死亡：否则锁 TTL 过期后另一 worker 可取得同一用户锁，
    跨 worker 同用户并发处理。
    """
    manager = WebHommeyManager()
    lock = FakeDistributedLock(
        "hommey:lock:user:u1",
        renew_exc=ConnectionError("redis down"),
    )
    _mock_redis(monkeypatch, lock=lock)
    _patch_concurrency(monkeypatch, lock_heartbeat_interval_sec=0.005)
    holder = StubProcess()
    manager._instances["u1"] = FakeInstance(holder.process)

    with pytest.raises(UpstreamError) as excinfo:
        await manager.process_message("u1", "m")
    assert excinfo.value.code == "LOCK_LOST"
    assert excinfo.value.retryable is True
    assert lock.released is True  # 释放链仍执行


class FakeStreamInstance:
    """已初始化的流式实例替身：转发 stream_message。"""
    def __init__(self):
        self.initialized = True

    async def stream_message(self, message, request_id=None, attachment_ids=None):
        yield {"type": "status", "phase": "done"}
        yield {"type": "done", "preferences_updated": False, "timings": {}}


@pytest.mark.asyncio
async def test_process_message_lazy_initializes_missing_instance(monkeypatch):
    """跨 worker 场景：当前 worker 无该用户实例时，process_message 先懒初始化再处理（不 deadlock）。"""
    manager = WebHommeyManager()
    _mock_redis(monkeypatch)
    holder = StubProcess()
    init_calls = []

    async def fake_initialize_user(user_id):
        init_calls.append(user_id)
        manager._instances[user_id] = FakeInstance(holder.process)

    monkeypatch.setattr(manager, "initialize_user", fake_initialize_user)

    result = await manager.process_message("u1", "hello")

    assert init_calls == ["u1"]
    assert result["response"] == "hello"
    assert holder.max_active == 1


@pytest.mark.asyncio
async def test_stream_message_lazy_initializes_missing_instance(monkeypatch):
    """跨 worker 场景：stream_message 无该用户实例时同样懒初始化后再流式转发。"""
    manager = WebHommeyManager()
    _mock_redis(monkeypatch)
    init_calls = []

    async def fake_initialize_user(user_id):
        init_calls.append(user_id)
        manager._instances[user_id] = FakeStreamInstance()

    monkeypatch.setattr(manager, "initialize_user", fake_initialize_user)

    events = [e async for e in manager.stream_message("u1", "hello")]

    assert init_calls == ["u1"]
    assert events[-1]["type"] == "done"
