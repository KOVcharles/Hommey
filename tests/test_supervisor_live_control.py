"""Opt-in real LLM + deterministic business fixtures, no production user writes.

This verifies provider schema/control compatibility, not real RAG/12306 quality.
"""
import asyncio
import json
import os
import time

import pytest

from agent_runtime.context_window import encoded_size
from agent_runtime.engine import Supervisor
from tests.test_supervisor_runtime import FakeServices, FakeStore, SCOPE


pytestmark = pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in provider calls")


class FixtureServices(FakeServices):
    def __init__(self):
        super().__init__()
        self.prefs = {"transportation_preference": "高铁", "hotel_brands": ["汉庭"]}

    async def execute(self, scope, name, args):
        self.calls.append(name)
        if name == "search_policy":
            return "policy", [{"content": "测试专用制度：南京属二类城市，普通员工住宿限额400元每晚、餐补80元每天，高铁二等座，市内交通据实报销。", "metadata": {"title": "合成测试制度，非生产政策"}}]
        if name == "search_memory":
            return "memory", [{"kind": "preferences", "data": dict(self.prefs)}]
        if name == "get_weather":
            return "weather", {"city": "南京", "forecasts": [{"date": "2026-09-11", "day_condition": "晴", "low_c": 22, "high_c": 28}]}
        if name == "search_trains":
            return "train", [{"train_no": "G_TEST", "from_station": args.origin, "to_station": args.destination,
                "depart_time": "08:00", "arrive_time": "12:00", "duration": "04:00", "travel_date": args.date, "seats": {"二等座": "有"}}]
        return "hotel", {"notice": "测试未配置酒店和通勤结果"}


@pytest.mark.asyncio
async def test_live_filled_trip_commits_and_terminates():
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG, SUPERVISOR_CONFIG
    from agent_runtime.model_client import create_tool_model
    model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 25)
    calls = []
    async def measured(messages, **kwargs):
        calls.append(encoded_size(messages) + encoded_size(kwargs["tools"]))
        print(json.dumps({"llm_call": len(calls), "input_chars": calls[-1]}), flush=True)
        return await model(messages, **kwargs)
    services = FixtureServices()
    store = FakeStore(services)
    runtime = Supervisor(measured, services, store, {**SUPERVISOR_CONFIG, "turn_timeout_sec": 60})
    started = time.perf_counter()
    result = await asyncio.wait_for(runtime.run(SCOPE,
        "从北京出发，目的地：南京，2026-09-11出发，出差2天，出差目的：参加会议"), 65)
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    for child in [checkpoint["main"], *checkpoint["children"].values()]:
        print(json.dumps({"role": child.get("role", "main"), "tools": [
            t["function"]["name"] for m in child["messages"] for t in m.get("tool_calls", [])],
            "errors": [json.loads(m["content"]) for m in child["messages"] if m["role"] == "tool" and json.loads(m["content"]).get("error")]}, ensure_ascii=False), flush=True)
    print(json.dumps({"elapsed_sec": round(time.perf_counter()-started, 2), "llm_calls": len(calls),
        "business_calls": services.calls, "writes": store.writes, "outcome": result.get("outcome", "completed"),
        "stop_reason": result.get("stop_reason"), "roles": result["agents"],
        "trip": services.trip, "response": result["response"],
        "failures": [c.get("last_error") for c in checkpoint["children"].values() if c.get("last_error")]}, ensure_ascii=False), flush=True)
    assert services.trip.get("origin") == "北京" and services.trip.get("destination") == "南京"
    assert services.trip.get("trip_purpose") == "参加会议" and services.trip.get("duration_days") == 2
    assert store.writes == 1 and result["response"]
    assert len(calls) <= 24 and max(calls) < 80000
    assert checkpoint["control"]["status"] == "completed"
    assert not any(r["status"] in {"unavailable", "error"} for r in result["agents"])
    assert result.get("outcome") != "degraded", "Fixture happy path must finish normally; failure fallback is tested separately"
