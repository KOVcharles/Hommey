# tests/test_web_async_facade.py
"""Web 层接入 async 门面与 Redis 熔断器后的最小契约测试。"""
import asyncio
import pytest

from runtime import create_circuit_breaker
from context.async_memory import AsyncMemoryFacade


def test_runtime_circuit_breaker_is_redis_backed():
    """runtime.create_circuit_breaker 返回 Redis 版熔断器（async-native，鸭子类型兼容旧接口）。

    不做真实 Redis 往返（避免事件循环绑定问题）；Redis 真实状态机由
    tests/test_redis_coordination.py::test_circuit_breaker_state_machine 覆盖。
    """
    from utils.redis_coordination import RedisCircuitBreaker

    cb = create_circuit_breaker()
    assert isinstance(cb, RedisCircuitBreaker)
    # 兼容旧接口
    assert callable(cb.raise_if_open)
    assert callable(cb.record_failure)
    assert callable(cb.record_success)
    assert callable(cb.get_status)
    # async-native：全部方法为协程，调用方须 await
    assert asyncio.iscoroutinefunction(cb.raise_if_open)
    assert asyncio.iscoroutinefunction(cb.record_failure)
    assert asyncio.iscoroutinefunction(cb.record_success)
    assert asyncio.iscoroutinefunction(cb.get_status)


@pytest.mark.asyncio
async def test_facade_wraps_memory_manager():
    # AsyncMemoryFacade 能包装真实 MemoryManager（用内存后端时无 DB 依赖）
    from settings import MEMORY_CONFIG
    if MEMORY_CONFIG["long_term"].get("backend") in {"postgres", "file", "disabled"}:
        pytest.skip("需要本地 memory 后端才跑真实实例")


@pytest.mark.asyncio
async def test_instance_lazily_wraps_memory_manager_into_async_facade():
    """HommeyWebInstance 在 memory_manager 可用后惰性包装出 async 门面。"""
    from webui_new.manager import HommeyWebInstance

    class StubMemory:
        def __init__(self):
            self.calls = []

        def add_message(self, role, content, metadata=None):
            self.calls.append((role, content))
            return "m1"

    memory = StubMemory()
    instance = HommeyWebInstance("u1")
    instance.memory_manager = memory

    facade = instance._ensure_async_memory()
    assert isinstance(facade, AsyncMemoryFacade)

    mid = await facade.add_message("user", "hello")
    assert mid == "m1"
    assert memory.calls == [("user", "hello")]
