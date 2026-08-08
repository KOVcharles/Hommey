"""声明式记忆回写契约：MemoryHookExecutor 按 skill 声明的 memory_hooks 应用副作用。

不再有按 agent_name 硬编码的 Python 分支——效果、归属 agent、触发字段全部来自
hommey.yaml 的 ``memory_hooks``。
"""
import asyncio

from core.orchestration.memory_hooks import MemoryHookExecutor
from core.orchestration.models import TaskResult


class _FakeLongTerm:
    def __init__(self):
        self.preferences = {}
        self.trip_history = []
        self.active_trip = {}

    def get_preference(self, pref_type=None):
        if pref_type is None:
            return self.preferences
        return self.preferences.get(pref_type)

    def save_preference(self, pref_type, value):
        self.preferences[pref_type] = value

    def save_trip_history(self, trip_info):
        self.trip_history.append(trip_info)

    def upsert_active_trip(self, trip_info):
        self.active_trip.update(trip_info)
        return dict(self.active_trip)

    def get_active_trip(self):
        return self.active_trip or None


class _FakeMemoryManager:
    def __init__(self):
        self.long_term = _FakeLongTerm()
        self.current_request_id = "req-123"
        self.active_updates = []

    def update_active_trip(self, trip_info):
        self.active_updates.append(trip_info)
        return self.long_term.upsert_active_trip(trip_info)

    def get_active_trip(self):
        return self.long_term.get_active_trip()

    def complete_active_trip(self, reason="planning_completed"):
        if not self.get_active_trip():
            return None
        return self.long_term.upsert_active_trip({"status": "completed", "completion_reason": reason})


def _result(agent_name, intent, data, status="success"):
    return TaskResult(
        task_id=f"{intent}-{agent_name}",
        intent=intent,
        agent_name=agent_name,
        status=status,
        data=data,
    )


def test_event_collection_hook_updates_active_trip():
    manager = _FakeMemoryManager()
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("event_collection", "event_collection", {
            "origin": "北京", "destination": "上海",
            "start_date": "2026-08-08", "duration_days": 3,
        }),
    ]))

    assert manager.active_updates == [{
        "origin": "北京", "destination": "上海",
        "start_date": "2026-08-08", "duration_days": 3,
    }]
    assert manager.long_term.active_trip["destination"] == "上海"


def test_preference_hook_saves_replace_and_append():
    manager = _FakeMemoryManager()
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("preference", "preference", {
            "preferences": [
                {"type": "hotel_brand", "value": "如家", "action": "replace"},
                {"type": "airline", "value": "国航", "action": "append"},
            ],
        }),
    ]))

    assert manager.long_term.preferences["hotel_brand"] == "如家"
    assert manager.long_term.preferences["airline"] == ["国航"]


def test_preference_append_extends_existing_list():
    manager = _FakeMemoryManager()
    manager.long_term.preferences["airline"] = ["南航"]
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("preference", "preference", {
            "preferences": [{"type": "airline", "value": "国航", "action": "append"}],
        }),
    ]))

    assert manager.long_term.preferences["airline"] == ["南航", "国航"]


def test_preference_skips_sensitive_values():
    manager = _FakeMemoryManager()
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("preference", "preference", {
            "preferences": [{"type": "id_card", "value": "110101199001011234"}],
        }),
    ]))

    assert "id_card" not in manager.long_term.preferences


def test_complete_trip_saves_history_and_completes_active_trip():
    manager = _FakeMemoryManager()
    manager.update_active_trip({
        "origin": "北京", "destination": "上海",
        "start_date": "2026-08-08", "trip_purpose": "客户拜访",
    })
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("itinerary_planning", "itinerary_planning", {
            "itinerary": {"title": "上海出差方案"},
            "planning_complete": True,
        }),
    ]))

    assert len(manager.long_term.trip_history) == 1
    saved = manager.long_term.trip_history[0]
    assert saved["destination"] == "上海"
    assert saved["purpose"] == "客户拜访"
    assert saved["request_id"] == "req-123"
    assert manager.long_term.active_trip["status"] == "completed"


def test_complete_trip_skipped_without_require_field():
    manager = _FakeMemoryManager()
    manager.update_active_trip({"destination": "上海"})
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("itinerary_planning", "itinerary_planning", {
            "answer": "还在收集信息",  # 没有 itinerary 字段
        }),
    ]))

    assert manager.long_term.trip_history == []
    assert "status" not in manager.long_term.active_trip


def test_failed_results_never_trigger_hooks():
    manager = _FakeMemoryManager()
    executor = MemoryHookExecutor(manager)
    asyncio.run(executor.apply([
        _result("event_collection", "event_collection",
                {"destination": "上海"}, status="error"),
        _result("preference", "preference",
                {"preferences": [{"type": "hotel_brand", "value": "如家"}]}, status="error"),
    ]))

    assert manager.active_updates == []
    assert manager.long_term.preferences == {}


def test_executor_without_memory_manager_is_noop():
    executor = MemoryHookExecutor(None)
    asyncio.run(executor.apply([
        _result("event_collection", "event_collection", {"destination": "上海"}),
    ]))  # 不抛异常即通过
