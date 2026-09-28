"""Opt-in real model check; weather/rail fixtures and all persistence are isolated."""
import json
import os

import pytest

from agent_runtime.engine import Supervisor
from agent_runtime.model_client import create_tool_model
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeStore
from tests.test_tool_discovery import NanjingServices, checkpoint


pytestmark = pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in provider calls")


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True], ids=["full_request", "continue_request"])
async def test_live_weather_and_rail_are_both_delivered(continuation):
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG
    model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 25)
    user_request = "我从北京出发，今天去南京出差，请查南京天气以及去南京的高铁信息。"

    class Services(NanjingServices):
        async def context(self, scope):
            # The Web entry persists the current message before reading context.
            return {"trip": {}, "recent": [{"role": "user", "content": user_request},
                    {"role": "user", "content": "继续"}] if continuation else []}

    services = Services()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, {**CONFIG, "child_timeout_sec": 60,
        "turn_timeout_sec": 100}).run(SCOPE, "继续" if continuation else user_request)
    trace = [{"role": state.get("role", "main"),
              "calls": [c["function"] for m in state["messages"] for c in m.get("tool_calls", [])],
              "failures": [json.loads(m["content"])["failure"] for m in state["messages"]
                           if m["role"] == "tool" and json.loads(m["content"]).get("failure")]}
             for state in [checkpoint(store)["main"], *checkpoint(store)["children"].values()]]
    details = json.dumps({"trace": trace, "outcome": result["outcome"], "response": result["response"],
                          "business_calls": [name for _, name in services.calls]}, ensure_ascii=False)
    print(details, flush=True)
    assert result["outcome"] != "degraded", details
    assert "晴" in result["response"] and "G101" in result["response"], details
    assert sorted(name for _, name in services.calls) == ["get_weather", "search_trains"], details
    assert store.writes == 0
