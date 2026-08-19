# tests/test_redis_coordination.py
import asyncio
import pytest
import pytest_asyncio

from utils.redis_coordination import (
    DistributedLock,
    RedisSemaphore,
    RedisCircuitBreaker,
    get_redis_coordination_client,
)


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    c = get_redis_coordination_client()
    await c.delete(
        "test:lock:u1", "test:sem:g", "test:sem:lease", "test:sem:expire",
        "test:sem:rel", "hommey:cb:test:cb:state", "hommey:cb:test:cb:opened_at",
        "hommey:cb:test:cb:count",
    )
    yield c
    # 清理测试 key，避免污染（redis.asyncio 的 delete 是协程，需 await）
    await c.delete(
        "test:lock:u1",
        "test:sem:g",
        "test:sem:lease",
        "test:sem:expire",
        "test:sem:rel",
        "hommey:cb:test:cb:state",
        "hommey:cb:test:cb:opened_at",
        "hommey:cb:test:cb:count",
    )


@pytest.mark.asyncio(loop_scope="session")
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


@pytest.mark.asyncio(loop_scope="session")
async def test_lock_release_by_wrong_token_does_not_delete(client):
    lock = DistributedLock("test:lock:u1", ttl_ms=2000)
    await lock.acquire()
    # 一个 token 错误的"释放"不应删掉真锁
    wrong = DistributedLock("test:lock:u1", ttl_ms=2000)
    assert await wrong.release() is False
    # 真锁还在
    assert await lock.release() is True


@pytest.mark.asyncio(loop_scope="session")
async def test_semaphore_bounds_concurrency(client):
    # token 化租约：一个对象 = 一个持有者（唯一 token）；多个持有者 = 多个对象。
    sem_a = RedisSemaphore("test:sem:g", max_concurrency=2, ttl_sec=5)
    sem_b = RedisSemaphore("test:sem:g", max_concurrency=2, ttl_sec=5)
    sem_c = RedisSemaphore("test:sem:g", max_concurrency=2, ttl_sec=5)
    assert await sem_a.acquire() is True
    assert await sem_b.acquire() is True
    # 达到上限，第三个持有者获取失败
    assert await sem_c.acquire() is False
    await sem_a.release()
    # 释放一个槽位后，可重新获得
    assert await sem_c.acquire() is True
    await sem_b.release()
    await sem_c.release()


@pytest.mark.asyncio(loop_scope="session")
async def test_semaphore_lease_renewal_keeps_slot_across_lease_periods(client):
    """请求执行时间超过一个租约周期时，持续续约仍只占一个槽位（§5.3）。"""
    sem = RedisSemaphore("test:sem:lease", max_concurrency=1, ttl_sec=1)
    assert await sem.acquire() is True
    # 模拟跨多个租约周期的长请求：每 0.35s 续约，覆盖 ttl_sec=1
    for _ in range(4):
        await asyncio.sleep(0.35)
        assert await sem.renew() is True
    # 第二个持有者仍不能进入
    contender = RedisSemaphore("test:sem:lease", max_concurrency=1, ttl_sec=1)
    assert await contender.acquire() is False
    await sem.release()


@pytest.mark.asyncio(loop_scope="session")
async def test_semaphore_lease_expires_and_slot_is_reclaimed(client):
    """持有者不续约时，租约过期后槽位被自动回收（崩溃恢复）。"""
    sem = RedisSemaphore("test:sem:expire", max_concurrency=1, ttl_sec=1)
    assert await sem.acquire() is True
    # 不续约，等租约过期
    await asyncio.sleep(1.4)
    contender = RedisSemaphore("test:sem:expire", max_concurrency=1, ttl_sec=1)
    assert await contender.acquire() is True  # 过期租约已被回收
    await contender.release()


@pytest.mark.asyncio(loop_scope="session")
async def test_semaphore_release_only_removes_own_token(client):
    """释放只删除自己的 token，不影响其他持有者。"""
    sem_a = RedisSemaphore("test:sem:rel", max_concurrency=2, ttl_sec=5)
    sem_b = RedisSemaphore("test:sem:rel", max_concurrency=2, ttl_sec=5)
    assert await sem_a.acquire() is True
    assert await sem_b.acquire() is True
    await sem_a.release()  # 只释放 A，B 仍占一个槽位
    sem_c = RedisSemaphore("test:sem:rel", max_concurrency=2, ttl_sec=5)
    assert await sem_c.acquire() is True  # 有一个空槽位可用
    await sem_b.release()
    await sem_c.release()


@pytest.mark.asyncio(loop_scope="session")
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
