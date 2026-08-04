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
