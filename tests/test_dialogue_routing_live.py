"""Opt-in prompt regression: real model decisions with isolated business fixtures.

HOMMEY_RUN_LIVE_AGENT_TESTS=1 python -m pytest tests/test_dialogue_routing_live.py -q
No production conversations or trips are read or written. Calls use model quota.
"""
import json
import os

import pytest

from agent_runtime.engine import Supervisor
from agent_runtime.model_client import create_tool_model
from tests.test_supervisor_runtime import CONFIG, FakeStore, SCOPE
from tests.test_supervisor_live_control import FixtureServices


pytestmark = pytest.mark.skipif(os.getenv("HOMMEY_RUN_LIVE_AGENT_TESTS") != "1", reason="opt-in real model decisions")


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected,max_calls", [
    ("1", "clarify", 0),
    ("随便看看", "clarify", 0),
    ("帮我写一个 Python 程序", "refuse", 0),
    ("麻烦帮我处理一下这个事情", "clarify", 2),
    ("这个要怎么弄呢", "clarify", 2),
    ("公司差旅住宿标准", "policy", 6),
    ("去南京出差", "trip", 5),
], ids=["bare_number", "vague_exact", "outside_scope", "vague_task", "vague_reference", "policy", "trip"])
async def test_live_routing_behavior(text, expected, max_calls):
    from runtime import _generate_kwargs
    from settings import LLM_CONFIG
    model = create_tool_model(LLM_CONFIG, _generate_kwargs(LLM_CONFIG), 20)
    calls = []
    async def measured(messages, **kwargs):
        calls.append(True)
        return await model(messages, **kwargs)
    services = FixtureServices()
    store = FakeStore(services)
    result = await Supervisor(measured, services, store, {**CONFIG, "child_timeout_sec": 40, "turn_timeout_sec": 60}).run(SCOPE, text)
    checkpoint = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]
    trace = [{"role": state.get("role", "main"),
              "tools": [c["function"] for m in state["messages"] for c in m.get("tool_calls", [])],
              "failures": [json.loads(m["content"])["failure"] for m in state["messages"]
                           if m["role"] == "tool" and json.loads(m["content"]).get("failure")]}
             for state in [checkpoint["main"], *checkpoint["children"].values()]]
    assert len(calls) <= max_calls, json.dumps(trace, ensure_ascii=False)
    if expected in {"clarify", "refuse"}:
        assert result["agents"] == [] and store.writes == 0 and services.calls == []
        assert result["presentation_document"] is None
        assert result["outcome"] == ("waiting_input" if expected == "clarify" else "completed")
        if calls:
            messages = store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["main"]["messages"]
            finishes = [json.loads(c["function"]["arguments"]) for m in messages for c in m.get("tool_calls", [])
                        if c["function"]["name"] == "finish"]
            assert finishes[-1]["kind"] == expected
    elif expected == "policy":
        assert [r["name"] for r in result["agents"]] == ["policy_rag"]
        assert result["answer_document"]["sources"] and store.writes == 0
        assert result["outcome"] in {"completed", "partial"}
    else:
        assert services.trip.get("destination") == "南京" and store.writes == 1, json.dumps(trace, ensure_ascii=False)
        assert result["presentation_document"]["type"] == "trip_intake"
        assert result["outcome"] == "waiting_input"
