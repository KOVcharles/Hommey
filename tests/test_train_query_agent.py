"""train-query 智能体测试：注册、行程卡消费、query 解析回退、失败降级。"""
from pathlib import Path
import asyncio
import importlib.util
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from agents.lazy_agent_registry import LazyAgentRegistry
from agentscope.message import Msg

SCRIPT_PATH = Path(".agents/skills/train-query/script/agent.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("train_query_scope_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_train_query_skill_is_registered():
    registry = LazyAgentRegistry(model=None, cache={})

    assert "train_query" in registry
    assert "train-query" in registry.keys()


def test_train_query_skill_has_agent_script():
    assert SCRIPT_PATH.exists()


def test_train_query_returns_structured_trains_from_backend():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)
    seen = {}

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            seen.update(origin=origin, destination=destination, date=date)
            return [{
                "train_no": "G2", "from_station": "上海虹桥", "to_station": "北京南",
                "depart_time": "07:00", "arrive_time": "11:30", "duration": "4:30",
                "seats": {"二等座": "有"}, "prices": {},
            }]

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator", role="user",
        content=json.dumps({
            "context": {
                "agent_query": "帮我查一下上海到北京的高铁车次",
                "active_task": {
                    "query": "帮我查一下上海到北京的高铁车次",
                    "entities": {"origin": "上海", "destination": "北京", "start_date": "2026-08-14"},
                },
            },
            "previous_results": [],
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is True
    assert data["results"]["trains"][0]["train_no"] == "G2"
    assert data["results"]["trains"][0]["from_station"] == "上海虹桥"
    assert seen == {"origin": "上海", "destination": "北京", "date": "2026-08-14"}
    assert "核验" in data["results"]["note"]


def test_train_query_consumes_trip_card_from_previous_results():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)
    seen = {}

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            seen.update(origin=origin, destination=destination, date=date)
            return []

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator", role="user",
        content=json.dumps({
            "context": {"agent_query": "查询车次", "active_task": {"query": "查询车次", "entities": {}}},
            "previous_results": [
                {"agent_name": "event_collection", "result": {"data": {
                    "planning_ready": True, "origin": "上海", "destination": "南京",
                    "start_date": "2026-08-20",
                }}},
            ],
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is True
    assert seen == {"origin": "上海", "destination": "南京", "date": "2026-08-20"}


def test_train_query_queries_both_legs_for_complete_company_trip():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)
    seen = []

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            seen.append((origin, destination, date))
            return [{
                "train_no": "G49" if origin == "北京" else "G38",
                "from_station": f"{origin}南站",
                "to_station": f"{destination}南站",
                "depart_time": "19:04" if origin == "北京" else "18:30",
                "arrive_time": "22:18" if origin == "北京" else "22:05",
                "duration": "3:14" if origin == "北京" else "3:35",
                "seats": {"二等座": "有"},
                "prices": {},
            }]

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator",
        role="user",
        content=json.dumps({
            "context": {"agent_query": "查询本次出差车次", "active_task": {"entities": {}}},
            "previous_results": [{
                "agent_name": "event_collection",
                "result": {"data": {
                    "planning_ready": True,
                    "origin": "北京",
                    "destination": "南京",
                    "start_date": "2026-08-19",
                    "duration_days": 2,
                }},
            }],
        }, ensure_ascii=False),
    )

    data = json.loads(asyncio.run(agent.reply(message)).content)

    assert seen == [
        ("北京", "南京", "2026-08-19"),
        ("南京", "北京", "2026-08-20"),
    ]
    assert data["query_success"] is True
    assert data["results"]["round_trip_complete"] is True
    assert data["results"]["outbound"]["trains"][0]["train_no"] == "G49"
    assert data["results"]["return_trip"]["trains"][0]["train_no"] == "G38"
    assert [row["direction"] for row in data["results"]["trains"]] == ["去程", "返程"]
    assert [row["travel_date"] for row in data["results"]["trains"]] == [
        "2026-08-19", "2026-08-20",
    ]


def test_train_query_uses_explicit_end_date_and_return_location():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)

    assert agent._resolve_return_query(
        {
            "end_date": "2026-08-25",
            "duration_days": 2,
            "return_location": "天津",
        },
        "北京",
        "南京",
        "2026-08-19",
    ) == ("南京", "天津", "2026-08-25")


def test_return_query_failure_keeps_real_outbound_results():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)

    class PartialBackend:
        async def query_trains(self, origin, destination, date):
            if origin == "南京":
                raise RuntimeError("return unavailable")
            return [{
                "train_no": "G49", "from_station": "北京南", "to_station": "南京南",
                "depart_time": "19:04", "arrive_time": "22:18", "duration": "3:14",
                "seats": {}, "prices": {},
            }]

    agent._backend = PartialBackend()
    message = Msg(
        name="Orchestrator",
        role="user",
        content=json.dumps({
            "context": {"agent_query": "查询本次出差车次", "active_task": {"entities": {}}},
            "previous_results": [{
                "agent_name": "event_collection",
                "result": {"data": {
                    "planning_ready": True,
                    "origin": "北京",
                    "destination": "南京",
                    "start_date": "2026-08-19",
                    "duration_days": 2,
                }},
            }],
        }, ensure_ascii=False),
    )

    data = json.loads(asyncio.run(agent.reply(message)).content)

    assert data["query_success"] is True
    assert data["results"]["round_trip_complete"] is False
    assert data["results"]["trains"][0]["direction"] == "去程"
    assert data["results"]["return_trip"]["query_success"] is False
    assert "返程车次查询暂时不可用" in data["results"]["return_trip"]["message"]


def test_train_query_parses_route_and_date_without_trip_card():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)
    seen = {}

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            seen.update(origin=origin, destination=destination, date=date)
            return []

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator", role="user",
        content=json.dumps({
            "context": {"agent_query": "帮我查一下这周四上海到北京的高铁车次", "active_task": {"entities": {}}},
            "previous_results": [],
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is True
    assert seen["origin"] == "上海" and seen["destination"] == "北京"


def test_train_query_fails_gracefully_when_backend_errors():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)

    class FailingBackend:
        async def query_trains(self, origin, destination, date):
            raise RuntimeError("connection refused")

    agent._backend = FailingBackend()
    message = Msg(
        name="Orchestrator", role="user",
        content=json.dumps({
            "context": {
                "agent_query": "上海到北京高铁",
                "active_task": {
                    "entities": {"origin": "上海", "destination": "北京", "start_date": "2026-08-14"},
                },
            },
            "previous_results": [],
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is False
    assert "核验" in data["results"]["note"]


def test_train_query_defaults_missing_date_to_today_in_china_timezone():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)
    seen = {}

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            seen.update(origin=origin, destination=destination, date=date)
            return []

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator",
        role="user",
        content=json.dumps(
            {
                "context": {
                    "agent_query": "南京到重庆的车票",
                    "active_task": {"entities": {"origin": "南京", "destination": "重庆"}},
                },
                "previous_results": [],
            },
            ensure_ascii=False,
        ),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is True
    assert seen == {
        "origin": "南京",
        "destination": "重庆",
        "date": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
    }


def test_train_query_uses_home_location_for_destination_only_query():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)
    seen = {}

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            seen.update(origin=origin, destination=destination, date=date)
            return []

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator",
        role="user",
        content=json.dumps(
            {
                "context": {
                    "agent_query": "南京的高铁查一下",
                    "active_task": {"query": "南京的高铁查一下", "entities": {}},
                    "user_preferences": {"home_location": "上海"},
                },
                "previous_results": [],
            },
            ensure_ascii=False,
        ),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is True
    assert seen["origin"] == "上海"
    assert seen["destination"] == "南京"
    assert data["results"]["query_context"]["origin_source"] == (
        "user_preferences.home_location"
    )
    assert "长期记忆" in data["results"]["assumptions"][0]


def test_train_query_asks_for_complete_route_when_missing():
    module = _load_module()
    agent = module.TrainQueryAgent(model=None)

    class FakeBackend:
        async def query_trains(self, origin, destination, date):
            raise AssertionError("backend 不应被调用")

    agent._backend = FakeBackend()
    message = Msg(
        name="Orchestrator", role="user",
        content=json.dumps({
            "context": {"agent_query": "帮我查一下车次", "active_task": {"entities": {}}},
            "previous_results": [],
        }, ensure_ascii=False),
    )

    result = asyncio.run(agent.reply(message))
    data = json.loads(result.content)

    assert data["query_success"] is False
    assert "出发城市" in data["results"]["message"]
