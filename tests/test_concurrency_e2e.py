# tests/test_concurrency_e2e.py
import asyncio
import pytest

from webui_new.manager import HommeyWebInstance, WebHommeyManager
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


class StubStreamInstance(HommeyWebInstance):
    """最小 stub：只覆盖 process_message，验证 stream_message 内层 request_task 的取消语义。"""

    def __init__(self):
        super().__init__("u1")
        self.initialized = True
        self.process_started = asyncio.Event()
        self.inner_cancelled = False
        self.memory_written = False

    async def process_message(self, message, *, request_id=None, attachment_ids=None, progress_callback=None):
        """模拟真实 process_message：先报一个进度事件，然后阻塞等待（直到被取消）。"""
        self.process_started.set()
        try:
            if progress_callback is not None:
                await progress_callback({"type": "agent_progress", "agent": "stub"})
            await asyncio.Event().wait()  # 模拟长耗时 LLM 工作，永不自然结束
        except asyncio.CancelledError:
            self.inner_cancelled = True
            raise
        self.memory_written = True  # 只有自然完成后才“写记忆”
        return {
            "response": message,
            "answer_document": {"content": message},
            "preferences_updated": False,
            "timings": {},
        }


@pytest.mark.asyncio(loop_scope="session")
async def test_stream_cancel_cancels_inner_request_task():
    """前端断连取消外层流时，instance.stream_message 的内层 request_task 被取消，
    不再继续执行 process_message（不写记忆），不遗留无锁孤儿任务。"""
    stub = StubStreamInstance()
    agen = stub.stream_message("hello", request_id="rid-1")
    collected = []

    async def consume():
        async for event in agen:
            collected.append(event)

    consumer = asyncio.create_task(consume())
    await stub.process_started.wait()
    # 让内层任务的进度事件被消费、生成器挂起在 queue.get()/yield 上，再“断连”取消
    for _ in range(100):
        if len(collected) >= 2:  # status + agent_progress 都已被消费
            break
        await asyncio.sleep(0.01)
    assert len(collected) >= 2, "expected progress event consumed before cancel"

    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert stub.inner_cancelled is True  # 内层 request_task 已被取消
    assert stub.memory_written is False  # 取消后未再写记忆
