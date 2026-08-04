# tests/test_async_memory.py
import asyncio
from context.async_memory import AsyncMemoryFacade


class _LongTerm:
    """镜像真实 MemoryManager.long_term 的同步替身。"""
    def __init__(self, owner):
        self._owner = owner

    def get_preference(self):
        return dict(self._owner.pref)

    def save_preference(self, pref_type, value):
        self._owner.calls.append(("save_preference", pref_type))
        self._owner.pref[pref_type] = value

    def save_trip_history(self, trip_info):
        self._owner.calls.append(("save_trip_history",))

    def get_trip_history(self, limit=None):
        return [{"destination": "北京"}]


class _ShortTerm:
    """镜像真实 MemoryManager.short_term 的同步替身。"""
    def __init__(self, owner):
        self._owner = owner

    def get_recent_context(self, n_turns=None):
        return [{"role": "user", "content": "hi"}]


class StubManager:
    """同步 memory_manager 替身：镜像真实 MemoryManager 的 long_term/short_term 结构，记录是否真的走了 to_thread。"""
    def __init__(self):
        self.calls = []
        self.pref = {"home_location": "上海"}
        self.long_term = _LongTerm(self)
        self.short_term = _ShortTerm(self)

    def add_message(self, role, content, metadata=None):
        self.calls.append(("add_message", role))
        return "m1"

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
