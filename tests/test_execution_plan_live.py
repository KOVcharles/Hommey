"""Opt-in provider compatibility test with synthetic policy/weather and no DB writes."""
import json
import os
import time

import pytest

from agent_runtime.engine import Supervisor
from agent_runtime.context_window import encoded_size
from tests.test_supervisor_live_control import FixtureServices
from tests.test_supervisor_runtime import FakeStore, SCOPE
from tests.test_supervisor_control import role_of


@pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in provider calls")
@pytest.mark.asyncio
async def test_live_weather_and_policy():
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG, SUPERVISOR_CONFIG
    from agent_runtime.model_client import create_tool_model
    raw_model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 25)
    calls = []
    async def model(messages, **kwargs):
        assert role_of(messages) in {"travel_info", "policy_rag"}
        calls.append(encoded_size(messages) + encoded_size(kwargs["tools"]))
        return await raw_model(messages, **kwargs)
    services = FixtureServices()
    store = FakeStore(services)
    runtime = Supervisor(model, services, store, SUPERVISOR_CONFIG)
    started = time.perf_counter()
    result = await runtime.run(SCOPE, "给我查一下南京天气以及相关的差旅标准")
    print(json.dumps({"elapsed_sec": round(time.perf_counter() - started, 2), "llm_calls": len(calls),
        "max_input_chars": max(calls), "business_calls": services.calls, "writes": store.writes,
        "outcome": result["outcome"], "steps": result["public_plan"]["steps"], "response": result["response"]}, ensure_ascii=False))
    assert len(result["public_plan"]["steps"]) == 2
    assert all(s["status"] in {"succeeded", "partial"} for s in result["public_plan"]["steps"])
    assert "get_weather" in services.calls and "search_policy" in services.calls
    assert store.writes == 0 and len(calls) <= 9 and max(calls) < 80000
    weather = next(r for r in store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["results"].values() if r["role"] == "travel_info")
    assert not any("差旅标准" in item for item in weather["missing_info"])
