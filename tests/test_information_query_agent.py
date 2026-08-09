from pathlib import Path
import asyncio
import importlib.util
import json

from agents.lazy_agent_registry import LazyAgentRegistry
from agentscope.message import Msg


def test_information_query_skill_is_registered():
    registry = LazyAgentRegistry(model=None, cache={})

    assert "information_query" in registry
    assert "query-info" in registry.keys()


def test_information_query_skill_has_agent_script():
    script_path = Path(".agents/skills/query-info/script/agent.py")

    assert script_path.exists()


def test_information_agent_uses_destination_entity_for_any_city():
    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_scope_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None)
    seen = {}

    async def fake_weather(query, city_hint=""):
        seen.update(query=query, city_hint=city_hint)
        return {"query_success": True, "results": {"summary": "ok"}}

    agent._weather_query = fake_weather
    message = Msg(
        name="Orchestrator", role="user",
        content=json.dumps({
            "context": {
                "agent_query": "南京天气以及差旅标准",
                "active_task": {
                    "query": "东京 2026-09-01 查询天气",
                    "entities": {"destination": "东京"},
                },
            },
            "previous_results": [],
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(message))

    assert json.loads(result.content)["query_success"] is True
    assert seen == {"query": "东京 2026-09-01 查询天气", "city_hint": "东京"}


def test_complete_trip_weather_uses_collected_destination_without_city_list():
    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_trip_scope_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None)
    seen = {}

    async def fake_weather(query, city_hint=""):
        seen.update(weather_query=query, city_hint=city_hint)
        return {"query_success": True, "results": {"summary": "ok"}}

    async def fake_search(query):
        seen["transport_query"] = query
        return {"query_success": True, "results": {"summary": "ok"}}

    agent._weather_query = fake_weather
    agent._web_search = fake_search
    result = asyncio.run(agent._trip_information_query({
        "origin": "Beijing",
        "destination": "München",
        "start_date": "2026-09-01",
        "duration_days": 3,
    }))

    assert result["query_success"] is True
    assert seen["city_hint"] == "München"
    assert "München" in seen["weather_query"]
    assert "Beijing到München" in seen["transport_query"]
