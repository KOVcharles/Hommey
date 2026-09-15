# tests/test_async_memory.py
import asyncio
import threading

from context.async_memory import AsyncMemoryFacade


class _LongTerm:
    """镜像真实 MemoryManager.long_term 的同步替身。"""
    def __init__(self, owner):
        self._owner = owner

    def _record(self):
        self._owner.thread_ids.append(threading.get_ident())

    def get_preference(self):
        self._record()
        return dict(self._owner.pref)

    def save_preference(self, pref_type, value):
        self._record()
        self._owner.calls.append(("save_preference", pref_type))
        self._owner.pref[pref_type] = value

    def save_trip_history(self, trip_info):
        self._record()
        self._owner.calls.append(("save_trip_history",))

    def get_trip_history(self, limit=None):
        self._record()
        return [{"destination": "北京"}]


class _ShortTerm:
    """镜像真实 MemoryManager.short_term 的同步替身。"""
    def __init__(self, owner):
        self._owner = owner

    def _record(self):
        self._owner.thread_ids.append(threading.get_ident())

    def get_recent_context(self, n_turns=None):
        self._record()
        return [{"role": "user", "content": "hi"}]


class StubManager:
    """同步 memory_manager 替身：镜像真实 MemoryManager 的 long_term/short_term 结构，记录是否真的走了 to_thread。"""
    def __init__(self):
        self.calls = []
        self.pref = {"home_location": "上海"}
        self.thread_ids = []
        self._thread_id = threading.get_ident()
        self.long_term = _LongTerm(self)
        self.short_term = _ShortTerm(self)

    def _record(self):
        self.thread_ids.append(threading.get_ident())

    def add_message(self, role, content, metadata=None):
        self._record()
        self.calls.append(("add_message", role))
        return "m1"

    def get_active_trip(self):
        self._record()
        return None

    def update_active_trip(self, trip_info):
        self._record()
        self.calls.append(("update_active_trip",))
        return trip_info

    def complete_active_trip(self, reason="planning_completed"):
        self._record()
        self.calls.append(("complete_active_trip", reason))
        return None

    def cancel_active_trip(self, reason="user_cancelled"):
        self._record()
        self.calls.append(("cancel_active_trip", reason))
        return None


def test_facade_routes_to_sync_manager_and_returns_expected():
    manager = StubManager()
    facade = AsyncMemoryFacade(manager)
    main_thread_id = threading.get_ident()

    async def run():
        mid = await facade.add_message("user", "hello")
        pref = await facade.get_preference()
        await facade.save_preference("budget_level", "L1")
        active = await facade.get_active_trip()
        recent = await facade.get_recent_context(3)
        trips = await facade.get_trip_history()
        updated = await facade.update_active_trip({"destination": "深圳"})
        completed = await facade.complete_active_trip("user_done")
        cancelled = await facade.cancel_active_trip("user_change")
        await facade.save_trip_history({"destination": "广州"})

        assert mid == "m1"
        assert pref == {"home_location": "上海"}
        assert manager.pref["budget_level"] == "L1"
        assert active is None
        assert recent == [{"role": "user", "content": "hi"}]
        assert trips == [{"destination": "北京"}]
        assert updated == {"destination": "深圳"}
        assert completed is None
        assert cancelled is None
        assert ("add_message", "user") in manager.calls
        assert ("save_preference", "budget_level") in manager.calls
        assert ("update_active_trip",) in manager.calls
        assert ("complete_active_trip", "user_done") in manager.calls
        assert ("cancel_active_trip", "user_change") in manager.calls
        assert ("save_trip_history",) in manager.calls
        assert manager.thread_ids and all(
            t != main_thread_id for t in manager.thread_ids
        ), "同步调用必须实际跑在 to_thread 的 worker 线程"

    asyncio.run(run())


def test_cancelled_message_waits_for_actual_thread_write_to_finish():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Memory:
        def add_message(self, *args):
            started.set()
            assert release.wait(3)
            finished.set()
            return "message"

    async def run():
        task = asyncio.create_task(AsyncMemoryFacade(Memory()).add_message("user", "hello"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done(), "Do not release the session lock while the DB thread is writing"
        finally:
            release.set()
            result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert finished.is_set()

    asyncio.run(run())
