import asyncio
import importlib.util
import json
from pathlib import Path

from agentscope.message import Msg

from agents.lazy_agent_registry import LazyAgentRegistry


def test_event_collection_skill_is_registered():
    registry = LazyAgentRegistry(model=None, cache={})

    assert "event_collection" in registry
    assert "event-collection" in registry.keys()


def test_event_collection_skill_has_agent_script():
    script_path = Path(".agents/skills/event-collection/script/agent.py")

    assert script_path.exists()


def test_event_collection_uses_recent_user_dialogue_to_fill_current_trip():
    script_path = Path(".agents/skills/event-collection/script/agent.py")
    spec = importlib.util.spec_from_file_location("event_collection_agent_for_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = {}

    async def model(messages):
        captured["prompt"] = messages[0]["content"]
        return {"content": json.dumps({
            "origin": "北京",
            "destination": "南京",
            "start_date": "2026-08-05",
            "end_date": None,
            "duration_days": None,
            "return_location": None,
            "trip_purpose": None,
            "work_location": None,
            "work_schedule": None,
        }, ensure_ascii=False)}

    agent = module.EventCollectionAgent(model=model)
    request = Msg(
        name="orchestrator",
        role="user",
        content=json.dumps({
            "context": {
                "rewritten_query": "帮我继续规划",
                "active_trip": {"origin": "北京", "destination": "南京"},
                "user_preferences": {
                    "home_location": "广州",
                    "frequent_destinations": ["深圳"],
                },
                "recent_dialogue": [
                    {"role": "user", "content": "我8月5日出发"},
                    {"role": "assistant", "content": "请补充日期"},
                ],
            }
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(request))

    assert "【当前会话最近提供的行程信息】" in captured["prompt"]
    assert "我8月5日出发" in captured["prompt"]
    assert "请补充日期" not in captured["prompt"]
    assert "家庭住址: 广州" not in captured["prompt"]
    assert "深圳" not in captured["prompt"]
    assert "用户偏好（包括家庭住址、常去城市）不得补全为当前行程事实" in captured["prompt"]
    assert json.loads(result.content)["destination"] == "南京"


def test_event_collection_rejects_locations_not_grounded_in_the_current_task():
    script_path = Path(".agents/skills/event-collection/script/agent.py")
    spec = importlib.util.spec_from_file_location("event_collection_grounding_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def model(_messages):
        return {"content": json.dumps({
            "origin": "北京",
            "destination": "南京",
            "start_date": None,
            "end_date": None,
            "duration_days": None,
            "return_location": None,
            "trip_purpose": None,
            "work_location": None,
            "work_schedule": None,
            "summary": "北京到南京的备选路线",
        }, ensure_ascii=False)}

    agent = module.EventCollectionAgent(model=model)
    request = Msg(
        name="orchestrator",
        role="user",
        content=json.dumps({
            "context": {
                "rewritten_query": "如果航班延误，帮我准备一条备选路线",
                "user_preferences": {"home_location": "北京"},
                "active_trip": {},
                "recent_dialogue": [],
            }
        }, ensure_ascii=False),
    )

    result = json.loads(asyncio.run(agent.reply(request)).content)

    assert result["origin"] is None
    assert result["destination"] is None
    assert "summary" not in result
    assert result["missing_required"][:2] == ["origin", "destination"]
