from pathlib import Path
import asyncio
import importlib.util
import json
from datetime import datetime

from agents.lazy_agent_registry import LazyAgentRegistry
from agentscope.message import Msg
from core.integrations.places.models import (
    GeoPoint,
    TransitRouteOption,
    TransitRoutePlan,
    VerifiedPlace,
    WeatherCurrent,
    WeatherForecastDay,
    WeatherReport,
)


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


def test_weather_prefers_amap_when_web_service_is_configured():
    class FakeService:
        configured = True

        async def weather(self, city, *, adcode=""):
            assert city == "杭州"
            return WeatherReport(
                city="杭州市", adcode="330100",
                current=WeatherCurrent(
                    condition="多云", temperature_c=29, humidity_pct=68,
                ),
                forecasts=[WeatherForecastDay(
                    date="2026-08-30", day_condition="多云", night_condition="小雨",
                    low_c=24, high_c=31,
                )],
                retrieved_at=datetime.now().astimezone(),
            )

    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_amap_weather_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None, travel_info_service=FakeService())

    result = asyncio.run(agent._weather_query("杭州天气", city_hint="杭州"))

    assert result["query_success"] is True
    assert result["results"]["provider"] == "amap"
    assert "杭州" in result["results"]["summary"]
    assert "24~31°C" in result["results"]["summary"]
    assert result["results"]["weather"]["adcode"] == "330100"


def test_weather_uses_open_meteo_when_amap_is_not_configured():
    class FakeService:
        configured = False

    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_weather_fallback_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None, travel_info_service=FakeService())
    seen = {}

    async def fallback(city, _httpx):
        seen["city"] = city
        return {"query_success": True, "results": {"summary": "fallback"}}

    agent._open_meteo_weather_query = fallback
    result = asyncio.run(agent._weather_query("东京天气", city_hint="东京"))

    assert result["query_success"] is True
    assert seen == {"city": "东京"}


def test_open_meteo_fallback_geocodes_overseas_city_in_local_timezone():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeHttpx:
        calls = []

        @classmethod
        def get(cls, url, *, params, **_kwargs):
            cls.calls.append((url, params))
            if "geocoding-api" in url:
                return FakeResponse({"results": [{
                    "latitude": 48.137, "longitude": 11.575,
                }]})
            assert params["timezone"] == "auto"
            return FakeResponse({
                "current": {
                    "temperature_2m": 18, "relative_humidity_2m": 61, "weather_code": 2,
                },
                "daily": {
                    "time": ["2026-08-30"], "weather_code": [2],
                    "temperature_2m_max": [23], "temperature_2m_min": [13],
                    "precipitation_probability_max": [20],
                },
            })

    class FakeService:
        configured = False

    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_overseas_weather_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None, travel_info_service=FakeService())

    result = asyncio.run(agent._open_meteo_weather_query("München", FakeHttpx))

    assert result["query_success"] is True
    assert "München当前天气" in result["results"]["summary"]
    assert len(FakeHttpx.calls) == 2


def test_explicit_route_prefers_amap_transit_between_verified_pois():
    now = datetime.now().astimezone()
    origin = VerifiedPlace(
        provider_place_id="O1", name="上海虹桥站", city="上海市", adcode="310105",
        citycode="021", location=GeoPoint(lng=121.327, lat=31.200), verified_at=now,
    )
    destination = VerifiedPlace(
        provider_place_id="D1", name="静安寺", city="上海市", adcode="310106",
        citycode="021", location=GeoPoint(lng=121.445, lat=31.223), verified_at=now,
    )

    class FakeService:
        configured = True

        async def resolve_anchor(self, keyword, *, city=""):
            places = {"上海虹桥站": origin, "静安寺": destination}
            place = places[keyword]
            return place, [place]

        async def transit_routes(self, route_origin, route_destination, *, limit=3):
            assert route_origin is origin
            assert route_destination is destination
            return TransitRoutePlan(
                origin=origin,
                destination=destination,
                options=[TransitRouteOption(
                    distance_m=18400, duration_sec=2700, transit_fee_cny=5,
                    lines=["地铁2号线"],
                )],
                retrieved_at=now,
            )

    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_amap_route_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None, travel_info_service=FakeService())

    result = asyncio.run(agent._local_transport_query("上海虹桥站到静安寺怎么走"))

    assert result["query_success"] is True
    assert result["results"]["provider"] == "amap"
    assert "地铁2号线" in result["results"]["summary"]
    assert "约45分钟" in result["results"]["summary"]


def test_intercity_workflow_transport_does_not_guess_city_center_route():
    class FakeService:
        configured = True

        async def resolve_anchor(self, *_args, **_kwargs):
            raise AssertionError("intercity trip must not guess concrete AMap route endpoints")

    script_path = Path(".agents/skills/query-info/script/agent.py")
    spec = importlib.util.spec_from_file_location("query_info_intercity_route_test", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.InformationQueryAgent(model=None, travel_info_service=FakeService())
    seen = {}

    async def web_fallback(query):
        seen["query"] = query
        return {"query_success": True, "results": {"summary": "公开交通信息"}}

    agent._web_search = web_fallback
    result = asyncio.run(agent._local_transport_query(
        "北京到上海的航班和接驳", origin="北京", destination="上海", allow_amap=False,
    ))

    assert result["query_success"] is True
    assert seen["query"] == "北京到上海的航班和接驳"
