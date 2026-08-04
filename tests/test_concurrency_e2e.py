# tests/test_concurrency_e2e.py
import asyncio
import pytest

from webui_new.manager import WebHommeyManager
from utils.redis_coordination import get_redis_coordination_client


class StubInstance:
    """Stub HommeyWebInstance：只记录并发，不走真实 LLM/DB。"""
    def __init__(self):
        self.initialized = True
        self.active = 0
        self.max_active = 0

    async def process_message(self, message, *, request_id=None, attachment_ids=None, progress_callback=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return {"response": message}


@pytest.mark.asyncio(loop_scope="session")
async def test_same_user_serializes_across_real_redis():
    manager = WebHommeyManager()
    stub = StubInstance()
    manager._instances["u1"] = stub

    async def send(i):
        return await manager.process_message("u1", f"msg-{i}")

    results = await asyncio.gather(send(1), send(2))
    assert sorted(r["response"] for r in results) == ["msg-1", "msg-2"]
    assert stub.max_active == 1  # 同用户真实 Redis 锁串行

    # 清理
    await get_redis_coordination_client().delete("hommey:lock:user:u1", "hommey:global:semaphore")


@pytest.mark.asyncio(loop_scope="session")
async def test_different_users_run_in_parallel():
    manager = WebHommeyManager()
    stub1 = StubInstance()
    stub2 = StubInstance()
    manager._instances["u1"] = stub1
    manager._instances["u2"] = stub2

    async def send(uid, i):
        return await manager.process_message(uid, f"msg-{i}")

    results = await asyncio.gather(send("u1", 1), send("u2", 2))
    assert sorted(r["response"] for r in results) == ["msg-1", "msg-2"]
    # 不同用户应可并行（若 max_active 为 1 说明被错误串行）
    assert stub1.max_active == 1
    assert stub2.max_active == 1

    await get_redis_coordination_client().delete("hommey:lock:user:u1", "hommey:lock:user:u2", "hommey:global:semaphore")
